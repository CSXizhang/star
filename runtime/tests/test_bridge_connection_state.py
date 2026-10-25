"""Discovery alone must never grant authority to mutate persistent work."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.websocket_client import WebSocketError


@pytest.mark.parametrize("connected", [False, True])
def test_successful_chat_connection_restarts_worker_and_never_mutates_saved_work(tmp_path, monkeypatch, connected):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        store = bridge._work_store
        store.add_goal("farm", "保留已接受的工作", source="user")
        store.begin_decision("farm", "still-valid-token")
        before = store.state("farm").goals
        before_epoch = store.state("farm").scheduler_epoch
        worker = SimpleNamespace(start=Mock(), stop=AsyncMock(), notify=Mock(), supplied_save_id=None)
        bridge._plan_worker = worker
        bridge._notify_plan_worker = Mock()
        stop = asyncio.Event()
        ws = SimpleNamespace(close=AsyncMock())

        async def connect(**kwargs):
            if not connected:
                stop.set()
                raise WebSocketError("stale endpoint refused connection")
            return ws

        async def receive(*args):
            stop.set()

        monkeypatch.setattr("stardew_ai_runtime.chat_bridge.resolve_discovery", lambda *a, **k: {
            "host": "127.0.0.1", "port": 12345, "sessionToken": "fake-test", "saveId": "farm"})
        monkeypatch.setattr("stardew_ai_runtime.chat_bridge.WebSocketClient.connect", connect)
        monkeypatch.setattr("stardew_ai_runtime.chat_bridge.asyncio.sleep", AsyncMock())
        bridge._receive_loop = receive
        await bridge.run(stop)
        if connected:
            worker.start.assert_called_once()
            # A bridge reconnect in the same game session retains the persisted
            # authorized selection; only a real native reload revokes it.
            assert store.state("farm").scheduler_epoch == before_epoch
            assert not store.state("farm").decision
            assert store.state("farm").goals == before
            ws.close.assert_awaited_once()
        else:
            worker.start.assert_not_called()
            bridge._notify_plan_worker.assert_not_called()
            assert store.state("farm").goals == before
            assert not store.state("farm").decision
            assert store.state("farm").goals == before
    asyncio.run(scenario())


def test_player_takeover_keeps_goal_and_requires_explicit_handoff(tmp_path):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge.actor_mode = "player"
        bridge.control_owner = "agent"
        goal = bridge._work_store.add_goal("farm", "持续经营，不丢失进度", source="user")
        bridge._autonomy.set_enabled("farm", True)
        bridge._work_store.begin_decision("farm", "old-token")
        ws = SimpleNamespace(send_text=AsyncMock())
        await bridge._handle_player_authority(ws, {"owner": "human", "reason": "human-takeover"}, "farm")
        assert bridge._work_store.state("farm").goals[0].id == goal.id
        assert bridge._work_store.state("farm").goals[0].status == "active"
        assert bridge._autonomy.state("farm").paused
        await bridge._maybe_claim_player_control(ws, "farm")
        ws.send_text.assert_not_awaited()
        await bridge._handle_player_authority(ws, {"owner": "agent", "reason": "human-explicit-handoff"}, "farm")
        assert not bridge._autonomy.state("farm").paused
        assert bridge._work_store.state("farm").goals[0].status == "active"
    asyncio.run(scenario())
