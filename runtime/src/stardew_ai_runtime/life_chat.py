"""Life session service: companion life chat sessions.

Session key: ``life:{backend}:{save_id}`` stored in the same chat_sessions.json
used by ChatBridge. This keeps the two session namespaces isolated by key prefix.

Key behaviours:
- Session fingerprint includes profileRevision + memoryRevision; any change
  rotates the life session so old agreements cannot bleed through.
- Each turn injects STARDEW_MCP_SURFACE=life into the backend environment.
- STARDEW_DECISION_TOKEN is NEVER injected.
- begin_decision is NEVER called.
- Scheduler/autonomy state is NEVER touched.
- mode=plan turn only includes read-only WorkStore overview; never dispatches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("stardew_ai_runtime.life_chat")

_PERSONALITY_PROMPTS: dict[str, str] = {
    "gentle": "你是一个温柔体贴的农场伙伴，说话轻声细语，充满关怀，喜欢用温暖的语气鼓励农夫。",
    "lively": "你是一个活泼开朗的农场伙伴，说话充满活力，喜欢用欢快的语气，偶尔加入感叹词。",
    "calm": "你是一个沉稳内敛的农场伙伴，说话简洁有力，不轻易表达情绪，但关心农夫的每一件事。",
    "tsundere": "你是一个嘴硬心软的农场伙伴，表面上总是嘴硬，实际上非常在乎农夫，会在不经意间表达真实关心。",
}

_PLAY_STYLE_SUMMARIES: dict[str, str] = {
    "earn": "优先赚钱：收获出货、按需补种，遵守每日购买上限",
    "workhorse": "任劳任怨：浇水除草收获、喂动物、收机器成品等日常杂务",
    "community": "社区中心（规划中能力）",
    "decor": "农场装修（规划中能力）",
}


def _life_session_key(backend: str, save_id: str) -> str:
    return f"life:{backend}:{save_id}"


def _life_fingerprint(profile_revision: int, memory_revision: int) -> str:
    material = json.dumps(
        {"profileRevision": profile_revision, "memoryRevision": memory_revision},
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class LifeChatService:
    """Manages life chat sessions using the same session store as ChatBridge.

    The bridge passes its ``sessions`` dict by reference so both share the same
    persistent backing. Session keys are namespaced with ``life:`` prefix.
    """

    def __init__(
        self,
        backend_name: str,
        sessions: dict[str, str],
        sessions_file: Path,
        fingerprints: dict[str, str],
        fingerprints_file: Path,
    ) -> None:
        self.backend_name = backend_name
        self._sessions = sessions
        self._sessions_file = sessions_file
        self._fingerprints = fingerprints
        self._fingerprints_file = fingerprints_file

    def get_session_id(self, save_id: str) -> str | None:
        key = _life_session_key(self.backend_name, save_id)
        return self._sessions.get(key)

    def record_session_id(self, save_id: str, conversation_id: str) -> None:
        key = _life_session_key(self.backend_name, save_id)
        self._sessions[key] = conversation_id
        self._save_sessions()

    def rotate_if_needed(
        self,
        save_id: str,
        profile_revision: int,
        memory_revision: int,
    ) -> str | None:
        """Return current conversation_id to resume, or None if a fresh session is needed."""
        fp_key = f"life:{self.backend_name}:{save_id}"
        current_fp = _life_fingerprint(profile_revision, memory_revision)
        recorded_fp = self._fingerprints.get(fp_key)

        current_cid = self.get_session_id(save_id)
        if current_cid is not None and recorded_fp != current_fp:
            # Rotation needed — drop the old session so deleted agreements can
            # never keep flowing through the previous context (contract §4).
            session_key = _life_session_key(self.backend_name, save_id)
            self._sessions.pop(session_key, None)
            self._save_sessions()
            logger.info(
                "Life session rotated for %s (profile/memory changed): old=%s",
                save_id,
                current_cid,
            )
            current_cid = None

        self._fingerprints[fp_key] = current_fp
        self._save_fingerprints()
        return current_cid

    def record_fingerprint(
        self, save_id: str, profile_revision: int, memory_revision: int
    ) -> None:
        fp_key = f"life:{self.backend_name}:{save_id}"
        self._fingerprints[fp_key] = _life_fingerprint(profile_revision, memory_revision)
        self._save_fingerprints()

    # ---------------------------------------------------------------- prompts
    @staticmethod
    def build_system_prompt(
        profile: dict[str, Any] | None,
        memory_render: dict[str, Any] | None,
        work_summary: dict[str, Any] | None,
        mode: str = "chat",
    ) -> str:
        """Build the system prompt for a life chat turn."""
        parts: list[str] = []

        if profile:
            personality = profile.get("personality", "gentle")
            name = profile.get("companionName", "阿星")
            play_style = profile.get("playStyle", "earn")
            style_summary = _PLAY_STYLE_SUMMARIES.get(play_style, play_style)
            personality_desc = _PERSONALITY_PROMPTS.get(personality, _PERSONALITY_PROMPTS["gentle"])
            parts.append(
                f"你是农场伙伴{name}。{personality_desc}\n"
                f"玩家的游戏风格：{style_summary}。"
            )
        else:
            parts.append("你是农场伙伴阿星，一个友善的星露谷伙伴。")

        parts.append("你可以和玩家轻松闲聊，分享农场生活，也可以帮助玩家商量计划（仅建议，不实际派发任务）。")
        parts.append(
            "这是只读生活对话：你不能派工、取消或暂停工作，也不能把任务加入队列。"
            "即使玩家在闲聊中说了‘去浇水’或‘取消现在的工作’，也绝不能说任务已执行、已取消、已安排或稍后会自动执行。"
            "需要实际操作时，请明确告诉玩家从‘帮我做件事’入口提出；此处只可讨论建议。"
            "此处也不能保存、纠正或删除记忆；需要记约定时请指向‘查看记忆’，不要声称会替玩家记下。"
        )
        parts.append("始终用自然中文回复玩家。不要显示 JSON、token 或会话 ID 等技术信息。")

        if memory_render:
            agreements = memory_render.get("agreements", [])
            preferences = memory_render.get("preferences", [])
            events = memory_render.get("recentEvents", [])
            if agreements:
                ag_texts = "；".join(a.get("text", "") for a in agreements)
                parts.append(f"玩家的约定：{ag_texts}。")
            if preferences:
                pref_texts = "；".join(p.get("text", "") for p in preferences)
                parts.append(f"玩家的偏好：{pref_texts}。")
            if events:
                ev_texts = "；".join(f"[{e.get('gameDate', '?')}]{e.get('text', '')}" for e in events)
                parts.append(f"近期共同经历：{ev_texts}。")

        if mode == "plan" and work_summary:
            parts.append("玩家正在和你商量计划。以下是当前农场工作状态（仅供参考，不要代为派发）：")
            parts.append(json.dumps(work_summary, ensure_ascii=False, separators=(",", ":")))
            parts.append("请根据实际状态给出建议，不要创造不存在的工具或任务。")
        elif work_summary:
            parts.append("当前工作状态仅供聊天时核对事实，不代表你可以改变它：")
            parts.append(json.dumps(work_summary, ensure_ascii=False, separators=(",", ":")))
            parts.append("没有显示的状态就是未知，不要擅称目标、待办或工作已清空。")

        return "\n".join(parts)

    @staticmethod
    def build_care_prompt(
        profile: dict[str, Any] | None,
        memory_render: dict[str, Any] | None,
        kind: str,
        game_date: str,
        weather: Any,
        ref_event: str | None,
    ) -> str:
        """Build a prompt asking the model to generate a care message."""
        name = profile.get("companionName", "阿星") if profile else "阿星"
        personality = profile.get("personality", "gentle") if profile else "gentle"
        personality_desc = _PERSONALITY_PROMPTS.get(personality, _PERSONALITY_PROMPTS["gentle"])

        parts = [
            f"你是农场伙伴{name}。{personality_desc}",
            "请根据以下情境生成一条简短的主动关怀消息（不超过100字），让玩家感到温暖。",
            f"当前游戏日期：{game_date}，时段：{'早晨' if kind == 'morning' else '傍晚' if kind == 'evening' else '工作完成后'}。",
        ]

        if weather and weather != "unknown":
            if isinstance(weather, dict):
                icon = weather.get("icon")
                is_raining = weather.get("isRaining")
                if icon:
                    parts.append(f"天气图标：{icon}。")
                elif is_raining:
                    parts.append("今天在下雨。")
            else:
                parts.append(f"天气：{weather}。")

        if ref_event:
            parts.append(f"刚刚完成的事情：{ref_event}。")

        if memory_render:
            agreements = memory_render.get("agreements", [])
            if agreements:
                ag_texts = "；".join(a.get("text", "") for a in agreements[:3])
                parts.append(f"玩家的约定：{ag_texts}。")

        parts.append("直接输出关怀消息正文，不要加引号或前缀说明。")
        return "\n".join(parts)

    # ---------------------------------------------------------------- persistence
    def _save_sessions(self) -> None:
        try:
            self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
            self._sessions_file.write_text(
                json.dumps(self._sessions, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as ex:
            logger.warning("Failed to save life chat sessions: %s", ex)

    def _save_fingerprints(self) -> None:
        try:
            self._fingerprints_file.parent.mkdir(parents=True, exist_ok=True)
            self._fingerprints_file.write_text(
                json.dumps(self._fingerprints, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as ex:
            logger.warning("Failed to save life chat fingerprints: %s", ex)


def make_life_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build env vars for a life chat backend turn.

    Injects STARDEW_MCP_SURFACE=life.
    Explicitly removes STARDEW_DECISION_TOKEN if present.
    """
    env = dict(base_env or os.environ)
    env["STARDEW_MCP_SURFACE"] = "life"
    env.pop("STARDEW_DECISION_TOKEN", None)
    return env
