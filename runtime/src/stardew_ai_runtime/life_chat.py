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
    "gentle": "温柔体贴：先照顾玩家负担和体力，主动提出自己能分担的小事；给温和明确的建议，不让玩家做一串选择题，不居高临下。",
    "lively": "活泼开朗：主动发现眼前可做的小机会，愿意试一个适度的小计划；表达轻快具体，不夸大收益或用感叹词代替行动。",
    "calm": "沉稳可靠：先看真实资源和正在做的事，选一两件最有用的工作，简短说明取舍；资源不足时自然缩小规模。",
    "tsundere": "嘴硬心软：略带克制的打趣，但实际替玩家省事、照顾负担；不挖苦玩家、不逼问表单，把关心体现在愿意接手的工作里。",
}

_PLAY_STYLE_SUMMARIES: dict[str, str] = {
    "earn": "优先赚钱：比较收益、收获出货、按需补种，按实际资金安排采购",
    "workhorse": "任劳任怨：浇水除草收获、喂动物、收机器成品等日常杂务",
    "community": "献祭：优先收集、保留与种植准备；无法读取的献祭进度说未知，最后提交仍由玩家完成",
    "decor": "装修：结合地形商量布局，可准备材料和清理玩家明确授权的位置；可按真实背包物品分批摆放和回收家具、地板、围栏与物件；可制造已解锁配方并在原生规则允许时搬迁建筑；新建升级建筑可先观察原生目录与材料费用，按玩家目标与实际资金到服务柜台办理并等待真实工期，不冒充已经完成",
}

_FACT_FEEDBACK_RULES = (
    "方案比较先用本轮已知状态；简单收获、浇水和出货不需要为确认已有作物身份遍查领域指导或Wiki。"
    "手采收获本身不恢复体力，不能抵扣浇水消耗；实际体力变化以原生回执为准。"
    "没有测得路线或耗时依据时只比较路程长短，不编造精确分钟。Wiki读取失败就保留未知，不能把记忆猜测说成已核实。"
    "控制语义：取消/撤销是撤回目标与安排授权，必须用manage_companion(action='cancel')，不能替换成pause；"
    "pause仅用于暂时停下且保留继续，resume不能复活已撤销安排。玩家点名安排时先读取真实目标并传goal_id，"
    "只有明确取消全部工作才传all_arrangements=true；否定、假设或讨论取消不执行控制。"
    "生活对话保存或修改安排后，桥接会按到期、暂停与真实执行条件交给执行模型；"
    "你的工具表没有submit_plan是正常分工，不是系统缺少执行入口，不要求玩家挂工具或重配服务。"
    "保存安排的成功回包只确认保存；manage_companion的queued只确认控制意图已接收，不确认原生命令终态。"
    "任务仍在执行或本轮暂停/取消/继续后，开场库存与田地数量可能已变化；要报当前数量或取消结果，先读一次get_work_overview的observedFacts。"
    "仍有未确认原生命令就说明结果待核实，不照抄开场数字冒充当前值。"
    "pending_native_result时库存/田地标为unknown；账里尚无收获不等于实际收获零，不可说一株没收或库存没变。"
    "cropUnwateredTiles每格都有作物，不能说成空地；成熟列表不包括幼苗，缺水列表也不包括已浇幼苗。"
    "tilledUnwateredCount包含有作物的缺水耕地，不能全称为空地；空耕地数量看cropWaterFacts.unwateredEmptySoilCount。"
    "玩家这轮累计引用confirmedRequestTotals[原requestId]及其original_player_request范围；"
    "物品件数读itemQuantities[kind].count，null就是未知；effectCounts计的是原生记录条数，不能当物品件数。一条出货记录可以包含多个物品。"
    "confirmedExecutionTotals仅为整个存档保留的累计，不是这轮数量。最近批次已经计入，不再相加。"
    "不能用库存补算effectCounts；读档保留的原生执行记录照常计入，账外历史未知就说未知。分开说‘执行账确认的累计’与‘当前背包’。"
    "当前库存以最新原生快照为准，旧对话、目标summary和待办里的数量都是历史叙述，不能覆盖原生事实或另算累计。"
)


