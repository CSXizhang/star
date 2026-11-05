"""Invocation-only Codex tool configuration; never alter user config or auth."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

CODEX_GAME_TOOL_PROFILE_VERSION = "stardew-game-tools-v5"

def game_tool_config() -> list[str]:
    """Add the game transport without disabling the harness's native tools."""
    return ["-c", "features.code_mode_host=true", "-c", "mcp_optional_startup_grace_ms=0"]


def write_game_instructions(run_dir: Path, instructions: str) -> Path:
    """Atomically replace the run-local instruction file for new and resume."""
    target = run_dir / "config" / "agent-system-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agent-instructions-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(instructions)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
