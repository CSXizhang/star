"""Offline unit suite must never invoke an installed model provider."""
import os
import subprocess
import pytest

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
