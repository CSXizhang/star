from pathlib import Path

from stardew_ai_runtime.codex_game_profile import (
    game_tool_config,
    write_game_instructions,
)


def test_instruction_file_replaces_previous_text_without_temporary_files(tmp_path: Path):
    target = write_game_instructions(tmp_path, "old core")
    assert write_game_instructions(tmp_path, "new core\n中文") == target
    assert target.read_text(encoding="utf-8") == "new core\n中文"
    assert list(target.parent.iterdir()) == [target]


def test_tool_profile_keeps_harness_capabilities_and_existing_authority():
    args = game_tool_config()
    assert "features.code_mode_host=true" in args
    assert not any(arg.startswith("features.") and arg.endswith("=false") for arg in args)
    assert "mcp_optional_startup_grace_ms=0" in args
    assert not any("approval_policy" in arg or "sandbox_mode" in arg for arg in args)
