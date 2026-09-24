import hashlib
import json

import pytest

from stardew_ai_runtime import compatibility as compat


def test_installed_package_selects_matching_ui_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "__file__", str(tmp_path/"runtime/src/stardew_ai_runtime/compatibility.py"))
    mod = tmp_path/"mod"
    mod.mkdir()
    dll = mod/"StardewAI.Companion.Mod.dll"
    manifest = tmp_path/"artifacts/releases/repair-r44/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"modSha256": hashlib.sha256(b"r44").hexdigest().upper()}))
    installed = tmp_path/"config/installed-candidate.json"
    installed.parent.mkdir()
    installed.write_text(json.dumps({"manifest": str(manifest)}))
    dll.write_bytes(b"r44")
    compat.assert_native_compatible(mod)
    dll.write_bytes(b"r43")
    with pytest.raises(compat.CompatibilityError, match="MOD_RUNTIME_MISMATCH"):
        compat.assert_native_compatible(mod)
    installed.write_text(json.dumps({"manifest": str(tmp_path/"outside.json")}))
    with pytest.raises(compat.CompatibilityError, match="COMPATIBILITY_UNKNOWN"):
        compat.assert_native_compatible(mod)
