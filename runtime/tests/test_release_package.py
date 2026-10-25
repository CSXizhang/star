"""Release integrity and installer boundaries; no game or account access."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from support.release_fixture import package_at

from stardew_ai_runtime import compatibility
from stardew_ai_runtime.release_package import managed_path, verify_release

ROOT = Path(__file__).resolve().parents[2]


def test_relocated_release_pairs_without_developer_metadata(tmp_path, monkeypatch):
    root = tmp_path / "搬迁 空格" / "StardewAI.Companion.Mod"
    package_at(root)
    monkeypatch.setattr(compatibility, "__file__", str(root / "runtime/src/stardew_ai_runtime/compatibility.py"))
    compatibility.assert_native_compatible(root)
    with pytest.raises(compatibility.CompatibilityError, match="MOD_RUNTIME_MISMATCH"):
        compatibility.assert_native_compatible(tmp_path / "other-mod")
    (root / "runtime/src/stardew_ai_runtime/chat_bridge.py").write_text("changed")
    with pytest.raises(compatibility.CompatibilityError, match="MOD_RUNTIME_MISMATCH"):
        compatibility.assert_native_compatible(root)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows release registration")
def test_codex_registration_from_release_is_local_and_preserves_configuration(tmp_path):
    import tomllib

    root = tmp_path / "玩家发行包"
    package_at(root)
    project = tmp_path / "我的农场"
    config = project / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    original = 'model = "existing-choice"\n[mcp_servers.other]\ncommand = "other.exe"\n'
    config.write_text(original, encoding="utf-8")
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(root / "tools/register-mcp.ps1"), "-RunDir", str(root), "-Agent", "codex",
               "-ProjectDir", str(project), "-Install"]
    for _ in range(2):
        result = subprocess.run(command, capture_output=True)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert config.read_text(encoding="utf-8").startswith(original)
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    server = data["mcp_servers"]["stardew-companion"]
    assert server["command"] == str(root / "runtime/python/python.exe")
    assert server["env"] == {"STARDEW_EXTERNAL_CODEX": "1"}
    assert server["args"][4] == str(root)
    assert (project / ".agents/skills/stardew-companion/SKILL.md").is_file()
    # A hand-owned collision must leave every existing byte untouched.
    config.write_text('[mcp_servers."stardew-companion"]\ncommand = "mine.exe"\n', encoding="utf-8")
    before = config.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert config.read_bytes() == before


@pytest.mark.parametrize("relative", ["../outside", "/outside", "C:/outside", "data/memory.json", "config/chat-backend.json", ".kimi-code/mcp.json"])
def test_manifest_cannot_escape_or_own_player_data(tmp_path, relative):
    with pytest.raises(ValueError):
        managed_path(tmp_path, relative)


def test_manifest_cannot_omit_native_pairing(tmp_path):
    doc = package_at(tmp_path)
    doc["modSha256"] = "0" * 64
    (tmp_path / "release-manifest.json").write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="pairing"):
        verify_release(tmp_path)


def test_normal_pairing_reads_only_required_files_but_explicit_verify_audits_all(tmp_path, monkeypatch):
    from stardew_ai_runtime.release_package import REQUIRED

    doc = package_at(tmp_path)
    doc["files"].append({"path": "runtime/python/unused-package-file.txt", "sha256": "0" * 64})
    (tmp_path / "release-manifest.json").write_text(json.dumps(doc))
    read = []
    original = Path.read_bytes

    def counted(path):
        read.append(path.relative_to(tmp_path).as_posix())
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    verify_release(tmp_path, full=False)
    assert set(read) == REQUIRED and len(read) == len(REQUIRED)
    with pytest.raises(ValueError, match="missing"):
        verify_release(tmp_path)
    (tmp_path / "StardewAI.Companion.Mod.dll").write_bytes(b"mismatch")
    with pytest.raises(ValueError, match="changed"):
        verify_release(tmp_path, full=False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
def test_installer_isolated_upgrade_preserves_configuration_and_uses_bundled_python(tmp_path):
    root = tmp_path / "包 空格"
    package_at(root)
    game = tmp_path / "游戏 空格"
    destination = game / "Mods/StardewAI.Companion.Mod"
    (destination / "data").mkdir(parents=True)
    (destination / "config").mkdir()
    (game / "StardewModdingAPI.exe").write_bytes(b"fixture-only")
    (destination / "data/keep.json").write_text('{"memory":"keep"}')
    settings = {"backend": "kimi", "model": "player-chosen-model", "agentFile": "player-agent.yaml", "custom": {"keep": True}}
    (destination / "config/chat-backend.json").write_text(json.dumps(settings))
    (destination / ".kimi-code").mkdir()
    other_mcp = {"command": "other-client", "args": [], "env": {"OPTION": "keep"}}
    (destination / ".kimi-code/mcp.json").write_text(json.dumps({"mcpServers": {"other": other_mcp}}))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(root / "tools/setup-companion.ps1"), "-AutoInstall", "-GameDir", str(game)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((destination / "config/chat-backend.json").read_text(encoding="utf-8-sig")) == settings
    assert json.loads((destination / "data/keep.json").read_text())["memory"] == "keep"
    servers = json.loads((destination / ".kimi-code/mcp.json").read_text(encoding="utf-8-sig"))["mcpServers"]
    assert servers["other"] == other_mcp
    mcp = servers["stardew-companion"]
    assert mcp["command"] == str(destination / "runtime/python/python.exe")
    assert "uv" not in mcp["args"]
    assert str(destination) in mcp["args"]
    verify_release(destination)
    # A corrupted input is rejected before touching an installed file or data.
    (root / "StardewAI.Companion.Mod.dll").write_bytes(b"corrupted")
    blocked = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(root / "tools/setup-companion.ps1"), "-AutoInstall", "-GameDir", str(game), "-Agent", "none"],
                             capture_output=True, text=True, errors="replace")
    assert blocked.returncode != 0
    verify_release(destination)
    # Non-entry files must also be verified before any installed bytes change.
    (root / "StardewAI.Companion.Mod.dll").write_bytes(b"fixture-dll")
    optional = root / "runtime/python/optional-fixture.dat"
    optional.write_bytes(b"expected")
    manifest = json.loads((root / "release-manifest.json").read_text())
    manifest["files"].append({"path": "runtime/python/optional-fixture.dat",
                              "sha256": hashlib.sha256(b"expected").hexdigest()})
    (root / "release-manifest.json").write_text(json.dumps(manifest))
    optional.write_bytes(b"corrupted")
    before = {p.relative_to(destination).as_posix(): p.read_bytes()
              for p in destination.rglob("*") if p.is_file()}
    blocked = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(root / "tools/setup-companion.ps1"), "-AutoInstall", "-GameDir", str(game), "-Agent", "none"],
                             capture_output=True, text=True, errors="replace")
    assert blocked.returncode != 0
    assert {p.relative_to(destination).as_posix(): p.read_bytes()
            for p in destination.rglob("*") if p.is_file()} == before


def test_builder_allowlists_production_inputs():
    """A DLL folder may contain game/test outputs; the packager only lists production files."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools/build-release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.MOD_FILES) == {"manifest.json", "StardewAI.Companion.Mod.dll", "StardewAI.Companion.Mod.deps.json"}
    assert "pip" not in module.TOOL_FILES


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
@pytest.mark.parametrize("explicit_effort", [None, "default"])
@pytest.mark.parametrize("previous", [None, {"backend": "mcode", "model": "", "effort": "high"},
                                     {"backend": "dsh", "model": "deepseek-flash", "effort": "max"}])
