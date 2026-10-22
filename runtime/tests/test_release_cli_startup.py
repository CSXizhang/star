"""Startup uses a Steam-like PATH; no game, model request, or account access."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_release_package import ROOT, package_at

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")


def _powershell(tmp_path, code, *, path=""):
    exe = shutil.which("powershell.exe")
    assert exe
    env = dict(os.environ)
    env.update(LOCALAPPDATA=str(tmp_path / "appdata"), APPDATA=str(tmp_path / "roaming"),
               USERPROFILE=str(tmp_path / "profile"), PATH=path)
    source = (ROOT / "tools/release-package.ps1").as_posix().replace("'", "''")
    return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", f". '{source}'; {code}"],
                          env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=15)


def test_desktop_cli_is_found_with_no_codex_in_steam_path(tmp_path):
    folder = tmp_path / "appdata/OpenAI/Codex/bin"
    old = folder / "old/codex.exe"
    current = folder / "new/codex.exe"
    old.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    (folder / "incomplete").mkdir()
    old.write_bytes(b"fixture, not executed")
    current.write_bytes(b"fixture, not executed")
    os.utime(old, (100, 100))
    os.utime(current, (200, 200))
    result = _powershell(tmp_path, "Find-ReleaseAiCli 'codex'")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == current


def test_explicit_path_installation_takes_precedence_over_desktop(tmp_path):
    desktop = tmp_path / "appdata/OpenAI/Codex/bin/new/codex.exe"
    explicit = tmp_path / "preferred/codex.exe"
    for file in (desktop, explicit):
        file.parent.mkdir(parents=True)
        file.write_bytes(b"fixture, not executed")
    result = _powershell(tmp_path, "Find-ReleaseAiCli 'codex'", path=str(explicit.parent))
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == explicit


@pytest.mark.parametrize("backend,location", [("agy", "appdata/agy/bin/agy.exe"), ("kimi", "profile/.kimi-code/bin/kimi.exe")])
def test_current_user_client_installation_is_found(tmp_path, backend, location):
    file = tmp_path / location
    file.parent.mkdir(parents=True)
    file.write_bytes(b"fixture, not executed")
    result = _powershell(tmp_path, f"Find-ReleaseAiCli '{backend}'")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == file


def test_check_only_does_not_hide_missing_selected_cli_behind_python_import(tmp_path):
    package_at(tmp_path / "package")
    config = tmp_path / "package/config/chat-backend.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"backend": "codex", "model": ""}), encoding="utf-8")
    root = config.parents[1].as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"Start-ReleaseCompanion '{root}' -CheckOnly")
    assert result.returncode != 0
    assert "AI CLI codex is missing" in result.stderr
    assert "Portable runtime imports OK" not in result.stdout
    assert "verified" not in result.stdout


def test_unusable_cli_reports_controlled_startup_error(tmp_path):
    bad = tmp_path / "bad/codex.exe"
    bad.parent.mkdir()
    bad.write_bytes(b"not an executable")
    path = bad.as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"Assert-ReleaseAiCli '{path}' 'codex'")
    assert result.returncode != 0
    assert "AI CLI codex could not start" in result.stderr


@pytest.mark.parametrize("backend", ["dsh", "mcode"])
def test_npm_cmd_in_custom_prefix_takes_precedence_over_roaming(tmp_path, backend):
    explicit = tmp_path / f"custom npm prefix/{backend}.cmd"
    standard = tmp_path / f"roaming/npm/{backend}.cmd"
    for file in (explicit, standard):
        file.parent.mkdir(parents=True)
        file.write_bytes(b"fixture, not executed")
    result = _powershell(tmp_path, f"Find-ReleaseAiCli '{backend}'", path=str(explicit.parent))
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == explicit


@pytest.mark.parametrize("backend", ["dsh", "mcode"])
def test_standard_npm_shim_is_found_without_any_cli_in_path(tmp_path, backend):
    shim = tmp_path / f"roaming/npm/{backend}.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"fixture, not executed")
    result = _powershell(tmp_path, f"Find-ReleaseAiCli '{backend}'")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == shim


@pytest.mark.parametrize("backend", ["dsh", "mcode"])
def test_npm_codex_app_cache_returns_physical_prefix(tmp_path, backend):
    shim = tmp_path / f"appdata/Packages/OpenAI.Codex_fixture/LocalCache/Roaming/npm/{backend}.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"fixture, not executed")
    root = (tmp_path / "release").as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"Find-ReleaseCachedNpmCli $env:LOCALAPPDATA '{backend}' -LogRoot '{root}'")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == shim
    probe = json.loads((tmp_path / "release/data/release-start.log").read_text(encoding="utf-8-sig").strip())
    assert probe["source"] == "codex-app-cache" and probe["exists"] is True
    assert probe["backend"] == backend
    assert Path(probe["path"]) == shim


def test_dsh_cache_discovery_ignores_other_apps_and_incomplete_codex_folders(tmp_path):
    unrelated = tmp_path / "appdata/Packages/Other.App_fixture/LocalCache/Roaming/npm/dsh.cmd"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"fixture, not executed")
    (tmp_path / "appdata/Packages/OpenAI.Codex_incomplete").mkdir()
    result = _powershell(tmp_path, "Find-ReleaseCachedDshCli $env:LOCALAPPDATA")
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip()


@pytest.mark.parametrize("backend", ["dsh", "mcode"])
def test_standard_npm_installation_takes_precedence_over_app_cache(tmp_path, backend):
    standard = tmp_path / f"roaming/npm/{backend}.cmd"
    cached = tmp_path / f"appdata/Packages/OpenAI.Codex_fixture/LocalCache/Roaming/npm/{backend}.cmd"
    for file in (standard, cached):
        file.parent.mkdir(parents=True)
        file.write_bytes(b"fixture, not executed")
    result = _powershell(tmp_path, f"Find-ReleaseAiCli '{backend}'")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == standard


@pytest.mark.parametrize("backend", ["dsh", "mcode"])
def test_npm_node_beside_shim_has_precedence_over_path(tmp_path, backend):
    adjacent = tmp_path / "npm prefix/node.exe"
    path_node = tmp_path / "other node/node.exe"
    for file in (adjacent, path_node):
        file.parent.mkdir(parents=True)
        file.write_bytes(b"fixture, not executed")
    shim = (adjacent.parent / f"{backend}.cmd").as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"Find-ReleaseNpmNode '{shim}' '{backend}'", path=str(path_node.parent))
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == adjacent


@pytest.mark.parametrize("backend", ["dsh", "mcode"])
def test_npm_node_standard_installation_is_found_with_empty_path(tmp_path, backend):
    node = tmp_path / "program files/nodejs/node.exe"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"fixture, not executed")
    program_files = node.parents[1].as_posix().replace("'", "''")
    shim = (tmp_path / f"npm/{backend}.cmd").as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"$env:ProgramW6432='{program_files}'; Find-ReleaseNpmNode '{shim}' '{backend}'")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == node


@pytest.mark.parametrize("backend", ["dsh", "mcode"])
def test_partial_npm_installation_reports_missing_entry_before_execution(tmp_path, backend):
    prefix = tmp_path / "npm"
    prefix.mkdir()
    (prefix / f"{backend}.cmd").write_bytes(b"fixture, not executed")
    (prefix / "node.exe").write_bytes(b"fixture, not executed")
    shim = (prefix / f"{backend}.cmd").as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"Assert-ReleaseAiCli '{shim}' '{backend}'")
    assert result.returncode != 0
    assert f"AI CLI {backend} entry point is missing" in result.stderr
    assert "could not start" not in result.stderr


def test_dsh_lookup_records_only_paths_and_probe_results(tmp_path):
    shim = tmp_path / "roaming/npm/dsh.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"fixture, not executed")
    root = (tmp_path / "release").as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"$env:DEEPSEEK_API_KEY='never-log-this'; Find-ReleaseAiCli 'dsh' -LogRoot '{root}'")
    assert result.returncode == 0, result.stderr
    raw = (tmp_path / "release/data/release-start.log").read_text(encoding="utf-8-sig")
    probe = json.loads(raw.strip())
    assert probe["event"] == "cli-probe" and probe["exists"] is True
    assert Path(probe["path"]) == shim
    assert "never-log-this" not in raw and "DEEPSEEK_API_KEY" not in raw


def test_mcode_version_check_runs_node_entry_with_spaces_and_only_version_argument(tmp_path):
    node = shutil.which("node.exe")
    if not node:
        pytest.skip("Node required for offline CLI fixture")
    prefix = tmp_path / "MiniMax & npm prefix"
    entry = prefix / "node_modules/@minimax-ai/code/cli.js"
    entry.parent.mkdir(parents=True)
    shim = prefix / "mcode.cmd"
    shim.write_text("@exit /b 93\n")  # The startup check must bypass this shim.
    marker = tmp_path / "version-arguments.json"
    entry.write_text(
        "require('fs').writeFileSync(" + json.dumps(str(marker)) + ", JSON.stringify(process.argv.slice(2)));\n"
        "console.log('offline-fixture-version');\n",
        encoding="utf-8",
    )
    binary = shim.as_posix().replace("'", "''")
    root = (tmp_path / "release").as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"Assert-ReleaseAiCli '{binary}' 'mcode' -LogRoot '{root}'", path=str(Path(node).parent))
    assert result.returncode == 0, result.stderr
    assert json.loads(marker.read_text()) == ["--version"]
    diagnostic = json.loads((tmp_path / "release/data/release-start.log").read_text(encoding="utf-8-sig").strip())
    assert Path(diagnostic["entry"]) == entry
    assert Path(diagnostic["executable"]) == Path(node)


def test_mcode_check_only_reaches_cli_validation_before_python(tmp_path):
    root = tmp_path / "package"
    package_at(root)
    config = root / "config/chat-backend.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"backend": "mcode", "model": "", "effort": "low"}), encoding="utf-8")
    prefix = tmp_path / "npm"
    prefix.mkdir()
    for name in ("mcode.cmd", "node.exe"):
        (prefix / name).write_bytes(b"fixture, not executed")
    package = root.as_posix().replace("'", "''")
    result = _powershell(tmp_path, f"Start-ReleaseCompanion '{package}' -CheckOnly", path=str(prefix))
    assert result.returncode != 0
    assert "AI CLI mcode entry point is missing" in result.stderr
    assert "Portable runtime imports OK" not in result.stdout
