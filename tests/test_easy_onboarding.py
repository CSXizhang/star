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


def test_setup_companion_local_dev_manifest_and_compatibility(tmp_path: Path) -> None:
    # The wizard writes repo-level binding state; snapshot and restore it so the
    # production binding survives the test regardless of outcome.
    installed_cfg = REPO_ROOT / "config" / "installed-candidate.json"
    local_dev_dir = REPO_ROOT / "artifacts" / "releases" / "local-dev"
    cfg_backup = installed_cfg.read_bytes() if installed_cfg.is_file() else None
    local_dev_backup = (local_dev_dir / "manifest.json").read_bytes() if (local_dev_dir / "manifest.json").is_file() else None
    try:
        _run_local_dev_onboarding_case(tmp_path, installed_cfg)
    finally:
        if cfg_backup is not None:
            installed_cfg.write_bytes(cfg_backup)
        elif installed_cfg.is_file():
            installed_cfg.unlink()
        if local_dev_backup is not None:
            local_dev_dir.mkdir(parents=True, exist_ok=True)
            (local_dev_dir / "manifest.json").write_bytes(local_dev_backup)
        elif (local_dev_dir / "manifest.json").is_file():
            (local_dev_dir / "manifest.json").unlink()


def _run_local_dev_onboarding_case(tmp_path: Path, installed_cfg: Path) -> None:
    import hashlib
    import sys

    import pytest
    sys.path.insert(0, str(REPO_ROOT / "runtime" / "src"))
    from stardew_ai_runtime import compatibility as compat

    fake_game_dir = tmp_path / "FakeGame"
    fake_mod_dir = fake_game_dir / "Mods" / "StardewAI.Companion.Mod"
    fake_mod_dir.mkdir(parents=True, exist_ok=True)
    (fake_game_dir / "StardewModdingAPI.exe").write_bytes(b"smapi-mock")

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "tools" / "setup-companion.ps1"),
        "-AutoInstall",
        "-GameDir",
        str(fake_game_dir),
        "-TargetModDir",
        str(fake_mod_dir),
        "-Agent",
        "none",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert res.returncode == 0, f"AutoInstall failed: {res.stderr}\n{res.stdout}"

    installed_cfg = REPO_ROOT / "config" / "installed-candidate.json"
    assert installed_cfg.is_file(), "config/installed-candidate.json was not created"
    installed_data = json.loads(installed_cfg.read_text(encoding="utf-8-sig"))
    assert installed_data["version"] == "local-dev"
    assert installed_data["manifestType"] == "local-dev"

    local_manifest = Path(installed_data["manifest"])
    assert local_manifest.is_file(), "local-dev manifest.json was not created"
    manifest_data = json.loads(local_manifest.read_text(encoding="utf-8"))
    assert manifest_data["version"] == "local-dev"
    assert manifest_data["modSha256"]

    installed_dll = fake_mod_dir / "StardewAI.Companion.Mod.dll"
    assert installed_dll.is_file()
    actual_hash = hashlib.sha256(installed_dll.read_bytes()).hexdigest().upper()
    assert manifest_data["modSha256"] == actual_hash

    # 1. Compatibility gate passes for freshly installed local-dev
    compat.assert_native_compatible(fake_mod_dir)

    # 2. start-companion.ps1 -CheckOnly succeeds for verified local-dev
    start_cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "tools" / "start-companion.ps1"),
        "-CheckOnly",
    ]
    start_res = subprocess.run(start_cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert start_res.returncode == 0, f"start-companion -CheckOnly failed: {start_res.stderr}\n{start_res.stdout}"
    assert "Candidate local-dev" in start_res.stdout

    # 3. DLL modified -> compatibility.py throws MOD_RUNTIME_MISMATCH with local-dev guidance
    installed_dll.write_bytes(b"tampered-dll-bytes")
    with pytest.raises(compat.CompatibilityError) as exc_info:
        compat.assert_native_compatible(fake_mod_dir)
    err = str(exc_info.value)
    assert "MOD_RUNTIME_MISMATCH" in err
    assert "tools/build-mod.ps1 与 tools/setup-companion.ps1" in err

    # 4. DLL modified -> start-companion.ps1 also catches difference and suggests rebuild + setup
    start_tampered = subprocess.run(start_cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert start_tampered.returncode != 0
    assert "Installed Mod and local-dev runtime differ" in start_tampered.stderr or "Installed Mod and local-dev runtime differ" in start_tampered.stdout
    assert "setup-companion.ps1" in (start_tampered.stderr + start_tampered.stdout)
