"""Offline unit suite must never invoke an installed model provider."""
import os
import subprocess
from pathlib import Path

import pytest

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
