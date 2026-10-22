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
    "\n当前为玩家模式：你控制存档中的玩家角色，按玩家委托经营与探索。"
    "结合当天状态安排工作、休息和原生睡眠。"
)


def _format_instructions(core: str, index: str) -> str:
    return _tool_links(core + "\n\n" + index)


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
    text = _format_instructions(decode(files[CORE_PATH]), decode(files[INDEX_PATH]))
    if actor_mode == "player":
        text += PLAYER_MODE_SECTION
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
