import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from stardew_ai_runtime.codex_game_profile import (
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



def test_tool_profile_keeps_harness_capabilities_and_existing_authority():
    args = game_tool_config()
    assert "features.code_mode_host=true" in args
    assert not any(arg.startswith("features.") and arg.endswith("=false") for arg in args)
    assert "mcp_optional_startup_grace_ms=0" in args
    assert not any("approval_policy" in arg or "sandbox_mode" in arg for arg in args)
