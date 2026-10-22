"""Unified companion conversation and persistent provider sessions.

Both legacy wire modes share conversation semantics. The model reads cached
facts, records explicitly accepted objectives, and submits control intents;
only the bridge/worker owns native execution. Mere discussion changes no work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from .agent_instructions import instructions_revision
from .codex_game_profile import CODEX_GAME_TOOL_PROFILE_VERSION
from .decision_policy import decision_policy

logger = logging.getLogger("stardew_ai_runtime.life_chat")

_PERSONALITY_PROMPTS: dict[str, str] = {
    "gentle": "温柔体贴",
    "lively": "活泼开朗",
    "calm": "沉稳可靠",
    "tsundere": "嘴硬心软",
}

_PLAY_STYLE_SUMMARIES: dict[str, str] = {
    "earn": "赚钱：收获、出货、采购与种植经营",
    "workhorse": "日常农活：作物照料、清理、动物照料与机器收取",
    "community": "社区中心：收集、种植和保留献祭所需物品",
    "decor": "装修农场：布局、清理、制作、物件摆放与建筑安排",
}

def _life_session_key(backend: str, save_id: str) -> str:
    # Dialogue, preparation, results and autonomous work belong to one agent.
    return f"{backend}:{save_id}"


def _life_fingerprint(profile_revision: int, instruction_revision: int) -> str:
    material = json.dumps(
        {"profileRevision": profile_revision, "memoryInstructionRevision": instruction_revision,
         "instructionsRevision": instructions_revision(), "gameToolProfile": CODEX_GAME_TOOL_PROFILE_VERSION},
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class LifeChatService:
    """Manages life chat sessions using the same session store as ChatBridge.

    The bridge passes its ``sessions`` dict by reference so both share the same
    persistent backing and provider/save key, including work and result turns.
    """

    def __init__(
        self,
        backend_name: str,
        sessions: dict[str, str],
        sessions_file: Path,
        fingerprints: dict[str, str],
        fingerprints_file: Path,
        context_limit: int = 100000,
        request_checkpoint: int = 20,
    ) -> None:
        self.backend_name = backend_name
        self._sessions = sessions
        self._sessions_file = sessions_file
        self._fingerprints = fingerprints
        self._fingerprints_file = fingerprints_file
        # Successful initialization is persisted with the exact provider session.
        self._prompt_sessions: dict[str, tuple[str, str]] = {}
        # Legacy budget arguments are accepted but no longer constrain the harness.
        self._context_path = sessions_file.with_name("life-context.json")
        try:
            state = json.loads(self._context_path.read_text(encoding="utf-8"))
            self._contexts = state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            self._contexts = {}
        for key, value in list(self._contexts.items()):
            if key.startswith("life:"):
                self._contexts.setdefault(key[5:], value)
                key = key[5:]
            prefix = f"{self.backend_name}:"
            if key.startswith(prefix) and value.get("promptSession") and value.get("promptMode"):
                self._prompt_sessions[key[len(prefix):]] = (value["promptSession"], value["promptMode"])

    def record_context(self, save_id: str, conversation_id: str, input_context: int | None) -> None:
        key = _life_session_key(self.backend_name, save_id)
        previous = self._contexts.get(key) or {}
        previous = previous if previous.get("session") == conversation_id else {}
        turns = previous.get("turns", 0)
        # Older persisted contexts only counted turns; preserve that fallback
        # until a measured turn establishes a fresh unknown-context streak.
        unmeasured_turns = previous.get("unmeasuredTurns", turns) + 1 if input_context is None else 0
        self._contexts[key] = {**previous, "session": conversation_id,
                               "latestInput": input_context, "turns": turns + 1,
                               "unmeasuredTurns": unmeasured_turns}
        self._context_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._context_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._contexts), encoding="utf-8")
        temporary.replace(self._context_path)

    def get_session_id(self, save_id: str) -> str | None:
        key = _life_session_key(self.backend_name, save_id)
        if key not in self._sessions:
            legacy = self._sessions.get(f"life:{key}")
            if legacy:
                self._sessions[key] = legacy
                self._save_sessions()
        return self._sessions.get(key)

    def record_session_id(self, save_id: str, conversation_id: str) -> None:
        key = _life_session_key(self.backend_name, save_id)
        self._sessions[key] = conversation_id
        self._save_sessions()

    def rotate_if_needed(
        self,
        save_id: str,
        profile_revision: int,
        instruction_revision: int,
    ) -> str | None:
        """Resume the shared session; context maintenance belongs to the harness.

        Kept as a compatibility entry point. Preferences and memory are current
        environment data, not a reason to discard the conversation.
        """
        self.record_fingerprint(save_id, profile_revision, instruction_revision)
        return self.get_session_id(save_id)

    def record_fingerprint(
        self, save_id: str, profile_revision: int, instruction_revision: int
    ) -> None:
        fp_key = _life_session_key(self.backend_name, save_id)
        self._fingerprints[fp_key] = _life_fingerprint(profile_revision, instruction_revision)
        self._save_fingerprints()

    def discussion_context(self, save_id: str, mode: str) -> list[dict[str, str]]:
        path = self._sessions_file.with_name("life_discussions.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data.get(f"{save_id}:{mode}", {})
            fingerprint = self._fingerprints.get(_life_session_key(self.backend_name, save_id))
            if entry.get("fingerprint") != fingerprint:
                return []
            return entry.get("turns", [])[-6:]
        except (OSError, ValueError, TypeError, AttributeError):
            return []

    def record_discussion(self, save_id: str, mode: str, player: str, reply: str) -> None:
        path = self._sessions_file.with_name("life_discussions.json")
        turns = self.discussion_context(save_id, mode)
        turns.extend([{"speaker": "player", "text": player[:2000]},
                      {"speaker": "companion", "text": reply[:2000]}])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[f"{save_id}:{mode}"] = {
            "fingerprint": self._fingerprints.get(_life_session_key(self.backend_name, save_id)),
            "turns": turns[-6:],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    # ---------------------------------------------------------------- prompts
    def mark_prompt_delivered(self, save_id: str, conversation_id: str, mode: str) -> None:
        """Call only after a successful provider turn with a resumable session."""
        self._prompt_sessions[save_id] = (conversation_id, mode)
        key = _life_session_key(self.backend_name, save_id)
        context = self._contexts.setdefault(key, {})
        context.update(promptSession=conversation_id, promptMode=mode)
        self._context_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._context_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._contexts), encoding="utf-8")
        temporary.replace(self._context_path)

    @staticmethod
    def compact_live_context(context: dict[str, Any] | None) -> dict[str, Any]:
        """Remove duplicated projections, retaining fresh facts and unknowns."""
        live = dict(context or {})
        for key in ("companion", "agreements", "recentSharedEvents", "goals"):
            live.pop(key, None)  # profile/memory/work have dedicated blocks
        if isinstance(live.get("resources"), dict):
            for key in ("funds", "fundsStatus", "stamina", "inventory"):
                live.pop(key, None)  # same facts under resources, wallets kept separate
        return live

    def build_turn_prompt(
        self, save_id: str, conversation_id: str | None,
        profile: dict[str, Any] | None, memory_render: dict[str, Any] | None,
        work_summary: dict[str, Any] | None, *, mode: str,
        milestones: list[dict[str, Any]] | None, live_context: dict[str, Any] | None,
    ) -> str:
        # Mode is a legacy UI entry point, not a different agent or authority.
        continuing = bool(conversation_id) and (
            self._prompt_sessions.get(save_id, (None, None))[0] == conversation_id
        )
        facts = {
            "live": self.compact_live_context(live_context),
            "profile": self.profile_context(profile),
            "memory": memory_render or {},
            "work": work_summary,
            "milestones": milestones or [],
        }
        if not continuing:
            discussion = self.discussion_context(save_id, mode)
            if discussion:
                facts["recentDialogue"] = discussion
        from .agent_instructions import load_instruction_bundle
        # PromptDelivery sends changed guidance once per shared provider session.
        # Each state block is self-contained, including removed preferences.
        return (load_instruction_bundle((live_context or {}).get("actorMode")).text
                + "\n实时上下文：" + json.dumps(facts, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def profile_context(profile: dict[str, Any] | None) -> dict[str, Any]:
        result = dict(profile or {})
        result["personalityStyle"] = _PERSONALITY_PROMPTS.get(
            result.get("personality", "gentle"), _PERSONALITY_PROMPTS["gentle"])
        style = result.get("playStyle", "earn")
        result["playStyleDescription"] = _PLAY_STYLE_SUMMARIES.get(style, style)
        return result

    @staticmethod
    def build_system_prompt(
        profile: dict[str, Any] | None,
        memory_render: dict[str, Any] | None,
        work_summary: dict[str, Any] | None,
        mode: str = "chat",
        milestones: list[dict[str, Any]] | None = None,
        live_context: dict[str, Any] | None = None,
        discussion: list[dict[str, str]] | None = None,
    ) -> str:
        """Game guidance and current environment; no per-turn policy overlay."""
        return decision_policy() + "\n实时上下文：" + json.dumps({
            "live": LifeChatService.compact_live_context(live_context),
            "profile": LifeChatService.profile_context(profile),
            "memory": memory_render or {},
            "work": work_summary,
            "milestones": milestones or [],
            "recentDialogue": discussion or [],
        }, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def build_care_prompt(
        profile: dict[str, Any] | None,
        memory_render: dict[str, Any] | None,
        kind: str,
        game_date: str,
        weather: Any,
        ref_event: str | None,
    ) -> str:
        """Add the event; the shared turn context already carries persona and memory."""
        parts = [
            "结合当前情境和玩家对你的设定，主动与玩家交流。",
            f"当前游戏日期：{game_date}，时段：{'早晨' if kind == 'morning' else '傍晚' if kind == 'evening' else '计划提醒' if kind == 'milestone' else '工作完成后'}。",
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
            if kind == "milestone":
                parts.append(f"需要提醒玩家的计划节点：{ref_event}。")
            else:
                parts.append(f"刚刚完成的事情：{ref_event}。")

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
