"""World changes steer a live turn or resume its exact session after joining it."""
import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from support.mcp_scheduler import _grant_decision
from support.mcp_scheduler import mock_scheduler as _scheduler_fixture

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.turn_messages import day_event, exchange

mock_scheduler = _scheduler_fixture


def test_day_change_publishes_once_without_rotating_original_session(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._active_task = ActiveChatTask("turn", "farm")
    bridge.record_conversation_id("farm", "original")
    bridge._latest_snapshot_payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 2},
                                       "companion": {"stamina": 136, "availableMoney": 700}}
    async def run():
        await bridge._settle_day_advance("farm", {"year": 1, "season": "spring", "dayOfMonth": 1})
        await bridge._settle_day_advance("farm", bridge._latest_snapshot_payload["world"])
        await bridge._settle_day_advance("farm", bridge._latest_snapshot_payload["world"])
    asyncio.run(run())
    messages = exchange(tmp_path / "data/turn-messages.json", "turn", "farm", pending_world=True)
    assert len(messages) == 1 and messages[0]["companion"]["availableMoney"] == 700
    assert bridge.get_conversation_id("farm") == "original"
    assert bridge._active_task.world_change_version == 1


@pytest.mark.parametrize("takeover", [False, True])
def test_missing_tool_boundary_joins_before_same_session_supplement(tmp_path, takeover):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._apply_life_intent = AsyncMock()
        bridge._observe_world_continuation = AsyncMock(return_value={"lastAction": {"completedCount": 20}, "receiptKnown": True})
        bridge.world_steering_timeout_seconds = .01
        active = bridge._active_task = ActiveChatTask("turn", "farm")
        active.world_change_at = time.monotonic()
        inbox = tmp_path / "data/turn-messages.json"
        exchange(inbox, "turn", "farm", publish=[day_event("1:spring:2", {"world": {"dayOfMonth": 2}})])
        exited = threading.Event()
        calls = []
        @asynccontextmanager
        async def pause(*args):
            yield
        def model(task, session, prompt):
            calls.append((session, prompt))
            if len(calls) == 1:
                limit = time.monotonic() + 3
                while not task.provider_interrupted and time.monotonic() < limit:
                    time.sleep(.002)
                assert task.provider_interrupted
                if takeover:
                    task.cancelled = True
                    bridge.control_owner = "human"
                exited.set()
                return {"success": False, "error": "CANCELLED", "conversation_id": session}
            assert exited.is_set() and session == "original"
            assert "completedCount" in prompt and "original long task" not in prompt
            return {"success": True, "conversation_id": session, "response": "continued"}
        bridge._thinking_pause = pause
        result = await bridge._await_provider(active, model, "original", "original long task")
        assert len(calls) == (1 if takeover else 2)
        if takeover:
            bridge._observe_world_continuation.assert_not_awaited()
            assert not result["success"]
        else:
            assert result["success"]
            assert not exchange(inbox, "turn", "farm", pending_world=True)
    asyncio.run(run())


def test_world_result_is_not_consumed_until_next_tool_or_confirmed_supplement(tmp_path):
    path = tmp_path / "inbox.json"
    event = day_event("1:spring:2", {"world": {"dayOfMonth": 2}})
    exchange(path, "turn", "farm", publish=[event])
    assert exchange(path, "turn", "farm", take=True) == [event]
    assert exchange(path, "turn", "farm") == []
    assert exchange(path, "turn", "farm", pending_world=True) == [event]
    exchange(path, "turn", "farm", publish=[event])
    assert exchange(path, "turn", "farm", take=True) == []
    assert exchange(path, "turn", "farm", ack_world=True) == [event]
    assert not exchange(path, "turn", "farm", pending_world=True)


