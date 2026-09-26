"""Release integrity and installer boundaries; no game or account access."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from stardew_ai_runtime import compatibility
from stardew_ai_runtime.release_package import managed_path, verify_release

ROOT = Path(__file__).resolve().parents[2]


def package_at(root):
    files = {
        "manifest.json": json.dumps({"Version": "0.2.0", "EntryDll": "StardewAI.Companion.Mod.dll"}).encode(),
        "StardewAI.Companion.Mod.dll": b"fixture-dll",
        "runtime/python/python.exe": b"fixture-python-not-executed",
        "runtime/src/stardew_ai_runtime/chat_bridge.py": b"# fixture-runtime",
    }
    for name in ("setup-companion.ps1", "install-release.ps1", "release-package.ps1", "register-mcp.ps1", "start-companion.ps1", "detect-game.ps1"):
        files["tools/" + name] = (ROOT / "tools" / name).read_bytes()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    doc = {
        "manifestType": "windows-release", "schemaVersion": 1, "version": "0.2.0",
        "platform": "windows-x64", "python": "runtime/python/python.exe",
        "modSha256": hashlib.sha256(files["StardewAI.Companion.Mod.dll"]).hexdigest(),
        "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items()],
    }
    (root / "release-manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc


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


def test_builder_allowlists_production_inputs():
    """A DLL folder may contain game/test outputs; the packager only lists production files."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools/build-release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.MOD_FILES) == {"manifest.json", "StardewAI.Companion.Mod.dll", "StardewAI.Companion.Mod.deps.json"}
    assert "pip" not in module.TOOL_FILES
