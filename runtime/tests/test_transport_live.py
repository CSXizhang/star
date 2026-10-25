"""Live local mock-transport tests for Stage 0 protocol and client.

NOTE: This is a local protocol integration test between the Python client
and a mock WebSocket server simulating the Companion Mod transport contract.
THIS IS CLEARLY NOT REAL GAME EVIDENCE.
"""

from __future__ import annotations

import asyncio

import pytest

from support.mock_mod_server import MockModTransportServer

from stardew_ai_runtime.client import TransportClient
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.websocket_client import WebSocketError

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def test_live_mock_unauthorized_rejected() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="invalid-token",
            )
            with pytest.raises(WebSocketError, match="401"):
                await client.connect()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_handshake_and_snapshot() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            welcome = await client.handshake()

            assert welcome.message_type == "mod.welcome"
            assert welcome.save_id == "mock-save-hash-123"
            assert "water-zone" in welcome.payload["skills"]

            snapshot = await client.wait_for_snapshot()
            assert snapshot.message_type == "world.snapshot"
            assert snapshot.payload["companion"]["locationId"] == "Farm"
            assert snapshot.payload["companion"]["stamina"] == 270.0

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_execute_water_zone() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            tiles = [{"x": 64, "y": 15}, {"x": 64, "y": 16}]
            cmd_id = await client.execute_water_zone(
                location_id="Farm",
                tiles=tiles,
                max_game_minutes=30,
                max_stamina=40.0,
                max_water=10,
            )

            result = await client.wait_for_result(cmd_id)
            assert result.message_type == "skill.result"
            assert result.payload["terminalState"] == "succeeded"
            assert result.payload["completedCount"] == 2
            assert result.payload["resources"]["waterUsed"] == 2

            assert len(server.executed_commands) == 1
            assert server.executed_commands[0]["commandId"] == cmd_id

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_cancel_skill() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            cmd_id = "cmd-to-cancel"
            await client.cancel_skill(
                command_id=cmd_id,
                correlation_id="msg-orig-001",
                reason="User requested stop",
            )

            result = await client.wait_for_result(cmd_id)
            assert result.message_type == "skill.result"
            assert result.payload["terminalState"] == "cancelled"

            assert len(server.cancelled_commands) == 1
            assert server.cancelled_commands[0]["commandId"] == cmd_id

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_pause_and_resume() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            await client.pause_skill(
                command_id="cmd-pause-001",
                correlation_id="msg-orig-002",
                reason="Dialogue open",
            )
            await asyncio.sleep(0.05)
            assert len(server.paused_commands) == 1

            await client.resume_skill(
                command_id="cmd-pause-001",
                correlation_id="msg-orig-002",
            )
            await asyncio.sleep(0.05)
            assert len(server.resumed_commands) == 1

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_reconcile_after_new_client_recovers_terminal_without_execute(tmp_path) -> None:
    async def run():
        server = MockModTransportServer()
        server.supports_result_query = True
        await server.start()
        clients = []
        try:
            async def connect():
                client = TransportClient(port=server.port, session_token=server.session_token)
                clients.append(client)
                await client.connect()
                await client.handshake()
                await client.wait_for_snapshot()
                return client

            original = await connect()
            command_id = await original.execute_water_zone("Farm", [{"x": 64, "y": 15}])
            receipt = await original.wait_for_result(command_id)
            await original.close()
            restarted = await connect()
            scheduler = CompanionScheduler(run_dir=tmp_path, client=restarted)
            recovered = await scheduler.reconcile_command(command_id)
            assert recovered == receipt.payload
            assert len(server.executed_commands) == 1
            assert len(server.result_queries) == 1
            assert server.result_queries[0]["saveId"] == server.save_id
            assert server.result_queries[0]["gameSessionId"] == server.game_session_id
        finally:
            for client in clients:
                await client.close()
            await server.stop()

    asyncio.run(run())


@pytest.mark.parametrize("query_supported", [True, False])
def test_reconcile_missing_receipt_keeps_unknown_and_never_dispatches(tmp_path, query_supported):
    async def run():
        server = MockModTransportServer()
        server.supports_result_query = query_supported
        await server.start()
        client = TransportClient(port=server.port, session_token=server.session_token)
        try:
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()
            scheduler = CompanionScheduler(run_dir=tmp_path, client=client)
            assert await scheduler.reconcile_command("unknown-command", timeout=0.05) is None
            assert len(server.result_queries) == int(query_supported)
            assert not server.executed_commands
        finally:
            await client.close()
            await server.stop()

    asyncio.run(run())