def test_cross_day_tool_returns_new_facts_then_executes_without_old_decision(tmp_path, monkeypatch, mock_scheduler):
    store = _grant_decision(tmp_path, mock_scheduler)
    sid = "mock-save-123"
    monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "turn")
    monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", sid)
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision-1")
    (tmp_path / "data/life-turn.json").write_text(json.dumps({"requestId": "turn", "saveId": sid}))
    store.settle_game_day(sid, year=1, season="spring", day=1)
    store.settle_game_day(sid, year=1, season="spring", day=2)
    assert not store.state(sid).decision
    inbox = tmp_path / "data/turn-messages.json"
    exchange(inbox, "turn", sid, publish=[day_event("1:spring:2", {"world": {"dayOfMonth": 2}})])
    mock_scheduler.execute_tiles = AsyncMock(return_value={"terminalState": "succeeded", "completedCount": 5})
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        args = {"action": {"water_tiles": {"tiles": [{"x": 4, "y": 5}]}}}
        _, result = await server.call_tool("submit_work", args)
        assert result["actionExecuted"] is False
        assert result["worldEvents"][0]["world"]["dayOfMonth"] == 2
        mock_scheduler.execute_tiles.assert_not_awaited()
        _, result = await server.call_tool("submit_work", args)
        assert result["completedCount"] == 5
        mock_scheduler.execute_tiles.assert_awaited_once()
        assert not exchange(inbox, "turn", sid, pending_world=True)
    asyncio.run(run())


def test_new_day_acquires_new_thinking_lease_and_takeover_stops_renewal(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge.actor_mode = "player"
        bridge.control_owner = "agent"
        bridge._active_task = ActiveChatTask("turn", "farm")
        bridge.thinking_renew_interval_seconds = .001
        messages = []
        async def send(ws, reply):
            messages.append(reply.payload)
            return True
        bridge._send_reply = send
        async with bridge._thinking_pause("turn", "farm"):
            await asyncio.sleep(.005)
            bridge._active_task.world_change_version = 1
            await asyncio.sleep(.005)
            bridge.control_owner = "human"
            await asyncio.sleep(.005)
            count = len(messages)
            await asyncio.sleep(.005)
            assert len(messages) == count
        begins = [m["leaseId"] for m in messages if m["state"] == "begin"]
        assert len(set(begins)) == 2
        assert messages[-1]["state"] == "end" and messages[-1]["leaseId"] == begins[-1]
    asyncio.run(run())


def test_work_turn_has_same_live_identity_as_chat_and_releases_it_after_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._apply_life_intent = AsyncMock()
    active = bridge._active_task = ActiveChatTask("work-turn", "farm")
    class Backend:
        def run(self, task, session, prompt):
            assert os.environ["STARDEW_LIFE_TURN_ID"] == task.request_id
            assert os.environ["STARDEW_LIFE_SAVE_ID"] == task.save_id
            lease = json.loads((tmp_path / "data/life-turn.json").read_text())
            assert lease["requestId"] == "work-turn" and lease["saveId"] == "farm"
            return {"success": True, "conversation_id": session}
    bridge._backend = Backend()
    before = os.environ.get("STARDEW_LIFE_TURN_ID")
    result = asyncio.run(bridge._await_work_provider(active, "original", "water remaining crops"))
    assert result["success"]
    assert json.loads((tmp_path / "data/life-turn.json").read_text())["requestId"] is None
    assert os.environ.get("STARDEW_LIFE_TURN_ID") == before


@pytest.mark.parametrize("delivered", [False, True])
def test_world_interruption_advances_context_only_with_delivery_receipt(tmp_path, monkeypatch, delivered):
    from unittest.mock import Mock
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    ack = Mock(wraps=bridge._acknowledge_prompt_delivery)
    bridge._acknowledge_prompt_delivery = ack
    task = ActiveChatTask("turn", "farm")
    class Backend:
        def run(self, active, session, prompt):
            active.provider_interrupted = True
            active.provider_input_delivered = delivered
            # Some adapters report cancellation without a separate error code.
            return {"success": False, "conversation_id": session}
    bridge._backend = Backend()
    bridge._execute_turn(task, "original", "continue")
    assert ack.call_count == int(delivered)
