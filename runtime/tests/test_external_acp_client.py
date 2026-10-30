"""Offline ACP transcript boundaries; never starts a model provider."""
import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from stardew_ai_runtime import external_connection as connection
from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge


def binding(tmp_path):
    return {"version": 1, "adapter": "mcode-acp", "providerSessionId": "original-session",
            "cwd": str(tmp_path), "clientName": "MiniMax Code", "conversationName": "Original partner",
            "command": "fake-mcode", "bindingId": "binding", "originalClientIdleAcknowledged": True,
            "enabled": True}


def transcript(*, permission=False):
    def result(i, value):
        return {"jsonrpc": "2.0", "id": i, "result": value}
    def update(text):
        return {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "original-session", "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text}}}}
    rows = [result(1, {"agentCapabilities": {"loadSession": True}}), update("PRIVATE OLD HISTORY"), result(2, {})]
    if permission:
        rows.append({"jsonrpc": "2.0", "id": 91, "method": "session/request_permission", "params": {}})
    else:
        rows.append(update("菜已经收好。"))
    rows.append(result(3, {"stopReason": "end_turn"}))
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def fake_process(monkeypatch, wire):
    calls = []
    process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(wire), stderr=io.StringIO(), poll=lambda: 0)
    def launch(command, **kwargs):
        calls.append((command, kwargs))
        return process
    monkeypatch.setattr(connection.subprocess, "Popen", launch)
    monkeypatch.setattr(connection.McodeBackend, "_executable", lambda self: "fake-mcode")
    monkeypatch.setattr(connection, "verify_session_identity", lambda binding: {"identityFingerprint": "fixture"})
    return process, calls


def test_original_acp_session_keeps_environment_and_does_not_replay_private_history(tmp_path, monkeypatch):
    original = binding(tmp_path)
    original["dataDir"] = str(tmp_path / "original-memory")
    process, calls = fake_process(monkeypatch, transcript())
    task = SimpleNamespace(cancelled=False, process=None, request_id="game-1")
    driver = connection.AcpSessionDriver(tmp_path, original, context={"STARDEW_DECISION_TOKEN": "turn-authority"})
    result = driver.run(task, "original-session", "玩家说：收菜")
    assert result["success"] and result["conversation_id"] == "original-session"
    assert result["response"] == "菜已经收好。" and task.provider_input_delivered
    command, options = calls[0]
    assert command == ["fake-mcode", "acp"]
    assert options["cwd"] == str(tmp_path)
    assert options["env"]["MINIMAX_DATA_DIR"] == original["dataDir"]
    sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert [row["method"] for row in sent] == ["initialize", "session/load", "session/prompt"]
    assert sent[1]["params"]["sessionId"] == "original-session"
    assert sent[1]["params"]["cwd"] == str(tmp_path)
    server = sent[1]["params"]["mcpServers"][0]
    assert server["name"] == "stardew-companion" and server["args"][-2:] == ["--surface", "light"]
    assert not (tmp_path / ".mcp.json").exists()
    assert "PRIVATE OLD HISTORY" not in json.dumps(result)


def test_original_permission_request_is_specific_failure_without_permission_escalation(tmp_path, monkeypatch):
    process, _ = fake_process(monkeypatch, transcript(permission=True))
    task = SimpleNamespace(cancelled=False, process=None, request_id="game-1")
    result = connection.AcpSessionDriver(tmp_path, binding(tmp_path)).run(task, "original-session", "收菜")
    assert not result["success"] and result["error"] == "EXTERNAL_PERMISSION_REQUIRED"
    answer = next(json.loads(line) for line in process.stdin.getvalue().splitlines() if '"id": 91' in line)
    assert answer["result"]["outcome"]["outcome"] == "cancelled"


def test_external_life_and_work_share_exact_binding_without_default_persona_or_delta(tmp_path, monkeypatch):
    monkeypatch.setenv("STARDEW_CONNECTION_SOURCE", "external")
    connection.write_binding(tmp_path, binding(tmp_path))
    bridge = ChatBridge(run_dir=tmp_path, backend="kimi", enable_plan_worker=False)
    bridge._sessions["mcode:farm"] = "builtin-partner"
    assert bridge._life_chat.get_session_id("farm") == "original-session"
    bridge._life_chat.record_session_id("farm", "original-session")
    assert bridge._sessions["mcode:farm"] == "builtin-partner"
    task = ActiveChatTask(request_id="game-chat", save_id="farm")
    prompt = bridge._life_chat.build_turn_prompt("farm", "original-session",
        {"companionName": "DEFAULT_REPLACEMENT_PERSONA", "personality": "tsundere"}, {}, None,
        mode="chat", milestones=None, live_context=bridge._decision_context("farm")) + "\n玩家说：保留幼树"
    key, first, pending = bridge._prepare_prompt_delivery(task, "original-session", prompt, None)
    bridge._acknowledge_prompt_delivery(key, pending, "farm", "original-session")
    _, second, _ = bridge._prepare_prompt_delivery(task, "original-session", prompt, None)
    assert "DEFAULT_REPLACEMENT_PERSONA" not in first and "嘴硬心软" not in first
    assert "保留幼树" in first and "原对话" in first
    assert "上下文增量：" not in second and "实时上下文：" in second


def test_game_message_waits_for_original_client_without_starting_a_model(tmp_path, monkeypatch):
    monkeypatch.setenv("STARDEW_CONNECTION_SOURCE", "external")
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._send_reply = AsyncMock(return_value=True)
    original_submit = bridge.handle_chat_submit
    async def run():
        await original_submit(None, "player-one", "请收菜放箱子", "farm")
        await original_submit(None, "player-one", "请收菜放箱子", "farm")
        assert len(bridge._life_queue) == 1 and bridge._backend is None
        await bridge._drain_life_queue(None)
        assert len(bridge._life_queue) == 1
        connection.write_binding(tmp_path, binding(tmp_path))
        bridge._external_status = "connected"
        bridge.handle_chat_submit = AsyncMock()
        await bridge._drain_life_queue(None)
        bridge.handle_chat_submit.assert_awaited_once_with(None, "player-one", "请收菜放箱子", "farm")
        assert not bridge._life_queue
    asyncio.run(run())


def test_connection_cli_survives_windows_legacy_redirect_encoding(tmp_path):
    environment = dict(os.environ, PYTHONIOENCODING="cp1252",
                       PYTHONPATH=str(Path(connection.__file__).resolve().parents[1]))
    result = subprocess.run([sys.executable, "-m", "stardew_ai_runtime.external_connection",
        "--run-dir", str(tmp_path), "--disconnect"], env=environment, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "游戏连接已断开；原客户端与原会话保留。" in result.stdout.decode("utf-8")
