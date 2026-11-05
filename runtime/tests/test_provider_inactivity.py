"""Real child processes keep progressing beyond the old total deadline."""
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from stardew_ai_runtime.agent_backends import CodexBackend, DshBackend, KimiBackend
from stardew_ai_runtime.agy_process import run_agy_process


@pytest.mark.parametrize("provider", ["codex", "kimi", "agy"])
def test_productive_stream_outlives_timeout_but_stderr_does_not(tmp_path, monkeypatch, provider):
    events = {
        "codex": ({"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "observe"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        "kimi": ({"type": "tool_start", "tool_name": "observe"}, {"role": "assistant", "content": "done"}),
        "agy": ({"event": "step_update", "step_update": {"tool_name": "observe"}},
                {"event": "result", "result": {"status": "SUCCESS", "response": "done"}}),
    }
    progress, final = events[provider]
    fake = tmp_path / "progress.py"
    fake.write_text("import sys,time\nfor i in range(9):\n print(" + repr(json.dumps(progress))
                    + ",flush=True)\n time.sleep(.12)\nprint(" + repr(json.dumps(final)) + ",flush=True)\n")
    python = tmp_path / "runtime/python/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    actual_popen = subprocess.Popen
    starts = []
    def spawn(command, **kwargs):
        if command[0] == "taskkill":
            return actual_popen(command, **kwargs)
        starts.append(command)
        return actual_popen([sys.executable, str(fake)], **kwargs)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    task = SimpleNamespace(cancelled=False, process=None, request_id="test", save_id="")
    def invoke():
        if provider == "agy":
            result = run_agy_process(task, ["fake"], "work", "original", timeout_seconds=.5)
            return not result.timed_out and json.loads(result.stdout).get("status") == "SUCCESS"
        backend = (CodexBackend(tmp_path, timeout_seconds=.5) if provider == "codex"
                   else KimiBackend(command="fake", timeout_seconds=.5))
        return backend.run(task, "original", "work")["success"]
    assert invoke() and task.process is None
    # Arbitrary diagnostics are not model/native progress.
    fake.write_text("import sys,time\nfor i in range(20):\n print('log',file=sys.stderr,flush=True)\n time.sleep(.12)\n")
    assert not invoke() and task.process is None
    assert len(starts) == 2
    assert all("mcp" not in cmd for cmd in starts)
    assert not any("enabled=false" in arg for cmd in starts for arg in cmd)


def test_dsh_root_progress_extends_silence_but_child_events_do_not(tmp_path, monkeypatch):
    from test_dsh_protocol import event, status
    backend = DshBackend(tmp_path, timeout_seconds=.35)
    monkeypatch.setattr(backend, "_prepare", lambda _: (tmp_path, {}))
    monkeypatch.setattr(backend, "_start", lambda *_: None)
    monkeypatch.setattr(backend, "_send", lambda *_: 42)
    monkeypatch.setattr(backend, "close", lambda: None)
    backend._process = SimpleNamespace(poll=lambda: None)
    task = SimpleNamespace(cancelled=False, process=None)
    def feed():
        backend._events.put({"id": 42, "result": {}})
        backend._events.put(event("turn/start"))
        for _ in range(7):
            time.sleep(.1)
            backend._events.put(event("tool/result"))
        backend._events.put(event("turn/end", {"reason": {"kind": "completed"}}))
        backend._events.put(status("idle"))
    writer = threading.Thread(target=feed)
    writer.start()
    try:
        assert backend.run(task, "root", "work")["success"]
    finally:
        writer.join(3)
    def child_feed():
        backend._events.put({"id": 42, "result": {}})
        backend._events.put(event("turn/start"))
        for _ in range(7):
            time.sleep(.1)
            backend._events.put(event("tool/result", session="child"))
    writer = threading.Thread(target=child_feed)
    writer.start()
    try:
        assert not backend.run(task, "root", "work")["success"]
    finally:
        writer.join(3)
