"""Fault injection against the real socket reader; these are not gameplay evidence."""
import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from test_websocket_receive_timeout import frame

from stardew_ai_runtime.client import TransportClient
from stardew_ai_runtime.protocol import Envelope
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.websocket_client import WebSocketClient


def envelope(kind="world.snapshot", revision=1, **payload):
    return Envelope(protocol_version="0.1", message_type=kind, message_id=f"{kind}-{revision}",
                    sender_instance_id="mod", sequence_number=revision, world_revision=revision,
                    sent_at=datetime.now(UTC), payload=payload, save_id="save", game_session_id="game")


def socket():
    reader, writer = asyncio.StreamReader(), Mock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer, WebSocketClient(reader, writer)


def feed(reader, *envelopes):
    reader.feed_data(b"".join(frame(json.dumps(e.to_mapping()).encode()) for e in envelopes))


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_idle_receiver_coalesces_snapshots_without_losing_receipts(monkeypatch):
    async def run():
        reader, _, ws = socket()
        monkeypatch.setattr(WebSocketClient, "connect", AsyncMock(return_value=ws))
        client = TransportClient()
        await client.connect()
        try:
            feed(reader, envelope("mod.welcome"))
            await client.handshake()
            for revision in range(2, 102):
                feed(reader, envelope(revision=revision, world={"timeOfDay": 600 + revision}))
                if revision in {25, 75}:
                    feed(reader, envelope("skill.result", revision, commandId=f"cmd-{revision}", terminalState="succeeded"))
            await until(lambda: client.latest_snapshot is not None and client.latest_snapshot.world_revision == 101)
            # No caller polled during the burst; only two receipts and one latest
            # snapshot remain, not an ever-growing backlog of world observations.
            assert len(client._incoming) == 2
            for revision in (25, 75):
                result = await client.wait_for_result(f"cmd-{revision}", timeout=1)
                assert result.payload["terminalState"] == "succeeded"
            assert (await client.wait_for_snapshot()).world_revision == 101
        finally:
            await client.close()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    asyncio.run(run())


def test_reset_is_not_cached_success_and_reconnect_does_not_replay_action(tmp_path, monkeypatch):
    async def run():
        first_reader, _, first_ws = socket()
        second_reader, _, second_ws = socket()
        monkeypatch.setattr(WebSocketClient, "connect", AsyncMock(side_effect=[first_ws, second_ws]))
        feed(first_reader, envelope("mod.welcome"), envelope(world={"timeOfDay": 600}))
        feed(second_reader, envelope("mod.welcome", 2), envelope(revision=2, world={"timeOfDay": 700}))
        scheduler = CompanionScheduler(run_dir=tmp_path, discovery_resolver=lambda _: {
            "host": "localhost", "port": 1, "sessionToken": "test"})
        sent = []
        original_send = TransportClient.send_envelope

        async def send(client, message):
            sent.append(message)
            await original_send(client, message)

        monkeypatch.setattr(TransportClient, "send_envelope", send)
        try:
            first = await scheduler.ensure_connected()
            await first.execute_native_action("toggle-animal-door", {"locationId": "Farm", "tiles": [{"x": 5, "y": 5}]},
                                              command_id="uncertain-action")
            first_reader.set_exception(ConnectionResetError(10054, "peer reset"))
            await until(lambda: not first.is_connected)
            with pytest.raises(ConnectionResetError):
                await first.wait_for_result("uncertain-action", timeout=1)
            with pytest.raises(ConnectionResetError):
                await scheduler._refresh_snapshot(first)
            status = await scheduler.get_status()
            assert status["world"]["timeOfDay"] == 700
            assert status["observedAt"]
            assert status["saveId"] == "save" and status["gameSessionId"] == "game"
            assert [e.message_type for e in sent] == ["runtime.hello", "skill.execute", "runtime.hello"]
        finally:
            await scheduler.close()
    asyncio.run(run())


def test_send_reset_invalidates_connection(monkeypatch):
    async def run():
        _, writer, ws = socket()
        writer.drain.side_effect = ConnectionResetError(10054, "peer reset")
        monkeypatch.setattr(WebSocketClient, "connect", AsyncMock(return_value=ws))
        client = TransportClient()
        await client.connect()
        try:
            with pytest.raises(ConnectionResetError):
                await client.handshake()
            assert not client.is_connected
            assert writer.close.called
        finally:
            await client.close()
    asyncio.run(run())


def test_parallel_observation_cannot_steal_action_receipt(monkeypatch):
    async def run():
        reader, _, ws = socket()
        monkeypatch.setattr(WebSocketClient, "connect", AsyncMock(return_value=ws))
        client = TransportClient()
        await client.connect()
        try:
            observation = asyncio.create_task(client.wait_for_snapshot(timeout=1))
            action = asyncio.create_task(client.wait_for_result("active", timeout=1))
            second_observer = asyncio.create_task(client.wait_for_result("active", timeout=1))
            await until(lambda: len(client._result_waiters.get("active", ())) == 2)
            feed(reader, envelope("skill.result", commandId="active", terminalState="succeeded"),
                 envelope(revision=2, world={"timeOfDay": 700}))
            snapshot, receipt, same_receipt = await asyncio.gather(observation, action, second_observer)
            assert snapshot.world_revision == 2
            assert receipt is same_receipt and receipt.payload["terminalState"] == "succeeded"
        finally:
            await client.close()
    asyncio.run(run())
