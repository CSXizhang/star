"""Discovery alone must never grant authority to mutate persistent work."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.websocket_client import WebSocketError


@pytest.mark.parametrize("connected", [False, True])
def test_decision_revocation_requires_successful_chat_connection(tmp_path, monkeypatch, connected):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        store = bridge._work_store
        store.add_goal("farm", "保留已接受的工作", source="user")
        store.begin_decision("farm", "still-valid-token")
        before = (tmp_path / "data" / "work-state.json").read_bytes()
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
            assert store.state("farm").scheduler_epoch == before_epoch + 1
            assert store.state("farm").decision == {}
            ws.close.assert_awaited_once()
        else:
            worker.start.assert_not_called()
            bridge._notify_plan_worker.assert_not_called()
            assert (tmp_path / "data" / "work-state.json").read_bytes() == before
            assert store.state("farm").decision["token"] == "still-valid-token"
    asyncio.run(scenario())
