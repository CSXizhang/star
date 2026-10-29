import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from support.mcp_scheduler import _grant_decision
from support.mcp_scheduler import mock_scheduler as _scheduler_fixture

from stardew_ai_runtime.mcp_server import create_mcp_server

mock_scheduler = _scheduler_fixture


@pytest.fixture(autouse=True)
def companion_identity(tmp_path, monkeypatch):
    config = tmp_path / "backend.json"
    config.write_text(json.dumps({"backend": "mcode"}))
    monkeypatch.setenv("STARDEW_CHAT_BACKEND_CONFIG", str(config))
    monkeypatch.setenv("STARDEW_CONNECTION_SOURCE", "builtin")
    (tmp_path / "chat_sessions.json").write_text(json.dumps({"mcode:mock-save-123": "original-session"}))


def test_memory_and_method_survive_server_restart_and_can_be_corrected(tmp_path, mock_scheduler):
    mock_scheduler.run_dir = str(tmp_path)

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        await server.call_tool("remember", {"entry": {"save_memory": {
            "text": "雨天想和玩家去海边，今天先休息。", "scope": "共同约定"}}})
        await server.call_tool("remember", {"entry": {"save_method": {
            "title": "收获整理", "text": "先看当前收成和可用空间；玩家已收走的部分不用重复处理。"}}})
        restarted = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        _, recalled = await restarted.call_tool("consult", {"request": {"recall_memory": {"query": "雨天 海边"}}})
        note = next(n for n in recalled["notes"] if "雨天" in n["text"])
        _, update = await restarted.call_tool("remember", {"entry": {"save_memory": {
            "note_id": note["id"], "text": "雨天有空再去海边，不固定安排。", "scope": "共同约定"}}})
        revised_id = update["note"]["id"]
        _, corrected = await restarted.call_tool("consult", {"request": {"recall_memory": {"note_id": revised_id}}})
        assert corrected["notes"][0]["text"] == "雨天有空再去海边，不固定安排。"
        _, methods = await restarted.call_tool("consult", {"request": {"recall_memory": {"kind": "method"}}})
        assert any(n["title"] == "收获整理" for n in methods["notes"])
        await restarted.call_tool("remember", {"entry": {"forget_memory": {"note_id": revised_id}}})
        _, remaining = await restarted.call_tool("consult", {"request": {"recall_memory": {"query": "雨天 海边"}}})
        assert not any(n["id"] == note["id"] for n in remaining["notes"])
        assert not (tmp_path / "data/work-state.json").exists(), "Notes must not create executable work"

    asyncio.run(run())


def test_notes_can_change_while_work_paused_but_not_from_stale_turn(tmp_path, monkeypatch, mock_scheduler):
    mock_scheduler.run_dir = str(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "current")
    monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "mock-save-123")
    lease = data / "life-turn.json"
    lease.write_text(json.dumps({"requestId": "current", "saveId": "mock-save-123", "workBlocked": True}))

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        await server.call_tool("remember", {"entry": {"save_memory": {"text": "今天只聊天。"}}})
        before = (data / "companion-memory.json").read_bytes()
        lease.write_text(json.dumps({"requestId": "newer", "saveId": "mock-save-123"}))
        with pytest.raises(ToolError, match="STALE_CONVERSATION"):
            await server.call_tool("remember", {"entry": {"save_memory": {"text": "过时回合的修改"}}})
        assert (data / "companion-memory.json").read_bytes() == before

    asyncio.run(run())


def test_pickup_returns_changed_world_without_inferring_inventory_attribution(tmp_path, monkeypatch, mock_scheduler):
    _grant_decision(tmp_path, mock_scheduler)
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision-1")
    mock_scheduler.pickup_items = AsyncMock(return_value={
        "status": "completed", "terminalState": "succeeded", "completedCount": 0,
        "skippedCount": 1, "effects": [{"reasonCode": "no-item"}]})
    mock_scheduler.query_production = AsyncMock(return_value={"locationId": "Farm", "groundItems": [], "groundItemsTruncated": False})
    # The player may have taken items. This observation is not an action delta.
    mock_scheduler.query_inventory = AsyncMock(return_value={"items": [{"itemId": "(O)388", "stack": 2}]})

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        _, result = await server.call_tool("submit_work", {"action": {"pickup_items": {"tiles": [{"x": 4, "y": 5}]}}})
        assert result["completedCount"] == 0 and result["skippedCount"] == 1
        assert result["afterWork"]["ground"]["groundItems"] == []
        assert result["afterWork"]["inventory"]["items"][0]["stack"] == 2
        assert "not an inventory-delta receipt" in result["afterWork"]["interpretation"]

    asyncio.run(run())


def test_first_memory_call_waits_for_the_actual_provider_session(tmp_path, monkeypatch, mock_scheduler):
    mock_scheduler.run_dir = str(tmp_path)
    sessions = tmp_path / "chat_sessions.json"
    sessions.write_text("{}")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "first")
    monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "mock-save-123")
    (data / "life-turn.json").write_text(json.dumps({"requestId": "first", "saveId": "mock-save-123",
        "memoryProvider": "mcode", "memoryConnectionSource": "builtin"}))

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        async def provider_started():
            await asyncio.sleep(.08)
            sessions.write_text(json.dumps({"mcode:mock-save-123": "actual-first-session"}))
        arrival = asyncio.create_task(provider_started())
        _, saved = await server.call_tool("remember", {"entry": {"save_memory": {"text": "叫玩家小禾。"}}})
        await arrival
        assert saved["status"] == "confirmed"
        _, recalled = await server.call_tool("consult", {"request": {"recall_memory": {"query": "小禾"}}})
        assert recalled["notes"][0]["id"] == saved["note"]["id"]
    asyncio.run(run())
