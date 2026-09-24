"""Offline unit suite must never invoke an installed model provider."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import stardew_ai_runtime.compatibility as compat_module
from stardew_ai_runtime.compatibility import CompatibilityError, assert_native_compatible
from stardew_ai_runtime.scheduler import DiscoveryError, resolve_discovery


@pytest.fixture(autouse=True)
def forbid_real_provider_processes(monkeypatch):
    original=subprocess.Popen
    class GuardedPopen(original):
        def __init__(self,command,*args,**kwargs):
            parts=command if isinstance(command,(list,tuple)) else [str(command)]
            executable=os.path.basename(str(parts[0])).lower()
            if executable in {"kimi", "kimi.exe", "agy", "agy.exe", "dsh", "dsh.exe"} or "stardew_ai_runtime.chat_bridge" in parts:
                raise AssertionError("OFFLINE_TEST_ONLY: installed provider subprocess is forbidden; use a fake backend")
            super().__init__(command,*args,**kwargs)
    monkeypatch.setattr(subprocess,"Popen",GuardedPopen)


@pytest.fixture
def bound_native_game() -> Path:
    """Skip unless this environment has a native-compatible game instance bound.

    Probes through the production discovery + compatibility gate (STARDEW_RUN_DIR /
    STARDEW_GAME_PATH, then the repo's own manifests), so tests wired to this
    fixture run wherever a real Companion Mod install is present and skip with the
    gate's own message instead of failing COMPATIBILITY_UNKNOWN.
    """
    try:
        discovery = resolve_discovery(None)
    except DiscoveryError as ex:
        pytest.skip(f"no bound game instance in this environment: {ex}")
    run_dir = Path(discovery["modDir"])
    try:
        assert_native_compatible(run_dir)
    except CompatibilityError as ex:
        pytest.skip(str(ex))
    return run_dir


@pytest.fixture
def native_compatible_run_dir(tmp_path: Path, monkeypatch) -> Path:
    """Synthetic run_dir that passes the real compatibility gate without a game.

    Builds the minimal local-dev pairing under tmp_path — a mod dir whose DLL
    sha256 matches a local-dev manifest — and points the gate's repo-root anchor
    (compatibility.__file__) at the same tmp tree. This is the exact recipe of
    test_compatibility_local_dev.py: assert_native_compatible itself runs
    unmocked against synthetic pairing materials; only the anchor that locates
    the repo's own artifacts/ and config/ is redirected. Core-logic unit tests
    wired to fake clients should use this instead of bound_native_game so they
    stop skipping wherever no game instance is bound.
    """
    repo_root = tmp_path / "repo"
    mod_dir = repo_root / "Mods" / "StardewAI.Companion.Mod"
    mod_dir.mkdir(parents=True)
    dll = mod_dir / "StardewAI.Companion.Mod.dll"
    dll.write_bytes(b"synthetic-local-dev-dll")
    manifest = repo_root / "artifacts" / "releases" / "local-dev" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "version": "local-dev",
                "manifestType": "local-dev",
                "modSha256": hashlib.sha256(dll.read_bytes()).hexdigest().upper(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        compat_module,
        "__file__",
        str(repo_root / "runtime" / "src" / "stardew_ai_runtime" / "compatibility.py"),
    )
    assert_native_compatible(mod_dir)
    return mod_dir
