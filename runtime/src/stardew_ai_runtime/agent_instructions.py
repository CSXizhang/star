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
        "\n\n工具入口：用 stardew-companion 操作游戏。"
        "已有状态和结果足够时直接行动；缺参数时 discover_capabilities(group) 返回必填、嵌套形状和示例。"
        "submit_plan 提交一项顺序作业，可包含导航、取料、开垦、种植和精确 water_tiles 浇水；"
        "原生部分成功或结果未知时先核对效果，再选择剩余行动。"
        "需要领域知识或执行细节时 read_guidance(topic)。"
        "工具回包读 structuredContent，无此字段则读 content。"
        "仅查当前所需的真实工具入口，不广搜 ALL_TOOLS，不读取本地文件或调用终端。"
        "直接对话用 manage_milestones 保存明确任务，用 manage_companion 处理暂停/继续/取消/作息；"
        "征求意见仅讨论，动作交给执行器。"
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
