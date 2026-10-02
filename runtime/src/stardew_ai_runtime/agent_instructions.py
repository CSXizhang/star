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


def _format_instructions(core: str, index: str) -> str:
    return _tool_links(core + "\n\n" + index) + (
        "\n\n工具入口：仅用 stardew-companion 操作游戏。"
        "领域指导和执行方法通过 read_guidance(topic) 读取；工作模式可在 knowledge 能力组发现并调用，"
        "生活对话直接调用只读 read_guidance。首次行动需要执行细节时读 execution。"
        "参数用 discover_capabilities(group) 获取，再用 call_capability(tool,params) 调用；"
        "submit_plan 选择一项短作业，get_status 获取必要的当前状态。"
        "若工具经 exec 宿主提供，首次只按精确名称 "
        "mcp__stardew_companion__discover_capabilities、mcp__stardew_companion__call_capability、"
        "mcp__stardew_companion__submit_plan、mcp__stardew_companion__get_status 查入口；"
        "入口发现单独完成，读到真实方法后在下一次 exec 调用；未发现的方法不能直接调用。"
        "不要广搜或输出全部 ALL_TOOLS。工具回包取 structuredContent，无此字段则读 content。"
        "不要读取本地文件或调用终端。直接对话用manage_milestones接下明确任务、manage_companion处理暂停/继续/取消/作息；征求意见仅讨论，选择偏好不是派活。动作交给执行器，当前对话不接管原生连接。"
    )


def read_guidance(topic: str) -> dict[str, str]:
    if topic not in GUIDANCE_TOPICS:
        raise ValueError("Unknown guidance topic; choose " + ", ".join(GUIDANCE_TOPICS))
    title, path = GUIDANCE_TOPICS[topic]
    return {"topic": topic, "title": title,
            "text": _tool_links((ROOT / path).read_text(encoding="utf-8-sig").strip())}


@dataclass(frozen=True)
class InstructionBundle:
    text: str
    revision: str


def load_instruction_bundle() -> InstructionBundle:
    """Use the same file bytes for a turn's instructions and session version."""
    files = {path: (ROOT / path).read_bytes() for path in INSTRUCTION_FILES}
    def decode(data: bytes) -> str:
        return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
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
