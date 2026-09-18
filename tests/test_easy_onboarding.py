"""Automated tests for easy onboarding wizard, detection, and registration tools."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_double_click_entry_files_exist() -> None:
    cmd_file = REPO_ROOT / "设置星露谷伙伴.cmd"
    bat_file = REPO_ROOT / "设置星露谷伙伴.bat"
    ps_file = REPO_ROOT / "tools" / "setup-companion.ps1"

    assert cmd_file.is_file(), "设置星露谷伙伴.cmd missing"
    assert bat_file.is_file(), "设置星露谷伙伴.bat missing"
    assert ps_file.is_file(), "tools/setup-companion.ps1 missing"

    content_cmd = cmd_file.read_text(encoding="utf-8")
    assert "setup-companion.ps1" in content_cmd
    assert "-STA" in content_cmd


def test_setup_companion_check_only() -> None:
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "tools" / "setup-companion.ps1"),
        "-CheckOnly",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert res.returncode == 0, f"CheckOnly failed: {res.stderr}\n{res.stdout}"

    # Parse JSON output
    data = json.loads(res.stdout.strip())
    assert "GameFound" in data
    assert "ModStatus" in data
    if data["GameFound"]:
        assert data["GamePath"]
        assert "TargetModDir" in data["ModStatus"]


def test_setup_companion_dry_run() -> None:
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "tools" / "setup-companion.ps1"),
        "-DryRun",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert res.returncode == 0, f"DryRun failed: {res.stderr}\n{res.stdout}"


def test_setup_companion_isolated_install(tmp_path: Path) -> None:
    # Test installing into a path with spaces and Chinese characters
    iso_mod_dir = tmp_path / "测试 安装 目录 with spaces" / "Mods" / "StardewAI.Companion.Mod"
    data_dir = iso_mod_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    custom_data = data_dir / "important.json"
    custom_data.write_text('{"keep": true}', encoding="utf-8")

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "tools" / "setup-companion.ps1"),
        "-AutoInstall",
        "-TargetModDir",
        str(iso_mod_dir),
        "-Agent",
        "none",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert res.returncode == 0, f"AutoInstall failed: {res.stderr}\n{res.stdout}"

    # Verify 4 production files were copied
    assert (iso_mod_dir / "manifest.json").is_file()
    assert (iso_mod_dir / "StardewAI.Companion.Mod.dll").is_file()
    assert (iso_mod_dir / "StardewAI.Companion.Mod.pdb").is_file()
    assert (iso_mod_dir / "StardewAI.Companion.Mod.deps.json").is_file()

    # Verify TestDriver is strictly NOT present
    assert not (iso_mod_dir / "StardewAI.Companion.TestDriver.dll").exists()

    # Verify data/ was protected
    assert custom_data.is_file()
    assert custom_data.read_text(encoding="utf-8") == '{"keep": true}'

    # Re-run for idempotency test
    res2 = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert res2.returncode == 0
    assert custom_data.read_text(encoding="utf-8") == '{"keep": true}'


def test_register_mcp_spaces_handling() -> None:
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "tools" / "register-mcp.ps1"),
        "-RunDir",
        "artifacts/dist/StardewAI.Companion.Mod",
        "-Agent",
        "all",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert res.returncode == 0, f"register-mcp failed: {res.stderr}\n{res.stdout}"
    assert "stardew-companion" in res.stdout
    assert "mcpServers" in res.stdout