def test_live_mock_duplicate_replay() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            tiles = [{"x": 64, "y": 15}]
            cmd_id1 = await client.execute_water_zone(
                location_id="Farm",
                tiles=tiles,
                command_id="cmd-dup-1",
                task_id="task-dup-1",
                idempotency_key="key-dup-1",
            )
            res1 = await client.wait_for_result(cmd_id1)
            assert res1.payload["terminalState"] == "succeeded"
            assert res1.payload["completedCount"] == 1
            assert len(server.executed_commands) == 1

            # Duplicate execute with identical parameters and key:
            cmd_id2 = await client.execute_water_zone(
                location_id="Farm",
                tiles=tiles,
                command_id="cmd-dup-1",
                task_id="task-dup-1",
                idempotency_key="key-dup-1",
                world_revision=1,
            )
            assert cmd_id2 == cmd_id1
            res2 = await client.wait_for_result(cmd_id2)
            assert res2.payload["terminalState"] == "succeeded"
            assert res2.payload["completedCount"] == 1
            # Replay did not re-execute in domain:
            assert len(server.executed_commands) == 1

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_idempotency_conflict_preserves_original() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            tiles1 = [{"x": 64, "y": 15}]
            cmd_id = await client.execute_water_zone(
                location_id="Farm",
                tiles=tiles1,
                command_id="cmd-conflict-1",
                task_id="task-conflict-1",
                idempotency_key="key-conflict-shared",
            )
            res1 = await client.wait_for_result(cmd_id)
            assert res1.payload["terminalState"] == "succeeded"

            # Conflicting execute with different tiles but SAME idempotency key:
            tiles2 = [{"x": 99, "y": 99}]
            await client.execute_water_zone(
                location_id="Farm",
                tiles=tiles2,
                command_id="cmd-conflict-2",
                task_id="task-conflict-2",
                idempotency_key="key-conflict-shared",
            )
            res2 = await client.wait_for_result("cmd-conflict-2")
            assert res2.payload["terminalState"] == "rejected"
            assert res2.payload["error"] == "IDEMPOTENCY_CONFLICT"

            # Original executed command remains intact and unaffected:
            assert len(server.executed_commands) == 1
            assert server.executed_commands[0]["commandId"] == "cmd-conflict-1"

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_cancel_before_execute() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        server.delay_execution = True
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            tiles = [{"x": 64, "y": 15}]
            cmd_id = await client.execute_water_zone(
                location_id="Farm",
                tiles=tiles,
                command_id="cmd-queued-1",
                task_id="task-queued-1",
                idempotency_key="key-queued-1",
            )

            # Cancel before execution runs:
            await client.cancel_skill(
                command_id=cmd_id,
                correlation_id="msg-exec-queued",
                reason="Pre-execution abort",
            )

            res = await client.wait_for_result(cmd_id)
            assert res.payload["terminalState"] == "cancelled"
            # Queue was purged; command never executed in domain:
            assert len(server.executed_commands) == 0
            assert len(server.pending_commands) == 0

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_execute_harvest_zone() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            welcome = await client.handshake()
            assert "harvest-zone" in welcome.payload["skills"]
            assert "deposit-chest" in welcome.payload["skills"]
            assert "organize-chest" in welcome.payload["skills"]
            await client.wait_for_snapshot()

            tiles = [{"x": 64, "y": 15}, {"x": 65, "y": 15}]
            cmd_id = await client.execute_harvest_zone(
                location_id="Farm",
                tiles=tiles,
            )

            result = await client.wait_for_result(cmd_id)
            assert result.message_type == "skill.result"
            assert result.payload["terminalState"] == "succeeded"
            assert result.payload["completedCount"] == 2

            assert len(server.executed_commands) == 1
            executed = server.executed_commands[0]
            assert executed["commandId"] == cmd_id
            assert executed["skillId"] == "harvest-zone"
            assert executed["skillVersion"] == "0.1"
            assert executed["parameters"]["tiles"] == tiles

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_execute_deposit_chest() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            cmd_id = await client.execute_deposit_chest(
                location_id="Farm",
                chest_x=70,
                chest_y=12,
                item_ids=["(O)24", "(O)188"],
            )

            result = await client.wait_for_result(cmd_id)
            assert result.message_type == "skill.result"
            assert result.payload["terminalState"] == "succeeded"

            assert len(server.executed_commands) == 1
            executed = server.executed_commands[0]
            assert executed["commandId"] == cmd_id
            assert executed["skillId"] == "deposit-chest"
            assert executed["parameters"]["chestTile"] == {"x": 70, "y": 12}
            assert executed["parameters"]["itemIds"] == ["(O)24", "(O)188"]
            assert "tiles" not in executed["parameters"]

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_execute_organize_chest() -> None:
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            cmd_id = await client.execute_organize_chest(
                location_id="Farm",
                chest_x=70,
                chest_y=12,
            )

            result = await client.wait_for_result(cmd_id)
            assert result.message_type == "skill.result"
            assert result.payload["terminalState"] == "succeeded"

            assert len(server.executed_commands) == 1
            executed = server.executed_commands[0]
            assert executed["commandId"] == cmd_id
            assert executed["skillId"] == "organize-chest"
            assert executed["parameters"]["chestTile"] == {"x": 70, "y": 12}
            assert "itemIds" not in executed["parameters"]
            assert "tiles" not in executed["parameters"]

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_live_mock_new_skills_duplicate_replay() -> None:
    """Duplicate replay (same key + parameters) does not re-execute for the new skills."""
    async def run() -> None:
        server = MockModTransportServer(session_token="test-secret-token")
        await server.start()
        try:
            client = TransportClient(
                host="127.0.0.1",
                port=server.port,
                session_token="test-secret-token",
            )
            await client.connect()
            await client.handshake()
            await client.wait_for_snapshot()

            # harvest-zone duplicate replay
            tiles = [{"x": 64, "y": 15}]
            cmd_id = await client.execute_harvest_zone(
                location_id="Farm",
                tiles=tiles,
                command_id="cmd-dup-h1",
                task_id="task-dup-h1",
                idempotency_key="key-dup-h1",
            )
            res1 = await client.wait_for_result(cmd_id)
            assert res1.payload["terminalState"] == "succeeded"
            cmd_id2 = await client.execute_harvest_zone(
                location_id="Farm",
                tiles=tiles,
                command_id="cmd-dup-h1",
                task_id="task-dup-h1",
                idempotency_key="key-dup-h1",
                world_revision=1,
            )
            res2 = await client.wait_for_result(cmd_id2)
            assert res2.payload["terminalState"] == "succeeded"
            assert res2.payload["completedCount"] == 1
            assert len(server.executed_commands) == 1

            # deposit-chest duplicate replay (itemIds order-insensitive fingerprint)
            cmd_id = await client.execute_deposit_chest(
                location_id="Farm",
                chest_x=70,
                chest_y=12,
                item_ids=["(O)24", "(O)188"],
                command_id="cmd-dup-d1",
                task_id="task-dup-d1",
                idempotency_key="key-dup-d1",
            )
            res1 = await client.wait_for_result(cmd_id)
            assert res1.payload["terminalState"] == "succeeded"
            cmd_id2 = await client.execute_deposit_chest(
                location_id="Farm",
                chest_x=70,
                chest_y=12,
                item_ids=["(O)188", "(O)24"],
                command_id="cmd-dup-d1",
                task_id="task-dup-d1",
                idempotency_key="key-dup-d1",
                world_revision=2,
            )
            res2 = await client.wait_for_result(cmd_id2)
            assert res2.payload["terminalState"] == "succeeded"
            assert len(server.executed_commands) == 2

            # organize-chest duplicate replay
            cmd_id = await client.execute_organize_chest(
                location_id="Farm",
                chest_x=70,
                chest_y=12,
                command_id="cmd-dup-o1",
                task_id="task-dup-o1",
                idempotency_key="key-dup-o1",
            )
            res1 = await client.wait_for_result(cmd_id)
            assert res1.payload["terminalState"] == "succeeded"
            cmd_id2 = await client.execute_organize_chest(
                location_id="Farm",
                chest_x=70,
                chest_y=12,
                command_id="cmd-dup-o1",
                task_id="task-dup-o1",
                idempotency_key="key-dup-o1",
                world_revision=3,
            )
            res2 = await client.wait_for_result(cmd_id2)
            assert res2.payload["terminalState"] == "succeeded"
            assert len(server.executed_commands) == 3

            await client.close()
        finally:
            await server.stop()

    asyncio.run(run())