def test_mcode_release_install_defaults_and_preserves_existing_choices(tmp_path, previous, explicit_effort):
    import os

    root = tmp_path / "新包 空格"
    package_at(root)
    game = tmp_path / "游戏 空格"
    destination = game / "Mods/StardewAI.Companion.Mod"
    (destination / "config").mkdir(parents=True)
    (destination / "data").mkdir()
    (game / "StardewModdingAPI.exe").write_bytes(b"fixture-only")
    memory = destination / "data/player-memory.json"
    memory.write_bytes(b'{"keep":true}')
    config = destination / "config/chat-backend.json"
    if previous:
        config.write_text(json.dumps({**previous, "custom": {"keep": True}}), encoding="utf-8")
    global_mcp = tmp_path / "minimax-account/mcp.json"
    global_mcp.parent.mkdir()
    original = b'{"mcpServers":{"other":{"command":"keep.exe"}}}'
    global_mcp.write_bytes(original)
    env = dict(os.environ, MINIMAX_DATA_DIR=str(global_mcp.parent), MAVIS_DATA_DIR=str(global_mcp.parent))
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(root / "tools/setup-companion.ps1"), "-AutoInstall", "-GameDir", str(game)]
    if not previous or previous["backend"] != "mcode":
        command += ["-Agent", "mcode"]
    if explicit_effort:
        command += ["-Effort", explicit_effort]
    result = subprocess.run(command, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    saved = json.loads(config.read_text(encoding="utf-8-sig"))
    assert saved["backend"] == "mcode" and saved["model"] == ""
    assert saved["effort"] == (explicit_effort or ("high" if previous and previous["backend"] == "mcode" else "default"))
    if previous:
        assert saved["custom"] == {"keep": True}
    assert memory.read_bytes() == b'{"keep":true}'
    assert global_mcp.read_bytes() == original
    assert "global MCP settings were not changed" in result.stdout
    verify_release(destination)
