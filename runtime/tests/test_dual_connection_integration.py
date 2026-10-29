"""Independent real-loopback checks; only the paid provider is replaced."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime import external_connection as external
from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge


@pytest.fixture
def adapter(tmp_path):
    binding = {"adapter": "mcode-acp", "providerSessionId": "original-session",
               "cwd": str(tmp_path), "originalClientIdleAcknowledged": True,
               "bindingId": "verified-original", "enabled": True,
               "clientName": "Original client", "conversationName": "Original partner"}
    server = external.AdapterServer(tmp_path, binding)
    external.write_binding(tmp_path, binding)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, binding
    finally:
        server.cancel_active()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_live_adapter_serializes_cancels_and_keeps_original_client_available(adapter, monkeypatch, tmp_path):
    server, binding = adapter
    entered = threading.Event()
    calls = []

    def run(driver, task, session, prompt):
        calls.append((session, prompt))
        entered.set()
        deadline = time.monotonic() + 5
        while not task.cancelled and time.monotonic() < deadline:
            time.sleep(.01)
        assert task.cancelled, "The real cancellation request never reached the original adapter"
        return {"success": False, "error": "CANCELLED", "conversation_id": session}

    monkeypatch.setattr(external.AcpSessionDriver, "run", run)
    backend = external.ExternalSessionBackend(tmp_path)
    task = ActiveChatTask(request_id="game-turn", save_id="farm")
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(backend.run, task, "original-session", "请先浇水")
        assert entered.wait(3)
        with pytest.raises(urllib.error.HTTPError) as rejected:
            external.call_adapter(binding, "turn", {"requestId": "second", "sessionId": "original-session", "prompt": "并发输入"})
        assert rejected.value.code == 409
        external.ExternalSessionBackend.terminate(task.process)
        assert result.result(timeout=3)["error"] == "CANCELLED"
    assert calls == [("original-session", "请先浇水")]
    assert external.call_adapter(binding, "health")["status"] == "connected"
    assert server.active is None and task.process is None


def test_retry_of_confirmed_game_message_returns_same_receipt_without_second_turn(adapter, monkeypatch, tmp_path):
    server, binding = adapter
    calls = []

    def run(driver, task, session, prompt):
        calls.append((session, prompt))
        task.provider_input_delivered = True
        return {"success": True, "conversation_id": session, "response": "已收好"}

    monkeypatch.setattr(external.AcpSessionDriver, "run", run)
    payload = {"requestId": "same-delivery", "sessionId": "original-session", "prompt": "把收成入箱"}
    first = external.call_adapter(binding, "turn", payload)
    retry = external.call_adapter(binding, "turn", payload)
    assert first == retry and first["inputDelivered"] is True
    assert calls == [("original-session", "把收成入箱")]
    conflict = external.call_adapter(binding, "turn", {**payload, "prompt": "不同的请求"})
    assert conflict["result"]["error"] == "EXTERNAL_DELIVERY_CONFLICT"
    assert len(calls) == 1
    pending = {**payload, "requestId": "interrupted-delivery"}
    server.save_receipt("original-session:interrupted-delivery",
                        {"digest": hashlib.sha256(pending["prompt"].encode()).hexdigest(), "state": "pending"})
    server.shutdown()
    server.server_close()
    restarted = external.AdapterServer(tmp_path, {**binding, "bindingId": "reconnected-original"})
    thread = threading.Thread(target=restarted.serve_forever, daemon=True)
    thread.start()
    try:
        replay = external.call_adapter(restarted.binding, "turn", payload)
        assert replay["result"] == first["result"] and replay["inputDelivered"]
        unknown = external.call_adapter(restarted.binding, "turn", pending)
        assert unknown["result"]["error"] == "EXTERNAL_DELIVERY_UNKNOWN"
        assert len(calls) == 1
    finally:
        restarted.shutdown()
        restarted.server_close()
        thread.join(timeout=3)


def test_stale_binding_never_starts_provider_and_is_not_connected(tmp_path, monkeypatch):
    server = external.AdapterServer(tmp_path, {"adapter": "mcode-acp", "providerSessionId": "original-session",
        "cwd": str(tmp_path), "originalClientIdleAcknowledged": True, "bindingId": "stale", "enabled": True})
    binding = server.binding
    server.server_close()
    external.write_binding(tmp_path, binding)
    monkeypatch.setenv("STARDEW_CONNECTION_SOURCE", "external")
    monkeypatch.setattr(external.subprocess, "Popen", lambda *a, **k: pytest.fail("Game-side bridge started a replacement provider"))
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._send_reply = AsyncMock(return_value=True)
    asyncio.run(bridge._refresh_external_connection(None))
    assert bridge._external_status == "disconnected" and not bridge._connection_ready()
    result = external.ExternalSessionBackend(tmp_path).run(ActiveChatTask(request_id="r", save_id="farm"),
                                                         "original-session", "你好")
    assert not result["success"] and result["conversation_id"] == "original-session"
    assert bridge.get_conversation_id("farm") == "original-session"


def test_explicit_source_roundtrip_preserves_separate_conversations_and_pause(adapter, tmp_path, monkeypatch):
    _, binding = adapter
    monkeypatch.delenv("STARDEW_CONNECTION_SOURCE", raising=False)
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge.record_conversation_id("farm", "builtin-session")
    bridge.actor_mode = "player"
    bridge.control_owner = "agent"
    bridge._autonomy.set_enabled("farm", False)
    bridge._send_reply = AsyncMock(return_value=True)

    async def scenario():
        await bridge._handle_player_authority(None, {"owner": "human", "reason": "human-takeover"}, "farm")
        await bridge._set_connection_source(None, {"source": "external", "requestId": "switch-1"}, "farm")
        await bridge._refresh_external_connection(None)
        assert bridge._connection_ready()
        assert bridge._life_chat.get_session_id("farm") == binding["providerSessionId"]
        bridge._life_chat.record_session_id("farm", "must-not-overwrite-builtin")
        await bridge._set_connection_source(None, {"source": "builtin", "requestId": "switch-2"}, "farm")
        assert bridge._life_chat.get_session_id("farm") == "builtin-session"
        await bridge._maybe_claim_player_control(None, "farm")
        assert bridge.control_owner == "human" and bridge._manual_takeover["farm"]
        assert bridge._autonomy.state("farm").paused
        assert not bridge._autonomy.state("farm").enabled
        assert all(call.args[1].message_type != "player.control" for call in bridge._send_reply.await_args_list)
    asyncio.run(scenario())


def test_game_text_reaches_original_session_over_http_and_real_acp_process(adapter, tmp_path, monkeypatch):
    server, binding = adapter
    binding["command"] = sys.executable
    binding["dataDir"] = str(tmp_path / "original-data")
    # The independent identity suite verifies provider metadata; this peer tests
    # process and protocol behavior and deliberately has no real account store.
    monkeypatch.setattr(external, "verify_session_identity", lambda value: {"identityFingerprint": "offline-peer"})
    external.write_binding(tmp_path, binding)
    # A real child process implements the offline ACP peer. No in-memory stream
    # or HTTP stub can hide framing, process lifetime, or workspace mistakes.
    (tmp_path / "acp").write_text('''import json, os, sys
from pathlib import Path
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
def emit(value):
    print(json.dumps({"jsonrpc":"2.0", **value}), flush=True)
with Path("wire.jsonl").open("w", encoding="utf-8") as log:
    log.write(json.dumps({"cwd":os.getcwd(), "dataDir":os.environ.get("MINIMAX_DATA_DIR"), "args":sys.argv}) + "\\n")
    log.flush()
    for line in sys.stdin:
        row=json.loads(line); log.write(json.dumps(row)+"\\n"); log.flush()
        method=row.get("method")
        if method=="initialize":
            emit({"id":row["id"], "result":{"agentCapabilities":{"loadSession":True}}})
        elif method=="session/load":
            emit({"method":"session/update", "params":{"sessionId":"original-session", "update":{"sessionUpdate":"agent_message_chunk", "content":{"type":"text", "text":"PRIVATE_HISTORY_MUST_NOT_REPLAY"}}}})
            emit({"id":row["id"], "result":{}})
        elif method=="session/prompt":
            emit({"method":"session/update", "params":{"sessionId":row["params"]["sessionId"], "update":{"sessionUpdate":"agent_message_chunk", "content":{"type":"text", "text":"GAME_REPLY"}}}})
            emit({"id":row["id"], "result":{"stopReason":"end_turn"}})
        elif method=="session/cancel":
            pass
        else:
            raise RuntimeError("Unexpected session creation or configuration override: "+str(row))
''', encoding="utf-8")
    task = ActiveChatTask(request_id="wire-turn", save_id="farm")
    result = external.ExternalSessionBackend(tmp_path).run(task, "original-session", "玩家说：先照料，保留幼树")
    assert result["success"] and result["response"] == "GAME_REPLY"
    assert result["conversation_id"] == "original-session" and task.provider_input_delivered
    rows = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["cwd"] == str(tmp_path)
    assert rows[0]["dataDir"] == binding["dataDir"]
    methods = [row["method"] for row in rows[1:]]
    assert methods[:3] == ["initialize", "session/load", "session/prompt"]
    assert set(methods) <= {"initialize", "session/load", "session/prompt", "session/cancel"}
    assert rows[2]["params"]["sessionId"] == "original-session"
    assert rows[3]["params"]["prompt"] == [{"type": "text", "text": "玩家说：先照料，保留幼树"}]
    assert not (tmp_path / ".mcp.json").exists()
    assert server.active is None
    assert external.call_adapter(binding, "health")["status"] == "connected"
