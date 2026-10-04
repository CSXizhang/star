"""Invocation-only Codex tool configuration; never alter user config or auth."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

CODEX_GAME_TOOL_PROFILE_VERSION = "stardew-game-tools-v2"

# Supported by the local 0.159.2 CLI and the official configuration schema.
# Do not use removed apply_patch_freeform/tool_search switches: those do not
# remove tools. The CLI has no documented general native-tool allowlist.
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec",
    "apps", "plugins", "remote_plugin", "hooks", "skill_search",
    "skill_mcp_dependency_install", "multi_agent", "multi_agent_v2",
    "browser_use", "computer_use", "image_generation", "view_image",
    "artifact", "workspace_dependencies", "goals", "sleep_tool",
)


def game_tool_config() -> list[str]:
    """Keep execution authority unchanged while removing unrelated surfaces."""
    values = [f"features.{name}=false" for name in _DISABLED_FEATURES]
    values.extend([
        # This CLI exposes game MCP calls through functions.exec/ALL_TOOLS.
        # Its code-mode host is transport infrastructure, not a shell surface.
        # Disabling it leaves listed MCP tools unusable (verified in r18).
        "features.code_mode_host=true",
        "features.skip_host_skill_discovery=true",
        "skills.bundled.enabled=false",
        "skills.include_instructions=false",
        'web_search="disabled"',
        "tools.update_plan.enabled=false",
    ])
    return [part for value in values for part in ("-c", value)]


def unrelated_mcp_config(command: str, run_dir: Path) -> list[str]:
    """Discover merged config read-only, then disable every other direct MCP.

    Assigning ``mcp_servers={}`` merges with inherited tables in Codex; it does
    not clear them. Let the CLI resolve user/project/managed layers rather than
    attempting to parse only the user's file. Fail closed if discovery fails.
    No MCP server or model is started by ``mcp list``.
    """
    result = subprocess.run(
        [command, *game_tool_config(), "mcp", "list", "--json"],
        cwd=str(run_dir), stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=10, check=False,
    )
    if result.returncode != 0:
        # Config output may contain credentials; do not echo stdout/stderr.
        raise RuntimeError("Could not inspect inherited Codex MCP configuration")
    servers = json.loads(result.stdout)
    if not isinstance(servers, list):
        raise ValueError("Codex MCP configuration must be a server list")
    values = []
    for server in servers:
        if not isinstance(server, dict) or not isinstance(server.get("name"), str):
            raise ValueError("Codex MCP configuration contains an invalid server")
        name = server["name"]
        # CLI dotted-path overrides split on dots; quoted components are not
        # unquoted (verified with 0.159.2). Refuse ambiguous server identifiers.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Codex MCP server name cannot be overridden safely")
        if name != "stardew-companion":
            values.extend(["-c", f"mcp_servers.{name}.enabled=false"])
        elif (server.get("transport") or {}).get("type") not in {None, "stdio"}:
            raise ValueError("Inherited stardew-companion MCP must use stdio")
    return values


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
