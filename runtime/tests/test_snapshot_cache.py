"""Real cache readers close Windows data handles before atomic publication."""
import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.snapshot_cache import read_cache


def test_mcp_reader_and_bridge_publication_share_cache_mutex(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    captured = time.time()
    bridge._latest_snapshot_payload = {"observedAt": captured, "world": {}, "companion": {}}
    bridge._latest_snapshot_revision = 1
    bridge._write_life_snapshot("farm")
    server = create_mcp_server(run_dir=tmp_path, surface="life")
    path = tmp_path / "data/life-snapshot.json"
    entered, release, writer_done = threading.Event(), threading.Event(), threading.Event()
    original_read = Path.read_text
    results, errors = [], []

    def held_read(target, *args, **kwargs):
        if target == path and threading.current_thread().name == "mcp-reader":
            with target.open("r", encoding="utf-8") as handle:
                entered.set()
                assert release.wait(5), "test did not release the reader"
                return handle.read()
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", held_read)

    def read():
        try:
            results.append(asyncio.run(server.call_tool("get_status", {}))[1])
        except BaseException as error:
            errors.append(error)

    def publish():
        try:
            bridge._latest_snapshot_revision = 2
            bridge._write_life_snapshot("farm")
        except BaseException as error:
            errors.append(error)
        finally:
            writer_done.set()

    reader = threading.Thread(target=read, name="mcp-reader")
    writer = threading.Thread(target=publish)
    reader.start()
    try:
        assert entered.wait(5)
        writer.start()
        assert not writer_done.wait(0.1), "publisher must wait while MCP holds the data handle"
    finally:
        release.set()
        reader.join(5)
        if writer.ident:
            writer.join(5)
    assert not reader.is_alive() and not writer.is_alive()
    assert not errors
    assert results[0]["worldRevision"] == 1
    cached = read_cache(path)
    assert cached["worldRevision"] == 2 and cached["capturedAt"] == captured


def test_new_published_lease_rejects_old_mcp_turn(tmp_path, monkeypatch):
    async def exercise():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._latest_snapshot_payload = {"observedAt": time.time(), "world": {}, "companion": {}}
        bridge._write_life_snapshot("farm")
        bridge._life_turn_lease("A", "farm")
        monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "A")
        monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "farm")
        server = create_mcp_server(run_dir=tmp_path, surface="life")
        await server.call_tool("get_status", {})
        bridge._life_turn_lease("B", "farm")
        with pytest.raises(ToolError, match="STALE_CONVERSATION"):
            await server.call_tool("manage_arrangements", {"change": {"manage_milestones": {"action": "propose", "title": "Old turn"}}})
    asyncio.run(exercise())


def test_local_publication_failure_does_not_drop_following_player_control(tmp_path, monkeypatch, caplog):
    async def exercise():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._handle_autonomy_control = AsyncMock()
        monkeypatch.setattr(bridge, "_write_life_snapshot", lambda _: (_ for _ in ()).throw(PermissionError("cache busy")))
        stop = asyncio.Event()
        common = {"protocolVersion": "0.1", "messageId": "m", "senderInstanceId": "mod",
                  "sequenceNumber": 1, "worldRevision": 7, "sentAt": "2026-10-07T00:00:00Z",
                  "saveId": "farm", "gameSessionId": "g"}
        frames = iter([
            {**common, "messageType": "world.snapshot", "payload": {"observedAt": 123, "world": {}}},
            {**common, "messageType": "autonomy.control", "payload": {"action": "pause", "requestId": "pause-B"}},
        ])

        class Socket:
            async def receive_text(self, timeout=None):
                try:
                    return json.dumps(next(frames))
                except StopIteration:
                    stop.set()
                    raise TimeoutError() from None

            async def send_text(self, text):
                pass

        await bridge._receive_loop(Socket(), "farm", stop)
        bridge._handle_autonomy_control.assert_awaited_once()
        assert bridge._latest_snapshot_payload["observedAt"] == 123

    asyncio.run(exercise())
    assert "Life snapshot cache publication failed (PermissionError)" in caplog.text
