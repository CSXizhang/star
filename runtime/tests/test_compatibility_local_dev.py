import hashlib
import json

import pytest

from stardew_ai_runtime import compatibility as compat


def test_missing_manifest_and_binding_shows_setup_wizard_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "__file__", str(tmp_path / "runtime/src/stardew_ai_runtime/compatibility.py"))
    with pytest.raises(compat.CompatibilityError) as exc_info:
        compat.assert_native_compatible(tmp_path)
    err = str(exc_info.value)
    assert "COMPATIBILITY_UNKNOWN" in err
    assert "先运行设置向导或安装修复包" in err


def test_local_dev_manifest_compatibility_success(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "__file__", str(tmp_path / "runtime/src/stardew_ai_runtime/compatibility.py"))
    mod = tmp_path / "Mods/StardewAI.Companion.Mod"
    mod.mkdir(parents=True)
    dll = mod / "StardewAI.Companion.Mod.dll"
    dll_content = b"local-dev-dll-content"
    dll.write_bytes(dll_content)
    dll_hash = hashlib.sha256(dll_content).hexdigest().upper()

    manifest = tmp_path / "artifacts/releases/local-dev/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "version": "local-dev",
        "manifestType": "local-dev",
        "modSha256": dll_hash,
    }, ensure_ascii=False), encoding="utf-8")

    installed = tmp_path / "config/installed-candidate.json"
    installed.parent.mkdir(parents=True)
    installed.write_text(json.dumps({
        "version": "local-dev",
        "manifestType": "local-dev",
        "manifest": str(manifest),
    }), encoding="utf-8")

    # Should pass cleanly
    compat.assert_native_compatible(mod)


def test_local_dev_dll_modified_raises_mod_runtime_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "__file__", str(tmp_path / "runtime/src/stardew_ai_runtime/compatibility.py"))
    mod = tmp_path / "Mods/StardewAI.Companion.Mod"
    mod.mkdir(parents=True)
    dll = mod / "StardewAI.Companion.Mod.dll"
    dll.write_bytes(b"modified-local-dev-dll")

    manifest = tmp_path / "artifacts/releases/local-dev/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "version": "local-dev",
        "manifestType": "local-dev",
        "modSha256": hashlib.sha256(b"original-content").hexdigest().upper(),
    }), encoding="utf-8")

    installed = tmp_path / "config/installed-candidate.json"
    installed.parent.mkdir(parents=True)
    installed.write_text(json.dumps({
        "version": "local-dev",
        "manifest": str(manifest),
    }), encoding="utf-8")

    with pytest.raises(compat.CompatibilityError) as exc_info:
        compat.assert_native_compatible(mod)
    err = str(exc_info.value)
    assert "MOD_RUNTIME_MISMATCH" in err
    assert "tools/build-mod.ps1 与 tools/setup-companion.ps1" in err


def test_local_dev_dll_missing_raises_compatibility_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "__file__", str(tmp_path / "runtime/src/stardew_ai_runtime/compatibility.py"))
    empty_mod = tmp_path / "empty_mod"
    empty_mod.mkdir()

    manifest = tmp_path / "artifacts/releases/local-dev/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "version": "local-dev",
        "modSha256": "SOMEHASH",
    }), encoding="utf-8")

    installed = tmp_path / "config/installed-candidate.json"
    installed.parent.mkdir(parents=True)
    installed.write_text(json.dumps({
        "version": "local-dev",
        "manifest": str(manifest),
    }), encoding="utf-8")

    with pytest.raises(compat.CompatibilityError) as exc_info:
        compat.assert_native_compatible(empty_mod)
    err = str(exc_info.value)
    assert "COMPATIBILITY_UNKNOWN" in err
    assert "tools/build-mod.ps1 与 tools/setup-companion.ps1" in err


def test_official_package_mismatch_retains_update_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "__file__", str(tmp_path / "runtime/src/stardew_ai_runtime/compatibility.py"))
    mod = tmp_path / "mod"
    mod.mkdir()
    dll = mod / "StardewAI.Companion.Mod.dll"
    dll.write_bytes(b"modified-official")

    manifest = tmp_path / "artifacts/releases/repair-r44/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "version": "farm-r44",
        "modSha256": hashlib.sha256(b"official-content").hexdigest().upper(),
    }), encoding="utf-8")

    installed = tmp_path / "config/installed-candidate.json"
    installed.parent.mkdir(parents=True)
    installed.write_text(json.dumps({
        "version": "farm-r44",
        "manifest": str(manifest),
    }), encoding="utf-8")

    with pytest.raises(compat.CompatibilityError) as exc_info:
        compat.assert_native_compatible(mod)
    err = str(exc_info.value)
    assert "MOD_RUNTIME_MISMATCH" in err
    assert "tools/setup-companion.ps1" in err


def test_local_dev_fallback_without_installed_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "__file__", str(tmp_path / "runtime/src/stardew_ai_runtime/compatibility.py"))
    mod = tmp_path / "mod"
    mod.mkdir()
    dll = mod / "StardewAI.Companion.Mod.dll"
    content = b"local-dev-fallback"
    dll.write_bytes(content)

    manifest = tmp_path / "artifacts/releases/local-dev/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "version": "local-dev",
        "modSha256": hashlib.sha256(content).hexdigest().upper(),
    }), encoding="utf-8")

    # Should use local-dev fallback even without config/installed-candidate.json
    compat.assert_native_compatible(mod)
