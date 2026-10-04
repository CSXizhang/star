import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from stardew_ai_runtime.codex_game_profile import (
    CODEX_GAME_TOOL_PROFILE_VERSION,
    game_tool_config,
    unrelated_mcp_config,
    write_game_instructions,
)


def test_mcp_discovery_disables_other_servers_without_changing_game_or_auth(tmp_path: Path):
    servers = [{"name": "unrelated", "enabled": True},
               {"name": "already-disabled", "enabled": False},
               {"name": "stardew-companion", "transport": {"type": "stdio"}}]
    with patch("subprocess.run", return_value=CompletedProcess([], 0, json.dumps(servers), "")) as run:
        args = unrelated_mcp_config("custom-codex.exe", tmp_path)
    assert args == ["-c", "mcp_servers.unrelated.enabled=false",
                    "-c", "mcp_servers.already-disabled.enabled=false"]
    assert run.call_args.args[0][0] == "custom-codex.exe"
    assert run.call_args.args[0][-3:] == ["mcp", "list", "--json"]
    assert run.call_args.kwargs["cwd"] == str(tmp_path)
    assert "--ignore-user-config" not in run.call_args.args[0]
    assert "env" not in run.call_args.kwargs


@pytest.mark.parametrize("stdout", ["{}", "[{}]", "not json", '[{"name":"unsafe.name"}]'])
def test_mcp_discovery_rejects_invalid_or_ambiguous_config(tmp_path: Path, stdout: str):
    with patch("subprocess.run", return_value=CompletedProcess([], 0, stdout, "")):
        with pytest.raises(ValueError):
            unrelated_mcp_config("codex.exe", tmp_path)


def test_instruction_file_replaces_previous_text_without_temporary_files(tmp_path: Path):
    target = write_game_instructions(tmp_path, "old core")
    assert write_game_instructions(tmp_path, "new core\n中文") == target
    assert target.read_text(encoding="utf-8") == "new core\n中文"
    assert list(target.parent.iterdir()) == [target]


def test_failed_mcp_discovery_does_not_expose_private_config(tmp_path: Path):
    with patch("subprocess.run", return_value=CompletedProcess([], 1, "private config", "secret")):
        with pytest.raises(RuntimeError) as failure:
            unrelated_mcp_config("codex.exe", tmp_path)
    assert "private" not in str(failure.value) and "secret" not in str(failure.value)


def test_inherited_game_http_transport_fails_before_mixing_stdio_configuration(tmp_path: Path):
    stdout = json.dumps([{"name": "stardew-companion", "transport": {"type": "streamable_http"}}])
    with patch("subprocess.run", return_value=CompletedProcess([], 0, stdout, "")):
        with pytest.raises(ValueError, match="stdio"):
            unrelated_mcp_config("codex.exe", tmp_path)


def test_tool_profile_preserves_mcp_discovery_and_execution_authority():
    args = game_tool_config()
    assert "features.shell_tool=false" in args
    assert "features.unified_exec=false" in args
    assert "features.code_mode_host=true" in args
    assert "features.code_mode=false" not in args
    assert "features.code_mode_host=false" not in args
    assert CODEX_GAME_TOOL_PROFILE_VERSION == "stardew-game-tools-v2"
    assert "features.apps=false" in args
    assert "features.plugins=false" in args
    assert not any("approval_policy" in arg or "sandbox_mode" in arg for arg in args)
    assert not any("tool_search=" in arg or "apply_patch_freeform=" in arg for arg in args)
