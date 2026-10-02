"""Exercise long model turns over real local TCP WebSocket frames, without a game/model."""
import asyncio
import json
import struct
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.websocket_client import WebSocketClient


def _message(kind, request_id, **payload):
    return {
        "protocolVersion": "0.1", "messageType": kind, "messageId": request_id,
        "senderInstanceId": "mod", "sequenceNumber": 0, "worldRevision": 1,
        "sentAt": "2026-10-02T00:00:00Z", "saveId": "Save1", "gameSessionId": "test",
        "payload": {"requestId": request_id, "saveId": "Save1", **payload},
    }


@asynccontextmanager
async def _wire(bridge):
    connected = asyncio.get_running_loop().create_future()

    async def accept(reader, writer):
        connected.set_result(WebSocketClient(reader, writer))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    client = WebSocketClient(reader, writer)
    peer = await connected
    receiving = asyncio.create_task(bridge._receive_loop(client, "Save1"))

    async def send(value):
        raw = json.dumps(value).encode("utf-8")
        if len(raw) < 126:
            header = bytes([0x81, len(raw)])
        elif len(raw) <= 65535:
            header = bytes([0x81, 126]) + struct.pack("!H", len(raw))
        else:
            header = bytes([0x81, 127]) + struct.pack("!Q", len(raw))
        peer._writer.write(header + raw)
        await peer._writer.drain()

    async def until(predicate):
        async with asyncio.timeout(2):
            while True:
                reply = json.loads(await peer.receive_text())
                if predicate(reply):
                    return reply

    try:
        yield send, until, receiving
    finally:
        receiving.cancel()
        await asyncio.wait_for(asyncio.gather(receiving, return_exceptions=True), 3)
        await client.close()
        await peer.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("mode", ["chat", "plan"])
def test_long_life_turn_keeps_reading_snapshots_and_profile_and_fifo_queue(tmp_path, monkeypatch, mode):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        release = threading.Event()
        calls = []

        def model(task, _cid, _prompt, _mode="chat"):
            calls.append(task.request_id)
            if task.request_id == "first":
                assert release.wait(3)
            return {"success": True, "response": "reply", "conversation_id": None}

        monkeypatch.setattr(bridge, "_execute_life_turn", model)
        try:
            async with _wire(bridge) as (send, until, _receiving):
                await send(_message("life.chat.submit", "first", mode=mode, text="hello"))
                await until(lambda r: r["payload"].get("status") == "processing")
                # Drain a snapshot larger than StreamReader's buffer during model thinking.
                await send(_message("world.snapshot", "snapshot", world={
                    "year": 1, "season": "spring", "dayOfMonth": 16, "timeOfDay": 600,
                    "observations": "x" * 300_000,
                }))
                await send(_message("life.profile.get", "profile"))
                await until(lambda r: r["messageType"] == "life.profile.state"
                            and r["payload"].get("requestId") == "profile")
                assert not release.is_set()
                assert bridge._latest_snapshot_payload["world"]["dayOfMonth"] == 16
                await send(_message("life.chat.submit", "second", mode=mode, text="next"))
                queued = await until(lambda r: r["payload"].get("requestId") == "second")
                assert queued["payload"]["status"] == "queued"
                assert calls == ["first"]
                release.set()
                await until(lambda r: r["payload"].get("requestId") == "first"
                            and r["payload"].get("status") == "completed")
                await until(lambda r: r["payload"].get("requestId") == "second"
                            and r["payload"].get("status") == "completed")
                assert calls == ["first", "second"]
        finally:
            release.set()

    asyncio.run(scenario())


def test_channel_shutdown_cancels_owned_life_provider_before_releasing_slot(tmp_path, monkeypatch):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        started = threading.Event()
        exited = threading.Event()
        killed = threading.Event()
        proc = SimpleNamespace(poll=lambda: None, kill=killed.set)
        bridge._backend = SimpleNamespace(terminate=lambda _proc: killed.set())

        def model(task, _cid, _prompt, _mode):
            task.process = proc
            started.set()
            assert killed.wait(3)
            assert task.cancelled
            exited.set()
            return {"success": False}

        monkeypatch.setattr(bridge, "_execute_life_turn", model)
        async with _wire(bridge) as (send, until, receiving):
            await send(_message("life.chat.submit", "interrupted", mode="chat", text="hello"))
            await until(lambda r: r["payload"].get("status") == "processing")
            assert await asyncio.to_thread(started.wait, 1)
            receiving.cancel()
            await asyncio.wait_for(asyncio.gather(receiving, return_exceptions=True), 2)
            assert killed.is_set() and exited.is_set()
            assert not bridge._busy_lock.locked()
            assert not bridge._execution_lock.locked()
            assert bridge._chat_ws is None

    asyncio.run(scenario())


def test_morning_care_model_does_not_block_wire_reader(tmp_path, monkeypatch):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._profile_store.set("Save1", {"onboarded": True, "careFrequency": "chatty"}, 0)
        release = threading.Event()
        started = threading.Event()

        def model(task, _cid, _prompt, _mode="chat"):
            started.set()
            assert release.wait(3)
            return {"success": True, "response": "morning"}

        monkeypatch.setattr(bridge, "_execute_life_turn", model)
        try:
            async with _wire(bridge) as (send, until, _receiving):
                await send(_message("world.snapshot", "morning", world={
                    "year": 1, "season": "spring", "dayOfMonth": 16, "timeOfDay": 600,
                }))
                assert await asyncio.to_thread(started.wait, 1)
                await send(_message("life.profile.get", "profile"))
                await until(lambda r: r["messageType"] == "life.profile.state"
                            and r["payload"].get("requestId") == "profile")
                await send(_message("life.chat.submit", "during-care", mode="chat", text="hello"))
                queued = await until(lambda r: r["payload"].get("requestId") == "during-care")
                assert queued["payload"]["status"] == "queued"
                release.set()
                await until(lambda r: r["messageType"] == "life.care")
                await until(lambda r: r["payload"].get("requestId") == "during-care"
                            and r["payload"].get("status") == "completed")
        finally:
            release.set()

    asyncio.run(scenario())
