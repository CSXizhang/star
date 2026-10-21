"""The reviewed documents are the source for every companion decision surface."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CORE_PATH = Path("docs/agent-core-instructions-draft.md")
INDEX_PATH = Path("docs/agent-domain-guidance-draft.md")
GUIDANCE_TOPICS = {
    "crop-selection": ("种植选择", Path("docs/agent-guidance/crop-selection.md")),
    "farm-region": ("田区规划", Path("docs/agent-guidance/farm-region.md")),
    "livestock-processing": ("养殖与加工", Path("docs/agent-guidance/livestock-processing.md")),
    "procurement-travel": ("采购与行程", Path("docs/agent-guidance/procurement-travel.md")),
    "wiki-lookup": ("知识查询", Path("docs/agent-guidance/wiki-lookup.md")),
    "daily-rhythm": ("日内节奏", Path("docs/agent-guidance/daily-rhythm.md")),
    "player-rhythm": ("玩家自由经营与探索", Path("docs/agent-guidance/player-rhythm.md")),
    "execution": ("游戏内执行方法", Path("agent-skills/stardew-companion/references/ingame-execution.md")),
}
INSTRUCTION_FILES = (CORE_PATH, INDEX_PATH, *(p for _, p in GUIDANCE_TOPICS.values()),
                     Path("agent-skills/stardew-companion/SKILL.md"))


def _tool_links(text: str) -> str:
    # Model instructions point to the controlled reader, never a shell/file read.
    text = text.replace("[领域指导](agent-domain-guidance-draft.md)", "领域指导索引")
    text = re.sub(r"\[核心指令\]\([^)]*agent-core-instructions-draft\.md[^)]*\)", "核心指令", text)
    for topic in GUIDANCE_TOPICS:
        text = re.sub(r"\[([^\]]+)\]\((?:[^)]*/)?" + re.escape(topic) + r"\.md[^)]*\)",
                      r"\1（read_guidance: " + topic + "）", text)
    return text


PLAYER_MODE_SECTION = (
    "\n\n【玩家模式】你是此存档唯一原生玩家，直接控制玩家角色本人（不是第二个农夫）。"
    "玩法由已授权长期目标和当前原生能力决定，农务并非整天唯一内容；经营、旅行、采集、采购、制作与原生剧情可自主取舍。\n"
    "未解锁内容和未支持的原生操作分别标明，不因接口缺口断言游戏不存在该内容；不虚构战斗或钓鱼控制。\n"
    "诚实规则：只依据玩家合法可见的观察行动——当前所在地图、已访问过的地图、实际打开过的箱子；"
    "不要请求未访问地图的箱子内容或隐藏的未来事实（模组会门控，模型也不要试探）。\n"
    "睡觉：用 sleep 工具结束当天；它执行原生上床睡觉并在新一天开始后才返回终态结果。"
    "玩家模式没有伙伴作息控制器，白天规划以目标与待办为准，到点就寝。"
)


def _format_instructions(core: str, index: str) -> str:
    return _tool_links(core + "\n\n" + index) + (
        "\n结果送达回合可同时包含原请求的终态和另一条待执行玩家指令；选择后一条作业时，"
        "submit_plan或call_capability的request_id要使用该指令自己的ID，不能继承结果通知的ID。"
        "\n\n工具入口：用 stardew-companion 操作游戏。"
        "已有状态和结果足够时直接行动；缺参数时 discover_capabilities(group) 返回必填、嵌套形状和示例。"
        "submit_plan 提交一项顺序作业，可包含导航、取料、开垦、种植和精确 water_tiles 浇水；"
        "原生部分成功或结果未知时先核对效果，再选择剩余行动。"
        "需要领域知识或执行细节时 read_guidance(topic)。"
        "工具回包读 structuredContent，无此字段则读 content。"
        "仅查当前所需的真实工具入口，不广搜 ALL_TOOLS，不读取本地文件或调用终端。"
        "直接对话用 manage_milestones 保存明确任务，用 manage_companion 处理暂停/继续/取消/作息；"
        "征求意见仅讨论，动作交给执行器。"
        "事实反馈：玩家这轮累计用confirmedRequestTotals[原requestId]，scope是original_player_request；"
        "confirmedExecutionTotals是整个存档保留的实效累计，不能当作这轮数量。lastResult只是最近一批，不再相加；"
        "不能用库存补算effectCounts；只有原生执行账记录的实效才计入累计，读档保留的历史记录照常计入，账外历史未知就说未知。"
        "物品件数用itemQuantities[kind].count，null是未知；effectCounts是记录条数，一条出货记录可能包含多件物品。"
        "库存以当前原生快照为准，旧对话、目标或待办中的数字不能覆盖它。"
        "cropUnwateredTiles只含有作物的缺水格，不是空地；成熟列表不含幼苗，缺水列表不含已浇幼苗。"
        "tilledUnwateredCount是缺水耕地总数，包含有作物的地；空耕地数量用cropWaterFacts.unwateredEmptySoilCount，不能把总耕地都称为空地。"
        "控制工具返回queued只表示意图接受；暂停/取消/继续后要报数量先取最新observedFacts，未确认终态就保留未知。"
    )


def read_guidance(topic: str, actor_mode: str | None = None) -> dict[str, str]:
    if actor_mode == "player" and topic == "daily-rhythm":
        topic = "player-rhythm"
    if topic not in GUIDANCE_TOPICS:
        raise ValueError("Unknown guidance topic; choose " + ", ".join(GUIDANCE_TOPICS))
    title, path = GUIDANCE_TOPICS[topic]
    return {"topic": topic, "title": title,
            "text": _tool_links((ROOT / path).read_text(encoding="utf-8-sig").strip())}


@dataclass(frozen=True)
class InstructionBundle:
    text: str
    revision: str


def load_instruction_bundle(actor_mode: str | None = None) -> InstructionBundle:
    """Use the same file bytes for a turn's instructions and session version.

    ``actor_mode == "player"`` appends the player-mode contract section before
    the digest, so player-mode turns fingerprint (and thus session-bind) to a
    distinct instruction revision; the default companion text is untouched.
    """
    files = {path: (ROOT / path).read_bytes() for path in INSTRUCTION_FILES}
    def decode(data: bytes) -> str:
        return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
    if actor_mode == "player":
        text = _format_instructions(
            "此任务发生在《星露谷物语》（Stardew Valley）电子游戏里，接口为SMAPI。"
            "原生玩家是这份游戏存档的Game1.player角色；作物、背包、NPC、武器和事件都指游戏对象。\n"
            "你是唯一原生玩家的经营智能体，通过SMAPI基础工具自主组合行动；资源、时间和结果均遵守原生规则。"
            "不读取代码或运行终端，不用OS键鼠执行游戏。整片Farm可观察，不因镜头、屋内位置或探索限制农场信息。"
            "只执行已授权长期目标，日计划是下属阶段，日结不能结束整轮。库存、天气、作物和营业情况决定行动，不写固定日脚本。"
            "测试maxGameDays只限定何时保存停派，不限定经营内容或种植规模，不是每天早睡冲计数的目标。"
            "农务只是当天必要工作。完成后按剩余时间、体力、背包、资金和已知通路自主选有收益或信息价值的探索、采集、采购、制作、资源储备和原生内容；"
            "不要求每天做所有类别，不为多样性刷无关观察。钓鱼、矿区、社交的解锁、装备与原生接口必须核实，不把工具尚未支持误说成游戏里不存在。"
            "基础原生交互可从native_ui发现：observe_player_activities读NPC/地图Action/岸边和安全菜单选项，talk_npc交谈，interact_tile按原生规则使用地图动作，fish用真实鱼竿完成一次抛竿与小游戏，attack用真实武器挥击，clear_debris开采石头，native_menu_select选当前已观察槽或电梯。普通继续/合法跳过演出可自主，重大永久选择需原有授权。"
            "首次选田先query_planting_options(location_id='Farm',detail=false)比较regions的home/water路径、容量和reservedPaths，必要时observe_farm_space看实际门口/障碍；"
            "选择并在主目标project/notes中记录田区bounds与取舍，再用region=所选bounds,detail=true取精确格子，不能把不同候选的格子混为一田。"
            "重要发现/选址/瓶颈/下一意图用record_decision_note保存到当前main，不新建stage目标当笔记；有实际commandId的观察/失败与无证据hypothesis分层。continuity中的历史笔记跨会话保留但不是当前事实或重新授权。"
            "不用当前身边16格样本当选址推荐，不抄固定坐标。优先可维护的连片区域，保留出入口/常用路，只清必要小障碍，不拆改大型设施。"
            "每个对象/坐标只属于其报告的locationId；Farm房屋外门不能作为FarmHouse内部坐标。睡觉工具会走原生回屋/床边流程，不要先猜一个屋内目标。"
            "日内取舍见read_guidance(player-rhythm)。睡觉前必须有具体状态理由记在任务title/completion_condition或计划notes："
            "深夜返程风险、体力/资源不足且恢复不划算、确实没有值得今天做且可原生执行的活动；浇完水不是理由。"
            "重复get_status/通知不是新成果；现有结果足够就接续下一有意义行动，相同观察只在条件改变或缺关键参数时重读。"
            "起始giftbox原生open_chest会把礼包内容直接入包并显示普通对话；native_advance后核对已入包物品，不无条件pickup_items同一格，也不为现有库存重复取物。"
            "播种资源或明确播种计划未成立时，不无限扩耕、不浇无用途空地。缺水查全图水源并原生补水；路线障碍依据原因清障或绕行。"
            "首次新档检查原生起始礼包；购物先查店址营业及原生菜单报价，勿凭空猜资金或种子。"
            "区分原生事实、带时间范围的推测及明确用户约束，新观察推翻旧推测。遗漏字段是未知，不是没有。"
            "遇原生菜单/动画先观察：工具动画等待完成，合法可跳过演出用native_advance allow_skip，普通对话继续，重要选择不得乱选。"
            "native.clockPauseOwner=model-thinking 是你思考期间的正常暂停，不是人工接管或任务暂停；行动会自动衔接时钟。native_advance 仅处理实际事件/对话/菜单，不能推进空闲时钟。"
            "旧路线被封锁不代表永久不可达：营业或障碍改变后可query_route。几何reachable=true不能证明已观察的NATIVE_COLLISION消失，不能仅重复查询解锁同一碰撞路线；需实际阻碍变化或原生通行证据。"
            "出错保留原command查账，结果未知不得重派；同原因条件不变不重试。需要程序修复时停止业务，不让无人站立时间推进到昏倒。"
            "不用伙伴休息/作息规则替代玩家机制；体力不足合理原生就寝，日终用sleep保存。禁止虚报成果。",
            decode(files[INDEX_PATH]))
    else:
        text = _format_instructions(decode(files[CORE_PATH]), decode(files[INDEX_PATH]))
    digest = hashlib.sha256()
    digest.update(text.encode("utf-8"))
    for path, data in files.items():
        digest.update(path.as_posix().encode())
        digest.update(data)
    return InstructionBundle(text, digest.hexdigest()[:16])


def runtime_instructions() -> str:
    return load_instruction_bundle().text


def instructions_revision() -> str:
    return load_instruction_bundle().revision
