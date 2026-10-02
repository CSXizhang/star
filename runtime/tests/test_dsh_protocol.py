"""Deterministic official SDK event-order and reusable-session boundaries."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stardew_ai_runtime.agent_backends import DshBackend


def event(kind, data=None, session="root"):
    return {"method": "session.event", "params": {"sessionId": session,
        "event": {"type": kind, "data": data or {}}}}


def status(value):
    return {"method": "session.status", "params": {"sessionId": "root", "status": value}}


def message(text, session="root"):
    return event("assistant/message", {"message": {"content": [{"type": "text", "text": text}]}}, session)


def run_frames(tmp_path, monkeypatch, frames, *, cancelled=False):
    monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
    backend = DshBackend(tmp_path, timeout_seconds=.2)
    monkeypatch.setattr(backend, "_prepare", lambda _: (tmp_path, {}))
    monkeypatch.setattr(backend, "_start", lambda *_: None)
    monkeypatch.setattr(backend, "_send", lambda *_: 42)
    for frame in frames:
        backend._events.put(frame)
    task = SimpleNamespace(cancelled=cancelled, process=None)
    close = Mock()
    monkeypatch.setattr(backend, "close", close)
    return backend.run(task, "root", "hello"), close


@pytest.mark.parametrize("receipt_first", [True, False])
def test_idle_and_terminal_before_prompt_receipt(tmp_path, monkeypatch, receipt_first):
    receipt = {"id": 42, "result": {}}
    frames = [status("running"), event("turn/start"), message("好，我来。"),
              event("turn/end", {"reason": {"kind": "completed"}}), status("idle")]
    frames.insert(0 if receipt_first else len(frames), receipt)
    result, close = run_frames(tmp_path, monkeypatch, frames)
    assert result["success"] and result["response"] == "好，我来。"
    close.assert_not_called()  # Chat retains its SDK runtime.


def test_stale_terminal_and_child_session_cannot_finish_root_turn(tmp_path, monkeypatch):
    frames = [event("turn/end", {"reason": {"kind": "completed"}}), status("idle"),
              message("old stale text"), {"id": 42, "result": {}}, status("running"),
              event("turn/start"), message("child text", "child"),
              event("turn/end", {"reason": {"kind": "failed"}}, "child"),
              message("current root"), event("turn/end", {"reason": {"kind": "completed"}}), status("idle")]
    result, _ = run_frames(tmp_path, monkeypatch, frames)
    assert result["success"] and result["response"] == "current root"


def test_cancel_closes_sdk_without_waiting_for_terminal(tmp_path, monkeypatch):
    result, close = run_frames(tmp_path, monkeypatch, [], cancelled=True)
    assert not result["success"] and result["error"] == "CANCELLED"
    close.assert_called_once()


def test_transport_failure_is_diagnostic_without_leaking_provider_details(tmp_path, monkeypatch):
    frames = [{"id": 42, "result": {}}, status("running"), event("turn/start"),
              event("turn/end", {"reason": {"kind": "failed", "failure": {
                  "type": "TRANSPORT", "message": "private-secret-provider-details"}}}), status("idle")]
    result, _ = run_frames(tmp_path, monkeypatch, frames)
    assert result["error"] == "DSH_TRANSPORT_FAILED"
    assert "private-secret" not in json.dumps(result)


def test_reused_runtime_refreshes_turn_authority_and_drops_old_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-test-only")
    monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
    monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "first")
    monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "farm-a")
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "obsolete-token")
    backend = DshBackend(tmp_path)
    _, env = backend._prepare("life")
    assert env["DEEPSEEK_API_KEY"] == "fake-test-only"
    monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "second")
    monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "farm-b")
    monkeypatch.delenv("STARDEW_DECISION_TOKEN")
    backend._prepare("life")
    context = json.loads((tmp_path / "data" / "dsh-turn-life.json").read_text())
    assert context["STARDEW_LIFE_TURN_ID"] == "second"
    assert context["STARDEW_LIFE_SAVE_ID"] == "farm-b"
    assert "STARDEW_DECISION_TOKEN" not in context
    assert "fake-test-only" not in (tmp_path / "data" / "dsh-life.patch.json").read_text()