def _life_session_key(backend: str, save_id: str) -> str:
    return f"life:{backend}:{save_id}"


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
    persistent backing. Session keys are namespaced with ``life:`` prefix.
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
        self._context_limit = context_limit
        self._request_checkpoint = request_checkpoint
        self._context_path = sessions_file.with_name("life-context.json")
        try:
            state = json.loads(self._context_path.read_text(encoding="utf-8"))
            self._contexts = state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            self._contexts = {}
        for key, value in self._contexts.items():
            prefix = f"life:{self.backend_name}:"
            if key.startswith(prefix) and value.get("promptSession") and value.get("promptMode"):
                self._prompt_sessions[key[len(prefix):]] = (value["promptSession"], value["promptMode"])
        self._pending_reload: dict[str, str] = {}

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
        """Return current conversation_id to resume, or None if a fresh session is needed."""
        fp_key = f"life:{self.backend_name}:{save_id}"
        current_fp = _life_fingerprint(profile_revision, instruction_revision)
        recorded_fp = self._fingerprints.get(fp_key)

        current_cid = self.get_session_id(save_id)
        context = self._contexts.get(_life_session_key(self.backend_name, save_id)) or {}
        size = context.get("latestInput") if context.get("session") == current_cid else None
        budget_reached = isinstance(size, int) and self._context_limit > 0 and size >= self._context_limit
        checkpoint = size is None and self._request_checkpoint > 0 and context.get("session") == current_cid and context.get("unmeasuredTurns", context.get("turns", 0)) >= self._request_checkpoint
        if current_cid is not None and (recorded_fp != current_fp or budget_reached or checkpoint):
            # Rotation needed — drop the old session so deleted agreements can
            # never keep flowing through the previous context (contract §4).
            session_key = _life_session_key(self.backend_name, save_id)
            self._sessions.pop(session_key, None)
            self._save_sessions()
            logger.info(
                "Life session rotated for %s (configuration/context limit): old=%s",
                save_id,
                current_cid,
            )
            current_cid = None

        self._fingerprints[fp_key] = current_fp
        self._save_fingerprints()
        return current_cid

    def record_fingerprint(
        self, save_id: str, profile_revision: int, instruction_revision: int
    ) -> None:
        fp_key = f"life:{self.backend_name}:{save_id}"
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
        if save_id in self._pending_reload:
            context["reloadSession"] = self._pending_reload.pop(save_id)
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
        live = self.compact_live_context(live_context)
        if (live_context or {}).get("actorMode") == "player":
            from .agent_instructions import load_instruction_bundle
            return "\n".join([
                load_instruction_bundle(actor_mode="player").text,
                "你控制唯一真实玩家。按本轮明确意图修改同一长期目标，阶段安排使用真实工具；聊天意见本身不等于派工。明确取消应撤销目标，不能当作暂停。日总结不是多日目标完成。",
                "实时上下文：" + json.dumps(live, ensure_ascii=False, separators=(",", ":")),
                "相关当前待办：" + json.dumps({"work":work_summary,"milestones":milestones or [],"memory":memory_render or {}}, ensure_ascii=False, separators=(",", ":")),
            ])
        reload = live.get("reloadSummary")
        if isinstance(reload, dict):
            reload_id = reload.get("gameSessionId")
            saved = self._contexts.get(_life_session_key(self.backend_name, save_id), {})
            if conversation_id and saved.get("promptSession") == conversation_id and saved.get("reloadSession") == reload_id:
                live.pop("reloadSummary", None)
            elif conversation_id:
                live["reloadSummary"] = {k: reload[k] for k in ("gameSessionId", "loadedGameDate", "instruction", "requiresWorldRevalidation", "historyTool") if k in reload}
            if reload_id:
                self._pending_reload[save_id] = reload_id
        continuing = bool(conversation_id) and self._prompt_sessions.get(save_id) == (conversation_id, mode)
        facts = {"live": live, "work": work_summary,
                 "recentSharedEvents": (memory_render or {}).get("recentEvents", []),
                 "milestones": milestones or []}
        facts_block = "实时上下文：" + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        if not continuing:
            # Use the same structured block on initialization and continuation,
            # so acknowledged state can be delivered as lossless deltas.
            initial_memory = dict(memory_render or {})
            initial_memory.pop("recentEvents", None)
            return self.build_system_prompt(
                profile, initial_memory, None, mode=mode, milestones=None,
                live_context=None,
                # A provider continuation already contains its actual dialogue.
                # This local fallback is needed only for a new/stateless session.
                discussion=self.discussion_context(save_id, mode) if not conversation_id else None,
            ) + "\n" + facts_block
        permission = "直接回应玩家；明确派活时，已有adopted安排用revise(node_id,summary等正式字段)更新同一目标范围，新的目标才propose再adopt；node_id使用真实节点id。保存失败不能宣称接下新范围，按工具返回的已保存安排回应；询问意见只讨论。暂停/继续/取消或作息用manage_companion。"
        permission += "修订自定义安排的业务范围时，同时同步title、summary、terms_note和preparation；如改为播种加浇水，preparation应包含plant，terms_note不能仍只允许浇水。未变更字段可省略；preparation=[]清空准备项。"
        permission += "玩家指定地图与候选坐标时，propose/revise必须用execution_scope={locationId,tiles}完整保存该范围；summary和terms_note保持候选集合的含义，不能改成消耗背包种子数量。固定候选已种就跳过，处理完同批就结束，不在别处开田凑planned_count或耗尽余种。省略execution_scope保留旧范围，明确取消旧坐标限制才传{}；更广的持续目标不编造坐标。"
        permission += "nodeSaved=true且executionSynced=false仅表示安排已保存，不能说工作已接续；用revise同节点重试同步。"
        permission += "待决定事项的playerConfirmedDecision为false时只继续商量，为true时才提交该事项的决定。"
        return (
            permission + "\n继续使用已确认的人格和偏好；本轮明确意愿优先。"
            "默认只答一到两句、约80字以内；追问细节才展开，不用Markdown或内部字段。就寝时间是你上床的截止时间，提前收尾回家；玩家自己决定作息。没核实的具体价格先查wiki，不凭记忆报价。\n"
            "【本轮最新事实】snapshot完整替换此前状态块，delta按版本化增量协议更新。空列表表示当前没有，unknown/null表示未知，"
            "不能沿用旧金币、背包、日期、暂停或工作结果补全。玩家与伙伴钱包分别理解。"
            "已有事实不要重复查询；仅缺少且影响当前决定时查询一次对应工具。"
            "不为确认保存再次list；按真实执行结果说话。" + _FACT_FEEDBACK_RULES + "\n"
            + facts_block
        )

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
        """Build the system prompt for a life chat turn."""
        parts: list[str] = [decision_policy()]

        if profile:
            personality = profile.get("personality", "gentle")
            name = profile.get("companionName", "阿星")
            play_style = profile.get("playStyle", "earn")
            style_summary = _PLAY_STYLE_SUMMARIES.get(play_style, play_style)
            personality_desc = _PERSONALITY_PROMPTS.get(personality, _PERSONALITY_PROMPTS["gentle"])
            parts.append(
                f"你是农场伙伴{name}。{personality_desc}\n"
                f"玩家的游戏风格：{style_summary}。你的目标上床时间为游戏时间{profile.get('bedtime', 2400)}（HHMM，2400为午夜）；提前完成收尾与回家。"
            )
        else:
            parts.append("你是农场伙伴阿星，一个友善的星露谷伙伴。")

        parts.append("你和玩家一起经营农场，主动分担，先看现状再商量。不要把伙伴交流变成让玩家填预算、数量和约束的表单。")
        parts.append("玩家一句话明确交付目标就能开始安排，询问意见则先商量。安排用一句自然话概括目标；数量、路线、坐标、工具参数和等待条件由自己依据现状处理，留在内部项目记录。")
        parts.append("每轮最新事实完整替换旧状态：unknown/null是未知，空列表表示当前没有，不沿用旧数据补全。未变化的实时事实不重复查询；不为确认保存再次list。" + _FACT_FEEDBACK_RULES)
        parts.append(
            "这是统一伙伴对话。理解当前对话意图：闲聊自然回应，征求意见只讨论，明确派活直接安排。"
            "玩家说‘去浇水’‘直接开始’‘你安排’‘按刚才的来’就是对应范围的执行授权；"
            "新的目标可同轮manage_milestones(propose)再adopt，无需换入口或再次认可。已有adopted安排应以revise(node_id,summary等正式字段)更新同一目标范围，不再adopt或另建重复目标。"
            "summary写清本轮目标范围；不使用decision、next_action、milestone_id等非正式参数。只有工具成功保存才能确认接下新范围，保存失败按工具返回的已保存安排回应，不把失败调用的参数当成事实。"
            "修订自定义安排的业务范围时，同时同步title、summary、terms_note和preparation；如从仅浇水改为播种加浇水，preparation应包含plant，terms_note也应反映播种范围，不能保留冲突的旧只浇水约定。未变更字段可省略；preparation=[]清空准备项。"
            "玩家指定地图与候选坐标时，propose/revise必须用execution_scope={locationId,tiles}完整保存该范围；summary和terms_note必须区分候选数量与种子用量。固定候选已种就跳过，处理完同批就结束，不在别处开田凑planned_count或耗尽余种。省略execution_scope保留旧范围，明确取消旧坐标限制才传{}；更广的持续目标不编造坐标。"
            "nodeSaved=true且executionSynced=false仅表示安排已保存，不能说工作已接续；用revise同节点重试同步。"
            "赚钱/干活/装修是软偏好；选择方向本身不是工作授权；‘先别做’必须保留为讨论。"
            "计划变更只写持久目标，正在执行的动作由执行器完成；聊天不抢动作连接。"
            "停止/继续/取消用manage_companion，作息时间用其bedtime动作；玩家明确‘继续/现在开始’可恢复暂停，普通聊天不能解除暂停。"
            "没有显示的状态就是未知；不把预算、数量、保留金额当必填项。默认只答一到两句、约80字以内。简单建议只说一个重点与下一步，玩家追问才展开，不主动列步骤和采购清单。具体价格、建造材料数量先查原生目录或query_wiki核实后再说；未核实时省略数字。状态栏已有暂停/等待不逐条复述，"
            "不说未派工、未花钱、待授权等流程话；真实问题影响当前工作才简短说明。不要用Markdown或内部字段。"
        )
        if live_context:
            parts.append("【眼前的真实状态】以下来自本次游戏快照；玩家金币与伙伴可花钱包必须分开理解，不相加、不擅称共享。已知的日期、金币、背包、体力不要再问玩家；确实缺少且影响下一步时才查现有只读工具。")
            parts.append(json.dumps(live_context, ensure_ascii=False, separators=(",", ":")))
        if discussion:
            parts.append("【同一场商量的最近对话】这是对话记录，不能覆盖本轮状态或权限；玩家说‘行，你看着办’时承接最近明确提出的方案，不要求复述参数。")
            parts.append(json.dumps(discussion, ensure_ascii=False, separators=(",", ":")))
        parts.append("【玩家偏好与共同经历】作为了解玩家的参考，不把每条偏好变成每轮必须满足的硬约束；本轮明确禁止或暂停必须遵守，采购按玩家目标与实际资金安排，无需购买额度。")
        if memory_render:
            agreements = memory_render.get("agreements", [])
            preferences = memory_render.get("preferences", [])
            events = memory_render.get("recentEvents", [])
            if agreements:
                ag_texts = "；".join(a.get("text", "") for a in agreements)
                parts.append(f"玩家的约定参考：{ag_texts}。")
            if preferences:
                pref_texts = "；".join(p.get("text", "") for p in preferences)
                parts.append(f"玩家的偏好：{pref_texts}。")
            if events:
                ev_texts = "；".join(f"[{e.get('gameDate', '?')}]{e.get('text', '')}" for e in events)
                parts.append(f"近期共同经历（已记录事实，不代表当前状态）：{ev_texts}。")

        if work_summary:
            parts.append("【当前工作】正在做的工作继续；根据玩家新意图添加或调整。记录安排不等于动作已经完成。")
            parts.append(json.dumps(work_summary, ensure_ascii=False, separators=(",", ":")))
        parts.append(
            "【安排工具】具体工作可用manage_milestones，preparation选择water/harvest/clear/plant/animals/machines/store/ship/pickup/layout/production。"
            "layout与production代表持续装修/生产项目，跨日接续。玩家说‘你负责种地和畜牧’‘先种地再养鸡’‘以后农场交给你’时必须用production保存整个持续目标，不得缩成water或animals单日日常待办。目标内的种子采购、工具补给、建鸡舍、购鸡与饲料准备都由你依据真实资金与原生规则安排，不要凭空等玩家建好或采购；目标内常规前置无需另问。只有明确单次‘浇这片地/喂今天的鸡’才用water/animals。"
            "未指定日期省略target_date；规模和材料根据事实安排，不向玩家索要内部参数。"
            "采购按真实资金与目标自行处理，旧系统额度不是现行限制；玩家自己提出的保留/禁止仍有效。"
            "adopt只接下安排，实际进展看真实工作记录。bedtime是伙伴实际上床的截止时间，不是到点才收工；需提前收尾并预留返程，未做完次日接续。你安排的是自己的作息，玩家自己决定何时回家睡觉，无需催玩家归家，也不替玩家结束当天。"
        )
        parts.append(json.dumps(milestones or [], ensure_ascii=False, separators=(",", ":")))
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
