"""Unit tests for CompanionScheduler and policy validation.

Tests:
1. Coordinate expansion & bounds checking (radius 0, 1, 2).
2. Discovery resolution and loopback security validation.
3. Single active task policy (rejection of concurrent tasks).
4. Idempotency key generation and lifecycle tracking.
5. Task control (pause, resume, cancel) state transitions and idle rejections.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.protocol import Envelope
from stardew_ai_runtime.scheduler import (
    CompanionScheduler,
    DiscoveryError,
    NoActiveTaskError,
    PolicyViolationError,
    SchedulerError,
    expand_water_zone,
    extract_chest_summary,
    resolve_discovery,
    validate_action_tiles,
    validate_seed_item_id,
)


def test_expand_water_zone_radius_0():
    tiles = expand_water_zone(64, 15, radius=0)
    assert tiles == [{"x": 64, "y": 15}]


def test_expand_water_zone_radius_1():
    tiles = expand_water_zone(64, 15, radius=1)
    assert len(tiles) == 9
    # Row-major ordering: sorted by y, then x
    assert tiles[0] == {"x": 63, "y": 14}
    assert tiles[1] == {"x": 64, "y": 14}
    assert tiles[2] == {"x": 65, "y": 14}
    assert tiles[3] == {"x": 63, "y": 15}
    assert tiles[4] == {"x": 64, "y": 15}
    assert tiles[5] == {"x": 65, "y": 15}
    assert tiles[6] == {"x": 63, "y": 16}
    assert tiles[7] == {"x": 64, "y": 16}
    assert tiles[8] == {"x": 65, "y": 16}


def test_expand_water_zone_radius_2():
    tiles = expand_water_zone(64, 15, radius=2)
    assert len(tiles) == 25
    assert tiles[0] == {"x": 62, "y": 13}
    assert tiles[-1] == {"x": 66, "y": 17}


def test_expand_water_zone_near_origin_filters_negatives():
    # Center (0, 0) with radius 1: (-1,-1), (-1,0), etc. must be filtered out
    tiles = expand_water_zone(0, 0, radius=1)
    # Only (0,0), (1,0), (0,1), (1,1) are non-negative
    assert len(tiles) == 4
    assert tiles == [
        {"x": 0, "y": 0},
        {"x": 1, "y": 0},
        {"x": 0, "y": 1},
        {"x": 1, "y": 1},
    ]


@pytest.mark.parametrize("invalid_radius", [-1, 3, 5, 10, "1", 1.5, True, False])
def test_expand_water_zone_invalid_radius(invalid_radius):
    with pytest.raises(PolicyViolationError, match="radius"):
        expand_water_zone(64, 15, radius=invalid_radius)


@pytest.mark.parametrize("invalid_coord", [-1, -10, "64", 64.5, True, False])
def test_expand_water_zone_invalid_coordinates(invalid_coord):
    with pytest.raises(PolicyViolationError):
        expand_water_zone(invalid_coord, 15, radius=0)
    with pytest.raises(PolicyViolationError):
        expand_water_zone(64, invalid_coord, radius=0)


def test_resolve_discovery_from_mod_data(tmp_path: Path):
    mod_data = tmp_path / "mods" / "StardewAI.Companion.Mod" / "data"
    mod_data.mkdir(parents=True)
    discovery_file = mod_data / "transport-discovery.json"
    discovery_file.write_text(
        json.dumps({
            "host": "127.0.0.1",
            "port": 61979,
            "sessionToken": "test-secret-token-123",
            "saveId": "save-448732481",
            "gameSessionId": "session-abc-1",
        }),
        encoding="utf-8",
    )

    data = resolve_discovery(tmp_path)
    assert data["host"] == "127.0.0.1"
    assert data["port"] == 61979
    assert data["sessionToken"] == "test-secret-token-123"
    assert data["saveId"] == "save-448732481"
    assert data["gameSessionId"] == "session-abc-1"


def test_resolve_discovery_missing_file_raises(tmp_path: Path):
    with pytest.raises(DiscoveryError, match="not found"):
        resolve_discovery(tmp_path)


def test_resolve_discovery_non_loopback_rejected(tmp_path: Path):
    disc = tmp_path / "transport-discovery.json"
    disc.write_text(
        json.dumps({
            "host": "192.168.1.50",
            "port": 61979,
            "sessionToken": "token",
        }),
        encoding="utf-8",
    )
    with pytest.raises(DiscoveryError, match="Non-loopback host"):
        resolve_discovery(tmp_path)


def test_resolve_discovery_missing_token_rejected(tmp_path: Path):
    disc = tmp_path / "transport-discovery.json"
    disc.write_text(
        json.dumps({
            "host": "127.0.0.1",
            "port": 61979,
            "sessionToken": "",
        }),
        encoding="utf-8",
    )
    with pytest.raises(DiscoveryError, match="sessionToken"):
        resolve_discovery(tmp_path)


def test_resolve_discovery_none_auto_discovers_from_env_run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STARDEW_RUN_DIR", str(tmp_path))
    disc = tmp_path / "data" / "transport-discovery.json"
    disc.parent.mkdir(parents=True, exist_ok=True)
    disc.write_text(
        json.dumps({
            "host": "127.0.0.1",
            "port": 50001,
            "sessionToken": "env-token-1",
            "saveId": "save-env-1",
        }),
        encoding="utf-8",
    )
    res = resolve_discovery(None)
    assert res["port"] == 50001
    assert res["sessionToken"] == "env-token-1"
    assert res["saveId"] == "save-env-1"
    assert res["modDir"] == str(tmp_path)


def test_resolve_discovery_none_auto_discovers_from_env_game_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STARDEW_RUN_DIR", raising=False)
    monkeypatch.setenv("STARDEW_GAME_PATH", str(tmp_path))
    mod_data = tmp_path / "Mods" / "StardewAI.Companion.Mod" / "data"
    mod_data.mkdir(parents=True, exist_ok=True)
    disc = mod_data / "transport-discovery.json"
    disc.write_text(
        json.dumps({
            "host": "127.0.0.1",
            "port": 50002,
            "sessionToken": "game-token-2",
            "saveId": "save-game-2",
        }),
        encoding="utf-8",
    )
    res = resolve_discovery(None)
    assert res["port"] == 50002
    assert res["sessionToken"] == "game-token-2"
    assert res["saveId"] == "save-game-2"


def test_resolve_discovery_none_ignores_test_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_run = tmp_path / ".test-runs" / "run-001"
    monkeypatch.setenv("STARDEW_RUN_DIR", str(test_run))
    disc = test_run / "data" / "transport-discovery.json"
    disc.parent.mkdir(parents=True, exist_ok=True)
    disc.write_text(
        json.dumps({
            "host": "127.0.0.1",
            "port": 50003,
            "sessionToken": "test-run-token",
            "saveId": "test-run-save",
        }),
        encoding="utf-8",
    )
    monkeypatch.delenv("STARDEW_GAME_PATH", raising=False)
    with pytest.raises(DiscoveryError):
        resolve_discovery(None)


def test_resolve_discovery_none_missing_raises_friendly_discovery_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STARDEW_RUN_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.delenv("STARDEW_GAME_PATH", raising=False)
    with pytest.raises(DiscoveryError, match="not found"):
        resolve_discovery(None)


@pytest.fixture
def mock_transport_client():
    client = MagicMock()
    client.is_connected = True
    client.save_id = "mock-save-id-001"
    client.game_session_id = "mock-session-001"
    client.world_revision = 5
    client.connect = AsyncMock()
    client.handshake = AsyncMock()
    client.close = AsyncMock()

    snapshot_env = Envelope.from_mapping({
        "protocolVersion": "0.1",
        "messageType": "world.snapshot",
        "messageId": "snap-1",
        "saveId": "mock-save-id-001",
        "gameSessionId": "mock-session-001",
        "senderInstanceId": "mod-instance-001",
        "sequenceNumber": 1,
        "worldRevision": 5,
        "sentAt": "2026-09-12T00:00:00Z",
        "payload": {
            "capturedRevision": 5,
            "companion": {
                "locationId": "Farm",
                "tileX": 64,
                "tileY": 15,
                "facingDirection": 2,
                "stamina": 250.0,
                "maxStamina": 270.0,
                "waterCanLevel": 38,
                "maxWaterCanLevel": 40,
                "hasWateringCan": True,
                "activity": "idle",
            },
            "world": {
                "currentLocation": "Farm",
                "timeOfDay": 610,
                "season": "spring",
                "dayOfMonth": 1,
                "isRaining": False,
            },
        },
    })
    client.latest_snapshot = snapshot_env
    client.wait_for_snapshot = AsyncMock(return_value=snapshot_env)

    return client


def test_scheduler_get_status(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        status = await scheduler.get_status()

        assert status["companion"]["locationId"] == "Farm"
        assert status["companion"]["tileX"] == 64
        assert status["companion"]["tileY"] == 15
        assert status["companion"]["stamina"] == 250.0
        assert status["companion"]["waterCanLevel"] == 38
        assert status["companion"]["activity"] == "idle"
        assert status["companion"]["currentTask"] is None
        assert status["world"]["timeOfDay"] == 610
        assert status["saveId"] == "mock-save-id-001"
        assert status["gameSessionId"] == "mock-session-001"
        assert status["worldRevision"] == 5

    asyncio.run(run())


def test_scheduler_single_active_task_policy(mock_transport_client, native_compatible_run_dir):
    """Enforces that concurrent task execution is rejected."""
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        wait_event = asyncio.Event()

        async def delayed_result(command_id, timeout=10.0):
            await wait_event.wait()
            return Envelope.from_mapping({
                "protocolVersion": "0.1",
                "messageType": "skill.result",
                "messageId": "res-1",
                "saveId": "mock-save-id-001",
                "gameSessionId": "mock-session-001",
                "senderInstanceId": "mod-instance-001",
                "sequenceNumber": 2,
                "worldRevision": 6,
                "sentAt": "2026-09-12T00:00:05Z",
                "payload": {
                    "commandId": command_id,
                    "taskId": "task-1",
                    "terminalState": "succeeded",
                    "completedCount": 1,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "effects": [{"tile": {"x": 64, "y": 15}, "state": "watered"}],
                },
            })

        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-water-001")
        mock_transport_client.wait_for_result = AsyncMock(side_effect=delayed_result)

        # Start first task
        first_task = asyncio.create_task(
            scheduler.execute_water_zone(center_x=64, center_y=15, radius=0)
        )

        # Yield control to let first_task begin execution
        await asyncio.sleep(0.01)
        assert scheduler.has_active_task
        assert scheduler.active_task.skill_id == "water-zone"
        assert scheduler.active_task.status == "running"

        # Second concurrent task MUST be rejected by policy
        with pytest.raises(PolicyViolationError, match="Concurrent tasks are not permitted"):
            await scheduler.execute_water_zone(center_x=65, center_y=15, radius=0)

        # Complete the first task
        wait_event.set()
        result = await first_task

        assert result["terminalState"] == "succeeded"
        assert result["completedCount"] == 1
        assert not scheduler.has_active_task

    asyncio.run(run())


def test_scheduler_idempotency_key_generation(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        result_env = Envelope.from_mapping({
            "protocolVersion": "0.1",
            "messageType": "skill.result",
            "messageId": "res-1",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 2,
            "worldRevision": 6,
            "sentAt": "2026-09-12T00:00:05Z",
            "payload": {
                "commandId": "cmd-water-001",
                "taskId": "task-custom-01",
                "terminalState": "succeeded",
                "completedCount": 9,
                "skippedCount": 0,
                "failedCount": 0,
                "effects": [],
            },
        })
        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-water-001")
        mock_transport_client.wait_for_result = AsyncMock(return_value=result_env)

        await scheduler.execute_water_zone(
            center_x=64, center_y=15, radius=1, task_id="task-custom-01"
        )

        call_kwargs = mock_transport_client.execute_water_zone.call_args.kwargs
        assert call_kwargs["idempotency_key"] == "mock-save-id-001:task-custom-01:attempt-1"
        assert call_kwargs["task_id"] == "task-custom-01"
        assert len(call_kwargs["tiles"]) == 9

    asyncio.run(run())


def test_scheduler_control_actions_when_idle_raises(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)

        with pytest.raises(NoActiveTaskError, match="No active task to pause"):
            await scheduler.pause_task()

        with pytest.raises(NoActiveTaskError, match="No active task to resume"):
            await scheduler.resume_task()

        with pytest.raises(NoActiveTaskError, match="No active task to cancel"):
            await scheduler.cancel_task()

    asyncio.run(run())


def test_scheduler_pause_resume_cancel_lifecycle(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        wait_event = asyncio.Event()

        async def wait_side_effect(command_id, timeout=10.0):
            await wait_event.wait()
            return Envelope.from_mapping({
                "protocolVersion": "0.1",
                "messageType": "skill.result",
                "messageId": "res-1",
                "saveId": "mock-save-id-001",
                "gameSessionId": "mock-session-001",
                "senderInstanceId": "mod-instance-001",
                "sequenceNumber": 5,
                "worldRevision": 7,
                "sentAt": "2026-09-12T00:00:10Z",
                "payload": {
                    "commandId": command_id,
                    "taskId": "task-test",
                    "terminalState": "cancelled",
                    "completedCount": 0,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "effects": [],
                },
            })

        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-water-001")
        mock_transport_client.wait_for_result = AsyncMock(side_effect=wait_side_effect)
        mock_transport_client.pause_skill = AsyncMock()
        mock_transport_client.resume_skill = AsyncMock()
        mock_transport_client.cancel_skill = AsyncMock()

        task_coro = asyncio.create_task(
            scheduler.execute_water_zone(center_x=64, center_y=15, radius=0, task_id="task-test")
        )
        await asyncio.sleep(0.01)

        # Pause
        pause_res = await scheduler.pause_task()
        assert pause_res["status"] == "paused"
        assert scheduler.active_task.status == "paused"
        mock_transport_client.pause_skill.assert_awaited_once()

        # Resume
        resume_res = await scheduler.resume_task()
        assert resume_res["status"] == "resumed"
        assert scheduler.active_task.status == "running"
        mock_transport_client.resume_skill.assert_awaited_once()

        # Cancel
        cancel_res = await scheduler.cancel_task(reason="Test cancel")
        assert cancel_res["status"] == "cancelling"
        assert scheduler.active_task.status == "cancelling"
        mock_transport_client.cancel_skill.assert_awaited_once()

        # Release waiting task
        wait_event.set()
        final_res = await task_coro
        assert final_res["terminalState"] == "cancelled"
        assert not scheduler.has_active_task

    asyncio.run(run())


def test_scheduler_query_farm_work_with_tiles(mock_transport_client):
    async def run():
        # Inject farmWork into snapshot
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}, {"x": 64, "y": 16}],
            "tilledUnwateredCount": 2,
            "isTruncated": False,
            "matureCropCount": 3,
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        result = await scheduler.query_farm_work()

        farm_work = result["farmWork"]
        assert len(farm_work["tilledUnwateredTiles"]) == 2
        assert farm_work["tilledUnwateredCount"] == 2
        assert farm_work["isTruncated"] is False
        assert farm_work["matureCropCount"] == 3
        assert result["companion"]["locationId"] == "Farm"
        assert result["world"]["timeOfDay"] == 610

    asyncio.run(run())


def test_scheduler_query_farm_work_empty(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [],
            "tilledUnwateredCount": 0,
            "isTruncated": False,
            "matureCropCount": 0,
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        result = await scheduler.query_farm_work()

        farm_work = result["farmWork"]
        assert farm_work["tilledUnwateredTiles"] == []
        assert farm_work["tilledUnwateredCount"] == 0
        assert farm_work["isTruncated"] is False

    asyncio.run(run())


def test_scheduler_execute_tiles_validation(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)

        # Empty list
        with pytest.raises(PolicyViolationError, match="non-empty"):
            await scheduler.execute_tiles([])

        # Exceeds 64
        too_many = [{"x": i, "y": 0} for i in range(65)]
        with pytest.raises(PolicyViolationError, match="exceeds maximum"):
            await scheduler.execute_tiles(too_many)

        # Invalid tile format
        with pytest.raises(PolicyViolationError, match="invalid tile format"):
            await scheduler.execute_tiles([{"x": 1}])

        # Negative coord
        with pytest.raises(PolicyViolationError, match="non-negative"):
            await scheduler.execute_tiles([{"x": -1, "y": 5}])

        # Non-int coord
        with pytest.raises(PolicyViolationError, match="integers"):
            await scheduler.execute_tiles([{"x": 1.5, "y": 5}])

    asyncio.run(run())


@pytest.mark.parametrize("invalid_max", [0, -1, 65, 100, "25", 25.5, True, False])
def test_scheduler_water_auto_invalid_max_tiles(mock_transport_client, invalid_max):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(PolicyViolationError, match="max_tiles"):
            await scheduler.water_auto(max_tiles=invalid_max)

    asyncio.run(run())


def test_scheduler_water_auto_no_work(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [],
            "tilledUnwateredCount": 0,
            "isTruncated": False,
            "matureCropCount": 0,
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        mock_transport_client.execute_water_zone = AsyncMock()

        res = await scheduler.water_auto(max_tiles=25)

        assert res["status"] == "no-work"
        assert res["targetTiles"] == []
        assert res["targetCount"] == 0
        assert res["remainingUnwateredCount"] == 0
        assert res["terminalState"] == "none"
        mock_transport_client.execute_water_zone.assert_not_called()

    asyncio.run(run())


def test_scheduler_water_auto_with_work_within_budget(mock_transport_client, native_compatible_run_dir):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [
                {"x": 64, "y": 15},
                {"x": 65, "y": 15},
                {"x": 66, "y": 15},
            ],
            "tilledUnwateredCount": 3,
            "isTruncated": False,
            "matureCropCount": 1,
        }
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        result_env = Envelope.from_mapping({
            "protocolVersion": "0.1",
            "messageType": "skill.result",
            "messageId": "res-1",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 2,
            "worldRevision": 6,
            "sentAt": "2026-09-12T00:00:05Z",
            "payload": {
                "commandId": "cmd-auto-1",
                "taskId": "task-auto-1",
                "terminalState": "succeeded",
                "completedCount": 3,
                "skippedCount": 0,
                "failedCount": 0,
                "effects": [{"tile": {"x": 64, "y": 15}, "state": "watered"}],
            },
        })
        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-auto-1")
        mock_transport_client.wait_for_result = AsyncMock(return_value=result_env)

        res = await scheduler.water_auto(max_tiles=25)

        assert res["status"] == "executed"
        assert res["terminalState"] == "succeeded"
        assert res["targetCount"] == 3
        assert res["remainingUnwateredCount"] == 0
        assert res["completedCount"] == 3
        mock_transport_client.execute_water_zone.assert_awaited_once()

    asyncio.run(run())


def test_scheduler_water_auto_compact_snapshot_targets_crops_only(mock_transport_client, native_compatible_run_dir):
    async def run():
        # The compact query used by water_auto must retain native crop-only
        # candidates; the empty tilled tile is an intentional negative target.
        crop_tiles = [{"x": 61, "y": 21}, {"x": 62, "y": 21}]
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": crop_tiles + [{"x": 65, "y": 15}],
            "tilledUnwateredCount": 3,
            "cropUnwateredTiles": crop_tiles,
            "cropUnwateredCount": 2,
            "isTruncated": False,
            "matureCropCount": 0,
        }
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)
        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-crop-only")
        mock_transport_client.wait_for_result = AsyncMock(return_value=Envelope.from_mapping({
            "protocolVersion": "0.1",
            "messageType": "skill.result",
            "messageId": "res-crop-only",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 2,
            "worldRevision": 6,
            "sentAt": "2026-09-12T00:00:05Z",
            "payload": {
                "commandId": "cmd-crop-only",
                "terminalState": "succeeded",
                "completedCount": 2,
                "skippedCount": 0,
                "failedCount": 0,
                "effects": [],
            },
        }))

        result = await scheduler.water_auto(max_tiles=25)

        assert result["targetTiles"] == crop_tiles
        assert result["targetCount"] == 2
        assert result["remainingUnwateredCount"] == 0
        mock_transport_client.execute_water_zone.assert_awaited_once()
        assert mock_transport_client.execute_water_zone.await_args.kwargs["tiles"] == crop_tiles

    asyncio.run(run())


def test_scheduler_water_auto_with_truncation(mock_transport_client, native_compatible_run_dir):
    async def run():
        # 10 unwatered tiles, max_tiles=4
        tiles = [{"x": 60 + i, "y": 10} for i in range(10)]
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": tiles,
            "tilledUnwateredCount": 10,
            "isTruncated": False,
            "matureCropCount": 0,
        }
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        result_env = Envelope.from_mapping({
            "protocolVersion": "0.1",
            "messageType": "skill.result",
            "messageId": "res-1",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 2,
            "worldRevision": 6,
            "sentAt": "2026-09-12T00:00:05Z",
            "payload": {
                "commandId": "cmd-auto-2",
                "taskId": "task-auto-2",
                "terminalState": "succeeded",
                "completedCount": 4,
                "skippedCount": 0,
                "failedCount": 0,
                "effects": [],
            },
        })
        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-auto-2")
        mock_transport_client.wait_for_result = AsyncMock(return_value=result_env)

        res = await scheduler.water_auto(max_tiles=4)

        assert res["status"] == "executed"
        assert res["targetCount"] == 4
        assert res["remainingUnwateredCount"] == 6
        assert res["isTruncated"] is True
        # Verify first 4 tiles were targeted
        assert res["targetTiles"] == tiles[:4]

    asyncio.run(run())


def test_scheduler_water_auto_concurrent_rejection(mock_transport_client, native_compatible_run_dir):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}],
            "tilledUnwateredCount": 1,
            "isTruncated": False,
            "matureCropCount": 0,
        }
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        wait_event = asyncio.Event()

        async def delayed_result(command_id, timeout=10.0):
            await wait_event.wait()
            return Envelope.from_mapping({
                "protocolVersion": "0.1",
                "messageType": "skill.result",
                "messageId": "res-1",
                "saveId": "mock-save-id-001",
                "gameSessionId": "mock-session-001",
                "senderInstanceId": "mod-instance-001",
                "sequenceNumber": 2,
                "worldRevision": 6,
                "sentAt": "2026-09-12T00:00:05Z",
                "payload": {
                    "commandId": command_id,
                    "taskId": "task-1",
                    "terminalState": "succeeded",
                    "completedCount": 1,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "effects": [],
                },
            })

        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-auto-1")
        mock_transport_client.wait_for_result = AsyncMock(side_effect=delayed_result)

        first_task = asyncio.create_task(scheduler.water_auto(max_tiles=10))
        await asyncio.sleep(0.01)

        assert scheduler.has_active_task

        # Second water_auto call must be rejected
        with pytest.raises(PolicyViolationError, match="Concurrent tasks are not permitted"):
            await scheduler.water_auto(max_tiles=10)

        wait_event.set()
        await first_task
        assert not scheduler.has_active_task

    asyncio.run(run())




def _make_skill_result_envelope(
    command_id: str = "cmd-test",
    task_id: str = "task-test",
    terminal_state: str = "succeeded",
    completed: int = 1,
    skipped: int = 0,
    failed: int = 0,
    effects: list | None = None,
    details: dict | None = None,
    error: object = None,
) -> Envelope:
    payload: dict = {
        "commandId": command_id,
        "taskId": task_id,
        "terminalState": terminal_state,
        "completedCount": completed,
        "skippedCount": skipped,
        "failedCount": failed,
        "effects": effects or [],
    }
    if details is not None:
        payload["details"] = details
    if error is not None:
        payload["error"] = error
    return Envelope.from_mapping({
        "protocolVersion": "0.1",
        "messageType": "skill.result",
        "messageId": "res-new-1",
        "saveId": "mock-save-id-001",
        "gameSessionId": "mock-session-001",
        "senderInstanceId": "mod-instance-001",
        "sequenceNumber": 9,
        "worldRevision": 6,
        "sentAt": "2026-09-12T00:00:05Z",
        "payload": payload,
    })


@pytest.mark.parametrize("invalid_max", [0, -1, 65, 100, "16", 16.5, True, False])
def test_scheduler_harvest_auto_invalid_max_tiles(mock_transport_client, invalid_max):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(PolicyViolationError, match="max_tiles"):
            await scheduler.harvest_auto(max_tiles=invalid_max)

    asyncio.run(run())


def test_scheduler_harvest_auto_no_work(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [],
            "tilledUnwateredCount": 0,
            "isTruncated": False,
            "matureCropCount": 0,
            "matureCrops": [],
            "matureCropsTruncated": False,
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        mock_transport_client.execute_harvest_zone = AsyncMock()

        res = await scheduler.harvest_auto(max_tiles=16)

        assert res["status"] == "no-work"
        assert res["targetTiles"] == []
        assert res["targetCount"] == 0
        assert res["remainingMatureCount"] == 0
        assert res["terminalState"] == "none"
        mock_transport_client.execute_harvest_zone.assert_not_called()

    asyncio.run(run())


def test_scheduler_harvest_auto_executed(mock_transport_client, native_compatible_run_dir):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [],
            "tilledUnwateredCount": 0,
            "isTruncated": False,
            "matureCropCount": 5,
            "matureCrops": [
                {"x": 64, "y": 15, "cropId": "(O)24"},
                {"x": 65, "y": 15, "cropId": "(O)188"},
                {"x": 64, "y": 16, "cropId": "(O)192"},
            ],
            "matureCropsTruncated": True,
        }
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        result_env = _make_skill_result_envelope(
            command_id="cmd-harvest-1",
            task_id="task-harvest-test-1",
            terminal_state="partially-succeeded",
            completed=2,
            effects=[
                {
                    "tile": {"x": 64, "y": 15},
                    "state": "harvested",
                    "cropId": "(O)24",
                    "itemId": "(O)24",
                    "itemName": "Parsnip",
                    "stack": 1,
                    "quality": 0,
                }
            ],
            details={"inventoryFull": True},
        )
        mock_transport_client.execute_harvest_zone = AsyncMock(return_value="cmd-harvest-1")
        mock_transport_client.wait_for_result = AsyncMock(return_value=result_env)

        res = await scheduler.harvest_auto(max_tiles=2, task_id="task-harvest-test-1")

        assert res["status"] == "executed"
        assert res["terminalState"] == "partially-succeeded"
        assert res["completedCount"] == 2
        assert res["skippedCount"] == 0
        assert res["failedCount"] == 0
        # First max_tiles crops (Y-X order as published by the mod) are targeted
        assert res["targetTiles"] == [{"x": 64, "y": 15}, {"x": 65, "y": 15}]
        assert res["targetCount"] == 2
        # Pre-count (5) minus targeted (2) estimate
        assert res["remainingMatureCount"] == 3
        assert res["isTruncated"] is True
        assert res["effects"][0]["state"] == "harvested"
        assert res["details"] == {"inventoryFull": True}
        assert res["error"] is None

        call_kwargs = mock_transport_client.execute_harvest_zone.call_args.kwargs
        assert call_kwargs["tiles"] == [{"x": 64, "y": 15}, {"x": 65, "y": 15}]
        assert call_kwargs["task_id"] == "task-harvest-test-1"
        assert (
            call_kwargs["idempotency_key"]
            == "mock-save-id-001:task-harvest-test-1:attempt-1"
        )

    asyncio.run(run())


def test_scheduler_harvest_auto_task_id_prefix(mock_transport_client, native_compatible_run_dir):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "matureCropCount": 1,
            "matureCrops": [{"x": 64, "y": 15, "cropId": "(O)24"}],
            "matureCropsTruncated": False,
        }
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        async def capture_execute(**kwargs):
            return "cmd-harvest-2"

        async def result_for(command_id, timeout=10.0):
            return _make_skill_result_envelope(command_id=command_id)

        mock_transport_client.execute_harvest_zone = AsyncMock(side_effect=capture_execute)
        mock_transport_client.wait_for_result = AsyncMock(side_effect=result_for)

        await scheduler.harvest_auto(max_tiles=16)

        call_kwargs = mock_transport_client.execute_harvest_zone.call_args.kwargs
        assert call_kwargs["task_id"].startswith("task-harvest-")
        assert call_kwargs["idempotency_key"] == (
            f"mock-save-id-001:{call_kwargs['task_id']}:attempt-1"
        )

    asyncio.run(run())


@pytest.mark.parametrize("bad_coord", [-1, -20, "70", 70.5, True, False])
def test_scheduler_deposit_to_chest_invalid_coords(mock_transport_client, bad_coord):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(PolicyViolationError):
            await scheduler.deposit_to_chest(chest_x=bad_coord, chest_y=12)
        with pytest.raises(PolicyViolationError):
            await scheduler.deposit_to_chest(chest_x=70, chest_y=bad_coord)

    asyncio.run(run())


def test_scheduler_deposit_to_chest_invalid_item_ids(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)

        with pytest.raises(PolicyViolationError, match="list"):
            await scheduler.deposit_to_chest(70, 12, item_ids="(O)24")

        with pytest.raises(PolicyViolationError, match="exceeds maximum"):
            await scheduler.deposit_to_chest(70, 12, item_ids=[f"(O){i}" for i in range(37)])

        with pytest.raises(PolicyViolationError, match="non-empty"):
            await scheduler.deposit_to_chest(70, 12, item_ids=[""])

        with pytest.raises(PolicyViolationError, match="non-empty"):
            await scheduler.deposit_to_chest(70, 12, item_ids=[24])

    asyncio.run(run())


def test_scheduler_deposit_to_chest_executed(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        result_env = _make_skill_result_envelope(
            command_id="cmd-deposit-1",
            task_id="task-deposit-test-1",
            terminal_state="partially-succeeded",
            completed=2,
            skipped=1,
            effects=[
                {
                    "state": "deposited",
                    "itemId": "(O)24",
                    "itemName": "Parsnip",
                    "stack": 3,
                    "quality": 0,
                    "chestTile": {"x": 70, "y": 12},
                }
            ],
            details={"chestFull": True},
        )
        mock_transport_client.execute_deposit_chest = AsyncMock(return_value="cmd-deposit-1")
        mock_transport_client.wait_for_result = AsyncMock(return_value=result_env)

        # Item ids are sorted before dispatch for a canonical wire shape
        res = await scheduler.deposit_to_chest(
            chest_x=70,
            chest_y=12,
            item_ids=["(O)24", "(O)192"],
            task_id="task-deposit-test-1",
        )

        assert res["terminalState"] == "partially-succeeded"
        assert res["completedCount"] == 2
        assert res["skippedCount"] == 1
        assert res["failedCount"] == 0
        assert res["effects"][0]["state"] == "deposited"
        assert res["details"] == {"chestFull": True}
        assert res["error"] is None

        call_kwargs = mock_transport_client.execute_deposit_chest.call_args.kwargs
        assert call_kwargs["chest_x"] == 70
        assert call_kwargs["chest_y"] == 12
        assert call_kwargs["item_ids"] == ["(O)192", "(O)24"]
        assert call_kwargs["task_id"] == "task-deposit-test-1"
        assert (
            call_kwargs["idempotency_key"]
            == "mock-save-id-001:task-deposit-test-1:attempt-1"
        )

    asyncio.run(run())


def test_scheduler_organize_chest_executed(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        result_env = _make_skill_result_envelope(
            command_id="cmd-organize-1",
            task_id="task-organize-test-1",
            terminal_state="succeeded",
            completed=1,
            effects=[
                {
                    "state": "merged",
                    "itemId": "(O)24",
                    "itemName": "Parsnip",
                    "quality": 0,
                    "mergedStack": 8,
                    "chestTile": {"x": 70, "y": 12},
                }
            ],
        )
        mock_transport_client.execute_organize_chest = AsyncMock(
            return_value="cmd-organize-1"
        )
        mock_transport_client.wait_for_result = AsyncMock(return_value=result_env)

        res = await scheduler.organize_chest(
            chest_x=70, chest_y=12, task_id="task-organize-test-1"
        )

        assert res["terminalState"] == "succeeded"
        assert res["completedCount"] == 1
        assert res["effects"][0]["state"] == "merged"
        assert res["details"] is None
        assert res["error"] is None

        call_kwargs = mock_transport_client.execute_organize_chest.call_args.kwargs
        assert call_kwargs["chest_x"] == 70
        assert call_kwargs["chest_y"] == 12
        assert call_kwargs["task_id"] == "task-organize-test-1"
        assert (
            call_kwargs["idempotency_key"]
            == "mock-save-id-001:task-organize-test-1:attempt-1"
        )

    asyncio.run(run())


@pytest.mark.parametrize("bad_coord", [-1, "70", 70.5, True, False])
def test_scheduler_organize_chest_invalid_coords(mock_transport_client, bad_coord):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(PolicyViolationError):
            await scheduler.organize_chest(chest_x=bad_coord, chest_y=12)
        with pytest.raises(PolicyViolationError):
            await scheduler.organize_chest(chest_x=70, chest_y=bad_coord)

    asyncio.run(run())


def test_scheduler_chest_skills_concurrent_rejection(mock_transport_client, native_compatible_run_dir):
    """Deposit/organize are rejected while another task is active."""
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        wait_event = asyncio.Event()

        async def delayed_result(command_id, timeout=10.0):
            await wait_event.wait()
            return _make_skill_result_envelope(command_id=command_id)

        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-water-1")
        mock_transport_client.wait_for_result = AsyncMock(side_effect=delayed_result)

        first_task = asyncio.create_task(
            scheduler.execute_water_zone(center_x=64, center_y=15, radius=0)
        )
        await asyncio.sleep(0.01)
        assert scheduler.has_active_task

        with pytest.raises(PolicyViolationError, match="Concurrent tasks are not permitted"):
            await scheduler.deposit_to_chest(chest_x=70, chest_y=12)

        with pytest.raises(PolicyViolationError, match="Concurrent tasks are not permitted"):
            await scheduler.organize_chest(chest_x=70, chest_y=12)

        wait_event.set()
        await first_task
        assert not scheduler.has_active_task

    asyncio.run(run())


def test_scheduler_query_inventory_projection(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12,
            "freeSlots": 10,
            "slots": [
                {
                    "index": 0,
                    "itemId": "(O)24",
                    "name": "Parsnip",
                    "stack": 3,
                    "quality": 0,
                }
            ],
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        res = await scheduler.query_inventory()

        assert res["inventory"]["capacity"] == 12
        assert res["inventory"]["freeSlots"] == 10
        assert res["inventory"]["slots"][0]["itemId"] == "(O)24"
        assert res["saveId"] == "mock-save-id-001"
        assert res["gameSessionId"] == "mock-session-001"
        assert res["worldRevision"] == 5

    asyncio.run(run())


def test_scheduler_query_inventory_missing_section(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(SchedulerError, match="too old"):
            await scheduler.query_inventory()

    asyncio.run(run())


def test_scheduler_query_chests_projection(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["chests"] = {
            "truncated": True,
            "items": [
                {
                    "tile": {"x": 70, "y": 12},
                    "capacity": 36,
                    "freeSlots": 35,
                    "contents": [
                        {
                            "slot": 0,
                            "itemId": "(O)24",
                            "name": "Parsnip",
                            "stack": 5,
                            "quality": 0,
                        }
                    ],
                }
            ],
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        res = await scheduler.query_chests()

        assert len(res["chests"]) == 1
        assert res["chests"][0]["tile"] == {"x": 70, "y": 12}
        assert res["chests"][0]["contents"][0]["stack"] == 5
        assert res["isTruncated"] is True
        assert res["saveId"] == "mock-save-id-001"
        assert res["gameSessionId"] == "mock-session-001"
        assert res["worldRevision"] == 5

    asyncio.run(run())


def test_scheduler_query_chests_missing_section(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(SchedulerError, match="too old"):
            await scheduler.query_chests()

    asyncio.run(run())


def test_scheduler_query_farm_work_mature_crops_projection(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}],
            "tilledUnwateredCount": 1,
            "isTruncated": False,
            "matureCropCount": 2,
            "matureCrops": [
                {"x": 64, "y": 16, "cropId": "(O)24"},
                {"x": 65, "y": 16, "cropId": "(O)188"},
            ],
            "matureCropsTruncated": True,
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        result = await scheduler.query_farm_work()

        farm_work = result["farmWork"]
        assert farm_work["tilledUnwateredCount"] == 1
        assert farm_work["matureCropCount"] == 2
        assert len(farm_work["matureCrops"]) == 2
        assert farm_work["matureCrops"][0]["cropId"] == "(O)24"
        assert farm_work["matureCropsTruncated"] is True

    asyncio.run(run())


def test_scheduler_get_work_overview_complete(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}],
            "tilledUnwateredCount": 1,
            "isTruncated": False,
            "matureCropCount": 3,
            "matureCrops": [
                {"x": 64, "y": 16, "cropId": "(O)24"},
                {"x": 65, "y": 16, "cropId": "(O)24"},
                {"x": 66, "y": 16, "cropId": "(O)188"},
            ],
            "matureCropsTruncated": False,
        }
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12,
            "freeSlots": 8,
            "slots": [
                {"slot": 0, "itemId": "(T)WateringCan", "name": "Watering Can", "stack": 1},
                {"slot": 1, "itemId": "(O)24", "name": "Parsnip", "stack": 5, "quality": 0},
            ],
        }
        mock_transport_client.latest_snapshot.payload["chests"] = {
            "items": [
                {
                    "tile": {"x": 70, "y": 12},
                    "capacity": 36,
                    "freeSlots": 34,
                    "contents": [
                        {
                            "slot": 0,
                            "itemId": "(O)24",
                            "name": "Parsnip",
                            "stack": 10,
                            "quality": 0,
                        },
                        {
                            "slot": 1,
                            "itemId": "(O)24",
                            "name": "Parsnip",
                            "stack": 5,
                            "quality": 0,
                        },
                    ],
                }
            ],
            "isTruncated": False,
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        overview = await scheduler.get_work_overview(detail=False)

        assert overview["worldRevision"] == 5
        assert overview["canSafelyPlan"] is True
        assert overview["missingSections"] == []
        assert overview["farmWork"]["matureCropCount"] == 3
        assert overview["farmWork"]["tilledUnwateredCount"] == 1
        assert overview["companion"]["freeSlots"] == 8
        assert len(overview["companion"]["nonToolItems"]) == 1
        assert overview["companion"]["nonToolItems"][0]["itemId"] == "(O)24"

        # Chest evaluation: Parsnip matches backpack, and chest has 2 Parsnip stacks (candidate)
        assert len(overview["chests"]) == 1
        chest = overview["chests"][0]
        assert chest["tile"] == {"x": 70, "y": 12}
        assert chest["hasDuplicateStacks"] is True
        assert chest["hasMergeableStacks"] == "unknown"
        assert len(chest["matchingItems"]) == 2
        assert chest["matchingItems"][0]["itemId"] == "(O)24"

    asyncio.run(run())


def test_scheduler_get_work_overview_missing_sections(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload.pop("farmWork", None)
        mock_transport_client.latest_snapshot.payload.pop("chests", None)

        scheduler = CompanionScheduler(client=mock_transport_client)
        overview = await scheduler.get_work_overview(detail=False)

        assert overview["canSafelyPlan"] is False
        assert "farmWork" in overview["missingSections"]
        assert "chests" in overview["missingSections"]
        assert overview["farmWork"]["missing"] is True
        assert overview["farmWork"]["matureCropCount"] is None
        assert overview["farmWork"]["tilledUnwateredCount"] is None
        assert overview["chestsMissing"] is True

    asyncio.run(run())


def test_scheduler_wait_for_fresh_snapshot_success(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        pre_rev = scheduler.latest_world_revision

        newer_env = Envelope.from_mapping({
            "protocolVersion": "0.1",
            "messageType": "world.snapshot",
            "messageId": "snap-newer",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 3,
            "worldRevision": pre_rev + 1,
            "sentAt": "2026-09-12T00:00:10Z",
            "payload": {"updated": True},
        })

        async def fake_wait_for_snapshot(predicate=None, timeout=None):
            mock_transport_client.latest_snapshot = newer_env
            mock_transport_client.world_revision = newer_env.world_revision
            return newer_env

        mock_transport_client.wait_for_snapshot = fake_wait_for_snapshot

        snap, fresh = await scheduler.wait_for_fresh_snapshot(pre_rev, timeout=0.1)
        assert fresh is True
        assert snap["payload"]["updated"] is True
        assert scheduler.latest_world_revision == pre_rev + 1

    asyncio.run(run())


def test_scheduler_wait_for_fresh_snapshot_timeout(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        pre_rev = scheduler.latest_world_revision

        async def fake_wait_for_snapshot(predicate=None, timeout=None):
            raise TimeoutError()

        mock_transport_client.wait_for_snapshot = fake_wait_for_snapshot

        snap, fresh = await scheduler.wait_for_fresh_snapshot(pre_rev, timeout=0.05)
        assert fresh is False
        assert snap is None

    asyncio.run(run())


def test_time_refresh_snapshot_updates_cache_without_bump(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        pre_rev = scheduler.latest_world_revision

        # TimeChanged snapshot arrives with same worldRevision, updated time and shop
        time_refresh_env = Envelope.from_mapping({
            "protocolVersion": "0.1",
            "messageType": "world.snapshot",
            "messageId": "snap-time-900",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 2,
            "worldRevision": pre_rev,  # revision unchanged on TimeChanged
            "sentAt": "2026-09-12T00:00:10Z",
            "payload": {
                "world": {"timeOfDay": 900},
                "shop": {
                    "shopId": "SeedShop",
                    "status": "ok",
                    "isOpen": True,
                    "availableMoney": 500,
                    "items": [],
                },
            },
        })

        async def fake_wait_for_snapshot(predicate=None, timeout=None):
            mock_transport_client.latest_snapshot = time_refresh_env
            return time_refresh_env

        mock_transport_client.wait_for_snapshot = fake_wait_for_snapshot

        # 1. query_shop / _refresh_snapshot picks up the refreshed snapshot
        shop_info = await scheduler.query_shop("SeedShop")
        assert shop_info["isOpen"] is True
        assert scheduler.latest_world_revision == pre_rev  # revision did NOT bump

        # 2. wait_for_fresh_snapshot strictly requires world_revision > pre_rev,
        # so it does NOT mistake the time-refresh snapshot for a completed task
        snap, fresh = await scheduler.wait_for_fresh_snapshot(pre_rev, timeout=0.05)
        assert fresh is False
        assert snap is None

    asyncio.run(run())


def test_extract_chest_summary_full_chest_repeat_full_stacks():
    # 36 stacks of 999 stone in full chest (freeSlots=0)
    contents = [
        {
            "slot": i,
            "itemId": "(O)390",
            "name": "Stone",
            "stack": 999,
            "quality": 0,
        }
        for i in range(36)
    ]
    chest = {"tile": {"x": 70, "y": 12}, "capacity": 36, "freeSlots": 0, "contents": contents}
    comp_items = [{"itemId": "(O)390", "name": "Stone", "stack": 50, "quality": 0}]

    summary = extract_chest_summary(chest, 70, 12, comp_items, detail=False)
    assert summary["tile"] == {"x": 70, "y": 12}
    assert summary["freeSlots"] == 0
    assert summary["itemCount"] == 36
    assert len(summary["items"]) == 6
    assert summary["itemsTruncated"] is True
    # Crucial: duplicate stacks without real maxStack are candidates
    # (hasDuplicateStacks=True, hasMergeableStacks="unknown")
    assert summary["hasDuplicateStacks"] is True
    assert summary["hasMergeableStacks"] == "unknown"
    # Crucial: Full chest with matching item but unknown maxStack must return unknown, NOT True
    assert summary["canDeposit"] == "unknown"


def test_extract_chest_summary_quality_aware_matching():
    # Full chest has regular quality Parsnip (quality 0)
    contents = [
        {"slot": 0, "itemId": "(O)24", "name": "Parsnip", "stack": 10, "quality": 0},
    ]
    chest = {"tile": {"x": 70, "y": 12}, "capacity": 36, "freeSlots": 0, "contents": contents}
    # Companion has gold quality Parsnip (quality 2)
    comp_items = [{"itemId": "(O)24", "name": "Parsnip", "stack": 5, "quality": 2}]

    summary = extract_chest_summary(chest, 70, 12, comp_items, detail=False)
    # Quality mismatch means no matching items
    assert len(summary["matchingItems"]) == 0
    # No duplicate stacks
    assert summary["hasDuplicateStacks"] is False
    assert summary["hasMergeableStacks"] is False
    # Full chest with NO matching items cannot accept deposit
    assert summary["canDeposit"] is False


def test_extract_chest_summary_non_full_mergeable_stacks():
    # 2 stacks of Parsnip (quality 0)
    contents = [
        {"slot": 0, "itemId": "(O)24", "name": "Parsnip", "stack": 10, "quality": 0},
        {"slot": 1, "itemId": "(O)24", "name": "Parsnip", "stack": 5, "quality": 0},
    ]
    chest = {"tile": {"x": 70, "y": 12}, "capacity": 36, "freeSlots": 34, "contents": contents}
    comp_items = [{"itemId": "(O)24", "name": "Parsnip", "stack": 5, "quality": 0}]

    summary = extract_chest_summary(chest, 70, 12, comp_items, detail=False)
    assert summary["hasDuplicateStacks"] is True
    assert summary["hasMergeableStacks"] == "unknown"
    assert summary["itemsTruncated"] is False
    assert summary["canDeposit"] is True


def test_validate_action_tiles():
    # Empty list
    with pytest.raises(PolicyViolationError, match="non-empty"):
        validate_action_tiles([])

    # Invalid tile format
    with pytest.raises(PolicyViolationError, match="invalid tile format"):
        validate_action_tiles([{"x": 1}])

    # Booleans rejected
    with pytest.raises(PolicyViolationError, match="integers"):
        validate_action_tiles([{"x": True, "y": 2}])
    with pytest.raises(PolicyViolationError, match="integers"):
        validate_action_tiles([{"x": 1, "y": False}])

    # Negative coordinates rejected
    with pytest.raises(PolicyViolationError, match="non-negative"):
        validate_action_tiles([{"x": -1, "y": 2}])

    # Deduplication and row-major sorting
    input_tiles = [
        {"x": 65, "y": 15},
        {"x": 64, "y": 14},
        {"x": 65, "y": 15},  # duplicate
        {"x": 64, "y": 15},
    ]
    res = validate_action_tiles(input_tiles)
    assert len(res) == 3
    assert res == [
        {"x": 64, "y": 14},
        {"x": 64, "y": 15},
        {"x": 65, "y": 15},
    ]

    # Exceeds 64 unique tiles
    too_many = [{"x": i, "y": 0} for i in range(65)]
    with pytest.raises(PolicyViolationError, match="exceeds maximum"):
        validate_action_tiles(too_many)


def test_validate_seed_item_id():
    assert validate_seed_item_id("(O)472") == "(O)472"
    assert validate_seed_item_id("  472  ") == "472"
    with pytest.raises(PolicyViolationError, match="string"):
        validate_seed_item_id(123)
    with pytest.raises(PolicyViolationError, match="string"):
        validate_seed_item_id(True)
    with pytest.raises(PolicyViolationError, match="cannot be empty"):
        validate_seed_item_id("   ")


def test_scheduler_query_planting_options_projection(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "season": "spring",
            "dayOfMonth": 1,
            "companionHasHoe": True,
            "seeds": [
                {
                    "itemId": "(O)472",
                    "name": "Parsnip Seeds",
                    "stack": 15,
                    "canPlantCurrentSeason": True,
                    "seasons": ["spring"],
                    "growthDays": 4,
                    "regrows": False,
                    "isRaised": False,
                }
            ],
            "candidateTiles": {
                "tilledEmptyCount": 8,
                "tilledEmptyTiles": [{"x": i, "y": 10} for i in range(8)],
                "tilledEmptyTruncated": False,
                "tillableCount": 20,
                "tillableTiles": [{"x": i, "y": 11} for i in range(8)],
                "tillableTruncated": False,
            },
            "searchBounds": {
                "center": {"x": 64, "y": 15},
                "radius": 15,
            },
        }
        scheduler = CompanionScheduler(client=mock_transport_client)

        # Default detail=False (sample 6 tiles, truncated=True for sample, concise seed)
        res = await scheduler.query_planting_options(detail=False)
        assert res["season"] == "spring"
        assert res["dayOfMonth"] == 1
        assert res["companionHasHoe"] is True
        assert len(res["seeds"]) == 1
        assert "growthDays" not in res["seeds"][0]
        assert res["seeds"][0]["itemId"] == "(O)472"
        assert res["candidateTiles"]["tilledEmptyCount"] == 8
        assert len(res["candidateTiles"]["tilledEmptyTiles"]) == 6
        assert res["candidateTiles"]["tilledEmptyTruncated"] is True
        assert res["candidateTiles"]["tillableCount"] == 20
        assert len(res["candidateTiles"]["tillableTiles"]) == 6
        assert res["candidateTiles"]["tillableTruncated"] is True
        assert res["searchBounds"]["radius"] == 15

        # detail=True (all 8 tiles, full seed growth metadata)
        res_det = await scheduler.query_planting_options(detail=True)
        assert len(res_det["candidateTiles"]["tilledEmptyTiles"]) == 8
        assert res_det["candidateTiles"]["tilledEmptyTruncated"] is False
        assert len(res_det["candidateTiles"]["tillableTiles"]) == 8
        assert res_det["candidateTiles"]["tillableTruncated"] is False
        assert res_det["seeds"][0]["growthDays"] == 4
        assert res_det["seeds"][0]["regrows"] is False
        assert res_det["seeds"][0]["isRaised"] is False

    asyncio.run(run())


def test_scheduler_query_planting_options_missing_section(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(SchedulerError, match="too old"):
            await scheduler.query_planting_options()

    asyncio.run(run())


def test_scheduler_query_planting_options_missing_candidate_tiles(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "season": "spring",
            "dayOfMonth": 1,
            "companionHasHoe": True,
            "seeds": [],
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(SchedulerError, match="candidateTiles"):
            await scheduler.query_planting_options()

    asyncio.run(run())


def test_scheduler_query_planting_options_explicit_unknown(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "candidateTiles": {},
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        res = await scheduler.query_planting_options()
        assert res["season"] == "unknown"
        assert res["dayOfMonth"] == "unknown"
        assert res["companionHasHoe"] == "unknown"
        assert res["seeds"] == "unknown"
        assert res["candidateTiles"]["tilledEmptyCount"] == "unknown"
        assert res["candidateTiles"]["tillableCount"] == "unknown"
        assert res["searchBounds"] == "unknown"

    asyncio.run(run())


def test_scheduler_execute_hoe_tiles(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)
        mock_transport_client.execute_hoe_tiles = AsyncMock(return_value="cmd-hoe-test-01")
        mock_transport_client.wait_for_result = AsyncMock(
            return_value=Envelope(
                protocol_version="0.1",
                message_type="skill.result",
                message_id="res-1",
                sender_instance_id="mod-1",
                sequence_number=1,
                world_revision=6,
                sent_at=datetime.now(UTC),
                payload={
                    "commandId": "cmd-hoe-test-01",
                    "taskId": "task-1",
                    "terminalState": "succeeded",
                    "completedCount": 2,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "finalWorldRevision": 6,
                    "effects": [
                        {"tile": {"x": 64, "y": 15}, "state": "hoed"},
                        {"tile": {"x": 65, "y": 15}, "state": "hoed"},
                    ],
                    "details": {
                        "hoedTiles": [{"x": 64, "y": 15}, {"x": 65, "y": 15}],
                    },
                },
            )
        )

        res = await scheduler.execute_hoe_tiles(
            tiles=[{"x": 65, "y": 15}, {"x": 64, "y": 15}, {"x": 65, "y": 15}]
        )
        assert res["terminalState"] == "succeeded"
        assert res["completedCount"] == 2
        # Verify deduplication was applied to client call
        called_tiles = mock_transport_client.execute_hoe_tiles.call_args.kwargs["tiles"]
        assert len(called_tiles) == 2
        assert called_tiles == [{"x": 64, "y": 15}, {"x": 65, "y": 15}]

    asyncio.run(run())


def test_scheduler_execute_plant_seeds(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)
        mock_transport_client.execute_plant_seeds = AsyncMock(return_value="cmd-plant-test-01")
        mock_transport_client.wait_for_result = AsyncMock(
            return_value=Envelope(
                protocol_version="0.1",
                message_type="skill.result",
                message_id="res-2",
                sender_instance_id="mod-1",
                sequence_number=2,
                world_revision=7,
                sent_at=datetime.now(UTC),
                payload={
                    "commandId": "cmd-plant-test-01",
                    "taskId": "task-2",
                    "terminalState": "partially-succeeded",
                    "completedCount": 1,
                    "skippedCount": 1,
                    "failedCount": 0,
                    "finalWorldRevision": 7,
                    "effects": [
                        {
                            "tile": {"x": 64, "y": 15},
                            "state": "planted",
                            "itemId": "(O)472",
                            "stack": 14,
                        },
                        {
                            "tile": {"x": 65, "y": 15},
                            "state": "skipped",
                            "reason": "already-occupied",
                        },
                    ],
                    "details": {
                        "plantedTiles": [{"x": 64, "y": 15}],
                        "remainingSeedStack": 14,
                        "seedItemId": "(O)472",
                        "outOfSeeds": False,
                    },
                },
            )
        )

        res = await scheduler.execute_plant_seeds(
            seed_item_id=" (O)472 ",
            tiles=[{"x": 64, "y": 15}, {"x": 65, "y": 15}],
        )
        assert res["terminalState"] == "partially-succeeded"
        assert res["completedCount"] == 1
        assert res["skippedCount"] == 1
        assert res["details"]["remainingSeedStack"] == 14
        called_seed = mock_transport_client.execute_plant_seeds.call_args.kwargs["seed_item_id"]
        assert called_seed == "(O)472"

    asyncio.run(run())


def test_scheduler_query_shop_projection(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["shop"] = {
            "shopId": "SeedShop",
            "status": "ok",
            "isOpen": True,
            "ownerPresent": True,
            "closedMessage": None,
            "owners": ["Pierre"],
            "currency": 0,
            "availableMoney": 1500,
            "moneyStatus": "ok",
            "itemsCount": 2,
            "locationId": "SeedShop",
            "interactionTile": {"x": 4, "y": 19},
            "items": [
                {
                    "itemId": "(O)472",
                    "name": "Parsnip Seeds",
                    "price": 20,
                    "stock": 2147483647,
                    "isInfiniteStock": True,
                    "limitedStockMode": "None",
                },
                {
                    "itemId": "(O)474",
                    "name": "Cauliflower Seeds",
                    "price": 80,
                    "stock": 5,
                    "isInfiniteStock": False,
                    "tradeItem": "(O)388",
                    "tradeItemCount": 2,
                    "limitedStockMode": "Player",
                    "actionsOnPurchase": ["AwardAchievement"],
                },
            ],
        }
        scheduler = CompanionScheduler(client=mock_transport_client)

        # Default detail=False (concise items)
        res = await scheduler.query_shop(detail=False)
        assert res["shopId"] == "SeedShop"
        assert res["locationId"] == "SeedShop"
        assert res["interactionTile"] == {"x": 4, "y": 19}
        assert res["status"] == "ok"
        assert res["isOpen"] is True
        assert res["ownerPresent"] is True
        assert res["closedMessage"] is None
        assert res["owners"] == ["Pierre"]
        assert res["availableMoney"] == 1500
        assert res["moneyStatus"] == "ok"
        assert res["itemsCount"] == 2
        assert len(res["items"]) == 2
        assert res["items"][0]["itemId"] == "(O)472"
        assert res["items"][0]["price"] == 20
        assert res["items"][0]["stock"] == 2147483647
        assert res["items"][0]["isInfiniteStock"] is True
        assert "limitedStockMode" not in res["items"][0]
        assert "actionsOnPurchase" not in res["items"][1]
        assert res["items"][1]["tradeItem"] == "(O)388"
        assert res["items"][1]["tradeItemCount"] == 2
        assert res["saveId"] == "mock-save-id-001"
        assert res["gameSessionId"] == "mock-session-001"
        assert res["worldRevision"] == 5

        # detail=True (metadata included)
        res_det = await scheduler.query_shop(detail=True)
        assert res_det["items"][1]["limitedStockMode"] == "Player"
        assert res_det["items"][1]["actionsOnPurchase"] == ["AwardAchievement"]

    asyncio.run(run())


def test_scheduler_query_shop_missing_section(mock_transport_client):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(SchedulerError, match="too old"):
            await scheduler.query_shop()

    asyncio.run(run())


def test_scheduler_query_shop_unsupported_shop_id(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["shop"] = {
            "shopId": "SeedShop",
            "status": "ok",
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        with pytest.raises(SchedulerError, match="Published shop ID\\(s\\): \\['SeedShop'\\]"):
            await scheduler.query_shop(shop_id="JojaMart")

    asyncio.run(run())


def test_scheduler_query_shop_explicit_unknown(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["shop"] = {
            "shopId": "SeedShop",
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        res = await scheduler.query_shop()
        assert res["shopId"] == "SeedShop"
        assert res["status"] == "unknown"
        assert res["isOpen"] is False
        assert res["ownerPresent"] is False
        assert res["availableMoney"] is None
        assert res["moneyStatus"] == "missing"
        assert res["items"] == "unknown"
        assert res["itemsCount"] == "unknown"

    asyncio.run(run())


def test_scheduler_validate_ship_items():
    # Empty list
    with pytest.raises(PolicyViolationError, match="non-empty"):
        CompanionScheduler._validate_ship_items([])

    # Exceeds 36
    too_many = [{"itemId": f"(O){i}", "count": 1} for i in range(37)]
    with pytest.raises(PolicyViolationError, match="exceeds maximum"):
        CompanionScheduler._validate_ship_items(too_many)

    # Non-dict
    with pytest.raises(PolicyViolationError, match="dictionary"):
        CompanionScheduler._validate_ship_items(["invalid"])

    # Bad itemId
    with pytest.raises(PolicyViolationError, match="non-empty string"):
        CompanionScheduler._validate_ship_items([{"itemId": True, "count": 1}])
    with pytest.raises(PolicyViolationError, match="cannot be empty"):
        CompanionScheduler._validate_ship_items([{"itemId": "   ", "count": 1}])

    # Bad count
    with pytest.raises(PolicyViolationError, match="integer"):
        CompanionScheduler._validate_ship_items([{"itemId": "(O)24", "count": True}])
    with pytest.raises(PolicyViolationError, match="at least 1"):
        CompanionScheduler._validate_ship_items([{"itemId": "(O)24", "count": 0}])

    # Valid items
    valid = CompanionScheduler._validate_ship_items([
        {"itemId": " (O)24 ", "count": 2},
        {"item_id": "24", "count": 1},
    ])
    assert valid == [
        {"itemId": "(O)24", "count": 2},
        {"itemId": "24", "count": 1},
    ]


def test_scheduler_execute_ship_items(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)
        mock_transport_client.execute_ship_items = AsyncMock(return_value="cmd-ship-test-01")
        mock_transport_client.wait_for_result = AsyncMock(
            return_value=Envelope(
                protocol_version="0.1",
                message_type="skill.result",
                message_id="res-ship-1",
                sender_instance_id="mod-1",
                sequence_number=1,
                world_revision=6,
                sent_at=datetime.now(UTC),
                payload={
                    "commandId": "cmd-ship-test-01",
                    "taskId": "task-ship-1",
                    "terminalState": "succeeded",
                    "completedCount": 2,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "finalWorldRevision": 6,
                    "effects": [
                        {
                            "state": "shipped",
                            "itemId": "(O)24",
                            "count": 2,
                            "estimatedUnitValue": 35,
                            "estimatedTotalValue": 70,
                            "remainingBackpackCount": 2,
                        }
                    ],
                    "details": {
                        "shippedItems": [
                            {
                                "itemId": "(O)24",
                                "count": 2,
                                "estimatedUnitValue": 35,
                                "estimatedTotalValue": 70,
                                "remainingBackpackCount": 2,
                            }
                        ],
                        "estimatedTotalValue": 70,
                        "estimatedValue": 70,
                        "shippingBinTotalCount": 2,
                        "remainingBackpackCounts": {"(O)24": 2},
                    },
                },
            )
        )

        res = await scheduler.execute_ship_items(
            items=[{"itemId": " (O)24 ", "count": 2}],
            task_id="task-ship-1",
        )
        assert res["terminalState"] == "succeeded"
        assert res["completedCount"] == 2
        assert res["details"]["estimatedValue"] == 70

        called_items = mock_transport_client.execute_ship_items.call_args.kwargs["items"]
        assert called_items == [{"itemId": "(O)24", "count": 2}]

    asyncio.run(run())


def test_scheduler_execute_navigate_to(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)
        mock_transport_client.execute_navigate_to = AsyncMock(return_value="cmd-nav-test-01")
        mock_transport_client.wait_for_result = AsyncMock(
            return_value=Envelope(
                protocol_version="0.1",
                message_type="skill.result",
                message_id="res-nav-1",
                sender_instance_id="mod-1",
                sequence_number=1,
                world_revision=7,
                sent_at=datetime.now(UTC),
                payload={
                    "commandId": "cmd-nav-test-01",
                    "taskId": "task-nav-1",
                    "terminalState": "succeeded",
                    "completedCount": 1,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "finalWorldRevision": 7,
                    "effects": [
                        {"location": "Farm", "pathLength": 15},
                        {"location": "BusStop", "pathLength": 20},
                        {"location": "Town", "pathLength": 25},
                    ],
                    "details": {
                        "visitedLocations": ["Farm", "BusStop", "Town"],
                        "finalLocation": "Town",
                        "finalTile": {"x": 43, "y": 58},
                        "hopCount": 2,
                    },
                    "skillId": "navigate-to",
                },
            )
        )

        res = await scheduler.execute_navigate_to(
            location_id="Town",
            tile={"x": 43, "y": 58},
            task_id="task-nav-1",
        )
        assert res["terminalState"] == "succeeded"
        assert res["completedCount"] == 1
        assert res["details"]["finalLocation"] == "Town"
        assert res["details"]["visitedLocations"] == ["Farm", "BusStop", "Town"]

        mock_transport_client.execute_navigate_to.assert_awaited_once_with(
            location_id="Town",
            tile={"x": 43, "y": 58},
            task_id="task-nav-1",
            idempotency_key=mock_transport_client.execute_navigate_to.call_args.kwargs["idempotency_key"],
            expires_seconds=mock_transport_client.execute_navigate_to.call_args.kwargs["expires_seconds"],
        )

        # Validation errors
        with pytest.raises(SchedulerError, match="non-empty string"):
            await scheduler.execute_navigate_to(location_id="", tile={"x": 10, "y": 10})

        with pytest.raises(SchedulerError, match="must be non-negative integers"):
            await scheduler.execute_navigate_to(location_id="Town", tile={"x": -1, "y": 10})

        with pytest.raises(SchedulerError, match="must be non-negative integers"):
            await scheduler.execute_navigate_to(location_id="Town", tile={"x": True, "y": 10})

        with pytest.raises(SchedulerError, match="must be non-negative integers"):
            await scheduler.execute_navigate_to(location_id="Town", tile={"x": 10})

    asyncio.run(run())


def test_scheduler_validate_purchase_items():
    with pytest.raises(PolicyViolationError, match="items must be a non-empty list"):
        CompanionScheduler._validate_purchase_items([])

    too_many = [{"itemId": f"item-{i}", "count": 1} for i in range(37)]
    with pytest.raises(PolicyViolationError, match="exceeds maximum allowed"):
        CompanionScheduler._validate_purchase_items(too_many)

    with pytest.raises(PolicyViolationError, match="must be a dictionary"):
        CompanionScheduler._validate_purchase_items(["invalid"])

    with pytest.raises(PolicyViolationError, match="itemId must be a non-empty string"):
        CompanionScheduler._validate_purchase_items([{"itemId": True, "count": 1}])
    with pytest.raises(PolicyViolationError, match="itemId cannot be empty"):
        CompanionScheduler._validate_purchase_items([{"itemId": "   ", "count": 1}])

    with pytest.raises(PolicyViolationError, match="count must be an integer"):
        CompanionScheduler._validate_purchase_items([{"itemId": "(O)472", "count": True}])
    with pytest.raises(PolicyViolationError, match="count must be at least 1"):
        CompanionScheduler._validate_purchase_items([{"itemId": "(O)472", "count": 0}])

    valid = CompanionScheduler._validate_purchase_items([
        {"itemId": " (O)472 ", "count": 2},
        {"item_id": "(O)474", "count": 1},
    ])
    assert valid == [
        {"itemId": "(O)472", "count": 2},
        {"itemId": "(O)474", "count": 1},
    ]


def test_scheduler_execute_purchase_items(mock_transport_client, native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)
        mock_transport_client.execute_purchase_items = AsyncMock(
            return_value="cmd-purchase-test-01"
        )
        mock_transport_client.wait_for_result = AsyncMock(
            return_value=Envelope(
                protocol_version="0.1",
                message_type="skill.result",
                message_id="res-purchase-1",
                sender_instance_id="mod-1",
                sequence_number=1,
                world_revision=8,
                sent_at=datetime.now(UTC),
                payload={
                    "commandId": "cmd-purchase-test-01",
                    "taskId": "task-purchase-1",
                    "terminalState": "succeeded",
                    "completedCount": 2,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "finalWorldRevision": 8,
                    "details": {
                        "shopId": "SeedShop",
                        "totalCost": 40,
                        "budgetLimit": 100,
                        "remainingBudget": 60,
                        "availableMoneyAfter": 460,
                        "purchasedItems": [
                            {"itemId": "(O)472", "count": 2, "unitPrice": 20, "subtotal": 40}
                        ],
                        "skippedItems": [],
                        "rollbackPerformed": False,
                    },
                    "skillId": "purchase-items",
                },
            )
        )

        res = await scheduler.execute_purchase_items(
            items=[{"itemId": " (O)472 ", "count": 2}],
            budget_limit=100,
            shop_id="SeedShop",
            task_id="task-purchase-1",
        )
        assert res["terminalState"] == "succeeded"
        assert res["completedCount"] == 2
        assert res["details"]["totalCost"] == 40
        assert res["details"]["remainingBudget"] == 60

        called_items = mock_transport_client.execute_purchase_items.call_args.kwargs["items"]
        assert called_items == [{"itemId": "(O)472", "count": 2}]
        assert (
            mock_transport_client.execute_purchase_items.call_args.kwargs["budget_limit"] == 100
        )
        assert (
            mock_transport_client.execute_purchase_items.call_args.kwargs["shop_id"] == "SeedShop"
        )

        # Invalid budget_limit
        with pytest.raises(PolicyViolationError, match="budget_limit must be a positive integer"):
            await scheduler.execute_purchase_items(
                items=[{"itemId": "(O)472", "count": 1}], budget_limit=0
            )

        with pytest.raises(PolicyViolationError, match="budget_limit must be a positive integer"):
            await scheduler.execute_purchase_items(
                items=[{"itemId": "(O)472", "count": 1}], budget_limit=-10
            )

    asyncio.run(run())


def test_scheduler_task_timeout_returns_executing_and_retains_active_task(mock_transport_client, native_compatible_run_dir):
    """Timeout does not mean task failure. Status is executing, active task is retained, blind re-dispatch blocked."""
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}],
            "tilledUnwateredCount": 1,
            "isTruncated": False,
            "matureCropCount": 0,
        }
        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-water-slow-1")
        mock_transport_client.wait_for_result = AsyncMock(side_effect=TimeoutError("Timed out"))

        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        res = await scheduler.water_auto(max_tiles=10, timeout_seconds=1.0)
        assert res["status"] == "executing"
        assert res["terminalState"] == "running"
        assert res["taskId"].startswith("task-water-")
        task_id = res["taskId"]

        # Active task is STILL retained!
        assert scheduler.has_active_task is True
        assert scheduler.active_task.task_id == task_id
        assert scheduler.active_task.status == "running"

        # Blind re-dispatch of a new task is rejected
        with pytest.raises(PolicyViolationError, match="Concurrent tasks are not permitted"):
            await scheduler.water_auto(max_tiles=5)

        # But calling with the SAME task_id queries/waits on the existing task!
        mock_transport_client.wait_for_result = AsyncMock(return_value=Envelope.from_mapping({
            "protocolVersion": "0.1",
            "messageType": "skill.result",
            "messageId": "res-slow-1",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 5,
            "worldRevision": 7,
            "sentAt": "2026-09-14T00:00:10Z",
            "payload": {
                "commandId": "cmd-water-slow-1",
                "taskId": task_id,
                "terminalState": "succeeded",
                "completedCount": 1,
                "skippedCount": 0,
                "failedCount": 0,
                "effects": [],
            },
        }))

        res2 = await scheduler.water_auto(max_tiles=10, task_id=task_id)
        assert res2["status"] == "executed"
        assert res2["terminalState"] == "succeeded"
        assert scheduler.has_active_task is False

    asyncio.run(run())


def test_scheduler_get_status_settles_completed_task(mock_transport_client, native_compatible_run_dir):
    """get_status detects background task completion via cached result."""
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}],
            "tilledUnwateredCount": 1,
            "isTruncated": False,
        }
        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-water-bg-1")
        mock_transport_client.wait_for_result = AsyncMock(side_effect=TimeoutError("Timed out"))

        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)
        res = await scheduler.water_auto(max_tiles=10)
        assert res["status"] == "executing"
        assert scheduler.has_active_task is True

        # Simulate Mod delivering skill.result into client's completed results
        mock_transport_client.get_cached_result = MagicMock(return_value=Envelope.from_mapping({
            "protocolVersion": "0.1",
            "messageType": "skill.result",
            "messageId": "res-bg-1",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 6,
            "worldRevision": 8,
            "sentAt": "2026-09-14T00:00:15Z",
            "payload": {
                "commandId": "cmd-water-bg-1",
                "terminalState": "succeeded",
                "completedCount": 1,
            },
        }))

        status = await scheduler.get_status()
        assert scheduler.has_active_task is False
        assert status["companion"]["currentTask"] is None

    asyncio.run(run())


def test_scheduler_pause_and_cancel_immediate_on_running_task(mock_transport_client, native_compatible_run_dir):
    """Running task after timeout can be paused and cancelled immediately."""
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}],
            "tilledUnwateredCount": 1,
            "isTruncated": False,
        }
        mock_transport_client.execute_water_zone = AsyncMock(return_value="cmd-water-ctl-1")
        mock_transport_client.wait_for_result = AsyncMock(side_effect=TimeoutError("Timed out"))
        mock_transport_client.pause_skill = AsyncMock()
        mock_transport_client.cancel_skill = AsyncMock()

        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)
        await scheduler.water_auto(max_tiles=10)

        assert scheduler.has_active_task is True

        # Pause works immediately
        p_res = await scheduler.pause_task()
        assert p_res["status"] == "paused"
        mock_transport_client.pause_skill.assert_awaited_once()

        # Cancel works immediately
        c_res = await scheduler.cancel_task()
        assert c_res["status"] == "cancelling"
        mock_transport_client.cancel_skill.assert_awaited_once()

    asyncio.run(run())


def test_scheduler_plant_crop_workflow_with_chest_withdraw(mock_transport_client):
    """plant_crop_workflow finds seeds in chest, withdraws, hoes if needed, plants, waters."""
    async def run():
        # Inventory has hoe but no seeds
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12,
            "freeSlots": 11,
            "slots": [
                {"itemId": "(T)Hoe", "name": "Hoe", "category": -99, "stack": 1}
            ],
        }
        # Chest at (70, 12) has Parsnip Seeds
        mock_transport_client.latest_snapshot.payload["chests"] = {
            "items": [
                {
                    "tile": {"x": 70, "y": 12},
                    "capacity": 36,
                    "freeSlots": 35,
                    "contents": [
                        {"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 15, "category": -74}
                    ],
                }
            ]
        }
        # 3 tilled empty tiles available
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "season": "spring",
            "dayOfMonth": 1,
            "companionHasHoe": True,
            "seeds": [],
            "candidateTiles": {
                "tilledEmptyTiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}, {"x": 66, "y": 14}],
                "tillableTiles": [],
                "tilledEmptyCount": 3,
                "tillableCount": 0,
            },
        }

        scheduler = CompanionScheduler(client=mock_transport_client)
        # Mock skill execution results
        scheduler.execute_skill = AsyncMock(return_value={
            "terminalState": "succeeded",
            "completedCount": 3,
            "effects": [{"tile": {"x": 64, "y": 14}, "state": "planted"}],
        })

        res = await scheduler.plant_crop_workflow(
            crop_name_or_id="Parsnip",
            count=3,
            auto_till=True,
            auto_water=True,
            withdraw_from_chest=True,
        )

        assert res["status"] == "succeeded"
        assert res["crop"] == "Parsnip"
        assert res["seedId"] == "(O)472"
        assert res["seedsPlanted"] == 3
        assert res["tilesWatered"] == 3
        assert res["withdrawnFromChest"] is not None
        assert res["withdrawnFromChest"]["itemId"] == "(O)472"
        assert res["withdrawnFromChest"]["count"] == 3

    asyncio.run(run())


def test_scheduler_plant_crop_workflow_chest_withdraw_slot_count_vs_stack_quantity(mock_transport_client):
    """When ChestAction returns completedCount=1 (1 item slot processed) but stack=3 in effects,
    scheduler credits the actual stack quantity (3 seeds) rather than 1."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12,
            "freeSlots": 11,
            "slots": [{"itemId": "(T)Hoe", "name": "Hoe", "category": -99, "stack": 1}],
        }
        mock_transport_client.latest_snapshot.payload["chests"] = {
            "items": [
                {
                    "tile": {"x": 59, "y": 17},
                    "capacity": 36,
                    "freeSlots": 35,
                    "contents": [
                        {"itemId": "(O)CarrotSeeds", "name": "Carrot Seeds", "stack": 3, "category": -74}
                    ],
                }
            ]
        }
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "candidateTiles": {
                "tilledEmptyTiles": [{"x": 61, "y": 21}],
                "tillableTiles": [{"x": 62, "y": 21}, {"x": 63, "y": 21}],
            }
        }

        scheduler = CompanionScheduler(client=mock_transport_client)

        async def mock_execute_skill(skill_id, **kwargs):
            if skill_id == "withdraw-chest":
                return {
                    "terminalState": "succeeded",
                    "status": "executed",
                    "completedCount": 1,  # 1 slot processed by mod
                    "effects": [
                        {"state": "withdrawn", "itemId": "(O)CarrotSeeds", "stack": 3, "chestTile": {"x": 59, "y": 17}}
                    ],
                }
            elif skill_id == "hoe-tiles":
                return {
                    "terminalState": "succeeded",
                    "status": "executed",
                    "completedCount": 2,
                    "effects": [
                        {"state": "hoed", "tile": {"x": 62, "y": 21}},
                        {"state": "hoed", "tile": {"x": 63, "y": 21}},
                    ],
                }
            elif skill_id == "plant-seeds":
                return {
                    "terminalState": "succeeded",
                    "status": "executed",
                    "completedCount": 3,
                    "effects": [
                        {"state": "planted", "tile": {"x": 61, "y": 21}},
                        {"state": "planted", "tile": {"x": 62, "y": 21}},
                        {"state": "planted", "tile": {"x": 63, "y": 21}},
                    ],
                    "details": {"plantedTiles": [{"x": 61, "y": 21}, {"x": 62, "y": 21}, {"x": 63, "y": 21}]},
                }
            elif skill_id == "water-zone":
                return {
                    "terminalState": "succeeded",
                    "status": "executed",
                    "completedCount": 3,
                    "effects": [
                        {"state": "watered", "tile": {"x": 61, "y": 21}},
                        {"state": "watered", "tile": {"x": 62, "y": 21}},
                        {"state": "watered", "tile": {"x": 63, "y": 21}},
                    ],
                }
            return {"terminalState": "succeeded", "completedCount": 1}

        scheduler.execute_skill = mock_execute_skill

        res = await scheduler.plant_crop_workflow(
            crop_name_or_id="Carrot",
            count=3,
            auto_till=True,
            auto_water=True,
            withdraw_from_chest=True,
        )

        assert res["status"] == "succeeded"
        assert res["crop"] == "Carrot"
        assert res["seedsPlanted"] == 3
        assert res["tilesHoed"] == 2
        assert res["tilesWatered"] == 3
        assert res["withdrawnFromChest"]["count"] == 3
        assert res["withdrawnFromChest"]["itemId"] == "(O)CarrotSeeds"

    asyncio.run(run())



def test_scheduler_plant_crop_workflow_blocked_no_seeds(mock_transport_client):
    """When no seeds exist in backpack or chest, returns blocked with clear pendingDecision."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12, "freeSlots": 12, "slots": []
        }
        mock_transport_client.latest_snapshot.payload["chests"] = {
            "items": [
                {
                    "tile": {"x": 70, "y": 12},
                    "capacity": 36,
                    "freeSlots": 35,
                    "contents": [{"itemId": "(O)390", "name": "Stone", "stack": 99}],
                }
            ]
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        res = await scheduler.plant_crop_workflow(
            crop_name_or_id="Carrot",
            count=3,
        )
        assert res["status"] == "blocked"
        assert res["seedsPlanted"] == 0
        assert "No seeds matching 'Carrot' found" in res["pendingDecision"]

    asyncio.run(run())


def test_scheduler_navigate_to_dynamic_shop_and_unsupported_landmark(mock_transport_client, native_compatible_run_dir):
    """navigate_to resolves dynamic shop interactionTile from snapshot, and rejects unsupported landmarks."""
    async def run():
        mock_transport_client.latest_snapshot.payload["shop"] = {
            "shopId": "SeedShop",
            "locationId": "SeedShop",
            "interactionTile": {"x": 4, "y": 17},
        }
        mock_transport_client.execute_navigate_to = AsyncMock(return_value="cmd-nav-dyn-01")
        mock_transport_client.wait_for_result = AsyncMock(
            return_value=Envelope(
                protocol_version="0.1",
                message_type="skill.result",
                message_id="res-nav-dyn-1",
                sender_instance_id="mod-1",
                sequence_number=1,
                world_revision=7,
                sent_at=datetime.now(UTC),
                payload={
                    "commandId": "cmd-nav-dyn-01",
                    "taskId": "task-nav-dyn-1",
                    "terminalState": "succeeded",
                    "completedCount": 1,
                    "details": {"finalLocation": "SeedShop", "finalTile": {"x": 4, "y": 17}},
                },
            )
        )
        scheduler = CompanionScheduler(client=mock_transport_client, run_dir=native_compatible_run_dir)

        # 1. Omitting tile resolves dynamic shop interactionTile
        res = await scheduler.execute_navigate_to(location_id="SeedShop")
        assert res["terminalState"] == "succeeded"
        mock_transport_client.execute_navigate_to.assert_awaited_with(
            location_id="SeedShop",
            tile={"x": 4, "y": 17},
            task_id=mock_transport_client.execute_navigate_to.call_args.kwargs["task_id"],
            idempotency_key=mock_transport_client.execute_navigate_to.call_args.kwargs["idempotency_key"],
            expires_seconds=mock_transport_client.execute_navigate_to.call_args.kwargs["expires_seconds"],
        )

        # 2. Omitting tile for location without dynamic data raises clear unsupported error
        with pytest.raises(SchedulerError, match="no native dynamic interaction data"):
            await scheduler.execute_navigate_to(location_id="Farm", landmark="shipping_bin")

    asyncio.run(run())


def test_scheduler_plant_crop_workflow_withdraw_failure_and_executing(mock_transport_client):
    """Withdraw failures do not inflate seed count, and executing withdraw returns executing immediately."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12, "freeSlots": 12, "slots": []
        }
        mock_transport_client.latest_snapshot.payload["chests"] = {
            "items": [
                {
                    "tile": {"x": 70, "y": 12},
                    "capacity": 36,
                    "freeSlots": 35,
                    "contents": [{"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 5, "category": -74}],
                }
            ]
        }
        scheduler = CompanionScheduler(client=mock_transport_client)

        # Case A: withdraw is still executing/running
        scheduler.withdraw_from_chest = AsyncMock(return_value={
            "status": "executing",
            "terminalState": "running",
            "taskId": "task-with-exec-1",
            "inProgress": True,
            "details": {"taskId": "task-with-exec-1"},
        })

        res_exec = await scheduler.plant_crop_workflow(crop_name_or_id="Parsnip", count=3)
        assert res_exec["status"] == "executing"
        assert res_exec["terminalState"] == "running"
        assert res_exec["inProgress"] is True
        assert res_exec["taskId"] == "task-with-exec-1"
        assert res_exec["seedsPlanted"] == 0

        # Case B: withdraw failed -> do not inflate backpack count
        scheduler.withdraw_from_chest = AsyncMock(return_value={
            "status": "executed",
            "terminalState": "failed",
            "completedCount": 0,
            "error": "Chest occupied by player",
        })

        res_fail = await scheduler.plant_crop_workflow(crop_name_or_id="Parsnip", count=3)
        assert res_fail["status"] == "blocked"
        assert res_fail["seedsPlanted"] == 0
        assert "Failed to withdraw seeds from chest" in res_fail["pendingDecision"]

    asyncio.run(run())


def test_scheduler_plant_crop_workflow_water_failure_not_succeeded(mock_transport_client):
    """If watering fails or is partial, overall status is partial, never succeeded."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12, "freeSlots": 11,
            "slots": [{"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 3, "category": -74}],
        }
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "candidateTiles": {
                "tilledEmptyTiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}, {"x": 66, "y": 14}],
                "tillableTiles": [],
            }
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        scheduler.execute_plant_seeds = AsyncMock(return_value={
            "terminalState": "succeeded",
            "completedCount": 3,
            "details": {"plantedTiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}, {"x": 66, "y": 14}]},
            "effects": [
                {"tile": {"x": 64, "y": 14}, "state": "planted"},
                {"tile": {"x": 65, "y": 14}, "state": "planted"},
                {"tile": {"x": 66, "y": 14}, "state": "planted"},
            ],
        })

        # Case A: Watering failed
        scheduler.execute_tiles = AsyncMock(return_value={
            "status": "executed",
            "terminalState": "failed",
            "completedCount": 0,
            "error": "Watering can empty",
        })

        res_fail = await scheduler.plant_crop_workflow(crop_name_or_id="Parsnip", count=3, auto_water=True)
        assert res_fail["status"] == "partial"  # NOT succeeded!
        assert res_fail["seedsPlanted"] == 3
        assert res_fail["tilesWatered"] == 0
        assert "Watered 0/3 tiles" in res_fail["pendingDecision"]

        # Case B: Watering still executing
        scheduler.execute_tiles = AsyncMock(return_value={
            "status": "executing",
            "terminalState": "running",
            "taskId": "task-water-exec-1",
            "inProgress": True,
            "details": {"taskId": "task-water-exec-1"},
        })

        res_exec = await scheduler.plant_crop_workflow(crop_name_or_id="Parsnip", count=3, auto_water=True)
        assert res_exec["status"] == "executing"
        assert res_exec["terminalState"] == "running"
        assert res_exec["inProgress"] is True

    asyncio.run(run())


def test_scheduler_plant_crop_workflow_real_planted_tiles_and_explicit_target_auto_till(mock_transport_client):
    """Explicit target_tiles with auto_till hoes only untilled tiles, and watering waters actual planted tiles."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12, "freeSlots": 11,
            "slots": [{"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 3, "category": -74}],
        }
        # (64, 14) is already tilled. (65, 14) and (66, 14) are untilled tillable tiles.
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "candidateTiles": {
                "tilledEmptyTiles": [{"x": 64, "y": 14}],
                "tillableTiles": [{"x": 65, "y": 14}, {"x": 66, "y": 14}],
            }
        }
        scheduler = CompanionScheduler(client=mock_transport_client)

        hoed_calls = []
        async def mock_hoe(tiles, location_id, command_id=None):
            hoed_calls.append(tiles)
            return {
                "terminalState": "succeeded",
                "completedCount": len(tiles),
                "effects": [{"tile": t, "state": "hoed"} for t in tiles],
            }
        scheduler.execute_hoe_tiles = AsyncMock(side_effect=mock_hoe)

        # Planting only succeeds on (64, 14) and (66, 14); (65, 14) failed/skipped!
        scheduler.execute_plant_seeds = AsyncMock(return_value={
            "terminalState": "partially-succeeded",
            "completedCount": 2,
            "details": {
                "plantedTiles": [{"x": 64, "y": 14}, {"x": 66, "y": 14}],
                "skippedTiles": [{"x": 65, "y": 14, "reason": "blocked"}],
            },
            "effects": [
                {"tile": {"x": 64, "y": 14}, "state": "planted"},
                {"tile": {"x": 66, "y": 14}, "state": "planted"},
            ],
        })

        water_calls = []
        async def mock_water(tiles, location_id, command_id=None):
            water_calls.append(tiles)
            return {
                "terminalState": "succeeded",
                "completedCount": len(tiles),
                "effects": [{"tile": t, "state": "watered"} for t in tiles],
            }
        scheduler.execute_tiles = AsyncMock(side_effect=mock_water)

        res = await scheduler.plant_crop_workflow(
            crop_name_or_id="Parsnip",
            count=3,
            target_tiles=[{"x": 64, "y": 14}, {"x": 65, "y": 14}, {"x": 66, "y": 14}],
            auto_till=True,
            auto_water=True,
        )

        # 1. Verify auto_till hoed ONLY untilled tiles (65, 14) and (66, 14), NOT (64, 14)
        assert len(hoed_calls) == 1
        assert hoed_calls[0] == [{"x": 65, "y": 14}, {"x": 66, "y": 14}]

        # 2. Verify watering watered EXACTLY the real planted tiles (64, 14) and (66, 14)
        # NOT a prefix slice [64, 65]!
        assert len(water_calls) == 1
        assert water_calls[0] == [{"x": 64, "y": 14}, {"x": 66, "y": 14}]
        assert res["targetTiles"] == [{"x": 64, "y": 14}, {"x": 66, "y": 14}]
        assert res["status"] == "partial"  # planted 2 out of 3 requested

    asyncio.run(run())


def test_scheduler_plant_crop_workflow_skipped_tiles_reporting(mock_transport_client):
    """When planting fails or skips tiles with reason like player-obstruction, report reason in pendingDecision."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12, "freeSlots": 10,
            "slots": [{"slot": 0, "itemId": "(O)472", "name": "Parsnip Seeds", "stack": 5, "quality": 0}],
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        scheduler.query_planting_options = AsyncMock(return_value={
            "candidateTiles": {
                "tilledEmptyTiles": [{"x": 60, "y": 18}, {"x": 61, "y": 21}],
                "tillableTiles": [],
            }
        })
        scheduler.execute_plant_seeds = AsyncMock(return_value={
            "terminalState": "failed",
            "completedCount": 0,
            "skippedCount": 2,
            "details": {
                "skippedTiles": [
                    {"x": 60, "y": 18, "reason": "player-obstruction"},
                    {"x": 61, "y": 21, "reason": "player-obstruction"},
                ]
            },
            "error": None,
        })

        res = await scheduler.plant_crop_workflow(crop_name_or_id="Parsnip", count=2)
        assert res["status"] == "failed"
        assert "player-obstruction" in res["pendingDecision"]
        assert "None" not in res["pendingDecision"]

    asyncio.run(run())


def test_order_candidates_for_contiguous_work_prefers_big_enough_patch():
    from stardew_ai_runtime.scheduler import order_candidates_for_contiguous_work

    # A single near tile and a contiguous 3-tile patch farther away.
    tiles = [
        {"x": 50, "y": 50},
        {"x": 60, "y": 60},
        {"x": 60, "y": 61},
        {"x": 60, "y": 62},
    ]
    ordered = order_candidates_for_contiguous_work(
        tiles, anchor={"x": 50, "y": 50}, preferred_group_size=3
    )
    # The contiguous patch that can satisfy the request comes first.
    assert ordered[:3] == [{"x": 60, "y": 60}, {"x": 60, "y": 61}, {"x": 60, "y": 62}]
    assert ordered[3] == {"x": 50, "y": 50}


def test_order_candidates_for_contiguous_work_keeps_consecutive_tiles_adjacent():
    from stardew_ai_runtime.scheduler import order_candidates_for_contiguous_work

    # Two separate patches, interleaved in scan order.
    tiles = [
        {"x": 10, "y": 10},
        {"x": 20, "y": 20},
        {"x": 11, "y": 10},
        {"x": 21, "y": 20},
    ]
    ordered = order_candidates_for_contiguous_work(tiles, anchor={"x": 10, "y": 10})
    assert len(ordered) == 4
    first_patch = [(t["x"], t["y"]) for t in ordered[:2]]
    second_patch = [(t["x"], t["y"]) for t in ordered[2:]]
    # Each contiguous patch stays together, and pairs inside a patch are adjacent.
    assert set(first_patch) == {(10, 10), (11, 10)}
    assert set(second_patch) == {(20, 20), (21, 20)}
    for patch in (ordered[:2], ordered[2:]):
        assert abs(patch[0]["x"] - patch[1]["x"]) + abs(patch[0]["y"] - patch[1]["y"]) == 1


def test_scheduler_plant_crop_workflow_already_watered_is_goal_satisfied(mock_transport_client):
    """watering that only skips AlreadyWatered tiles is a satisfied goal."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12,
            "freeSlots": 11,
            "slots": [{"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 3, "category": -74}],
        }
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "worldRevision": 77,
            "candidateTiles": {
                "tilledEmptyTiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}, {"x": 66, "y": 14}],
                "tillableTiles": [],
            },
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        scheduler.execute_plant_seeds = AsyncMock(return_value={
            "terminalState": "succeeded",
            "completedCount": 3,
            "details": {"plantedTiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}, {"x": 66, "y": 14}]},
            "effects": [
                {"tile": {"x": 64, "y": 14}, "state": "planted"},
                {"tile": {"x": 65, "y": 14}, "state": "planted"},
                {"tile": {"x": 66, "y": 14}, "state": "planted"},
            ],
        })
        # Mod reports succeeded with 0 watered and 3 skipped: all tiles were already wet.
        scheduler.execute_tiles = AsyncMock(return_value={
            "status": "executed",
            "terminalState": "succeeded",
            "completedCount": 0,
            "skippedCount": 3,
            "effects": [],
        })

        res = await scheduler.plant_crop_workflow(crop_name_or_id="Parsnip", count=3, auto_water=True)

        assert res["status"] == "succeeded"
        assert res["outcome"] == "completed"
        assert res["goalSatisfied"] is True
        assert res["reasonCode"] == "OK"
        assert res["tilesWatered"] == 0
        assert res["pendingDecision"] is None
        assert res["snapshotRevision"] == mock_transport_client.world_revision

    asyncio.run(run())


def test_scheduler_plant_crop_workflow_selects_contiguous_tiles(mock_transport_client):
    """Auto tile selection prefers a contiguous patch large enough for the request."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12,
            "freeSlots": 11,
            "slots": [{"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 3, "category": -74}],
        }
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "searchBounds": {"center": {"x": 50, "y": 50}, "radius": 20},
            "candidateTiles": {
                # A near lone tile first, then a contiguous 3-tile row farther out.
                "tilledEmptyTiles": [
                    {"x": 50, "y": 50},
                    {"x": 60, "y": 60},
                    {"x": 60, "y": 61},
                    {"x": 60, "y": 62},
                ],
                "tillableTiles": [],
            },
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        planted_calls = []

        async def mock_plant(seed_item_id, tiles, location_id, command_id=None):
            planted_calls.append(tiles)
            return {
                "terminalState": "succeeded",
                "completedCount": len(tiles),
                "details": {"plantedTiles": tiles},
                "effects": [{"tile": t, "state": "planted"} for t in tiles],
            }

        scheduler.execute_plant_seeds = mock_plant
        scheduler.execute_tiles = AsyncMock(return_value={
            "status": "executed",
            "terminalState": "succeeded",
            "completedCount": 3,
        })

        res = await scheduler.plant_crop_workflow(crop_name_or_id="Parsnip", count=3, auto_water=True)

        assert planted_calls, "plant should have been dispatched"
        chosen = [(t["x"], t["y"]) for t in planted_calls[0]]
        assert chosen == [(60, 60), (60, 61), (60, 62)]
        assert res["status"] == "succeeded"
        assert res["goalSatisfied"] is True

    asyncio.run(run())


def test_scheduler_water_auto_reports_unified_outcome(mock_transport_client):
    async def run():
        mock_transport_client.latest_snapshot.payload["farmWork"] = {
            "tilledUnwateredTiles": [{"x": 1, "y": 1}, {"x": 2, "y": 1}],
            "tilledUnwateredCount": 2,
            "cropUnwateredTiles": [{"x": 1, "y": 1}, {"x": 2, "y": 1}],
            "cropUnwateredCount": 2,
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        scheduler.execute_tiles = AsyncMock(return_value={
            "status": "executed",
            "terminalState": "succeeded",
            "completedCount": 2,
            "skippedCount": 0,
            "failedCount": 0,
            "effects": [{"tile": {"x": 1, "y": 1}, "state": "watered"}],
        })

        res = await scheduler.water_auto(max_tiles=25)
        assert res["outcome"] == "completed"
        assert res["goalSatisfied"] is True
        assert res["reasonCode"] == "OK"

    asyncio.run(run())


def test_scheduler_plant_crop_workflow_derives_deterministic_subcommand_ids(mock_transport_client):
    """A composite plan step's persisted id reaches each real sub-skill deterministically."""
    async def run():
        mock_transport_client.latest_snapshot.payload["inventory"] = {
            "capacity": 12,
            "freeSlots": 11,
            "slots": [{"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 2, "category": -74}],
        }
        mock_transport_client.latest_snapshot.payload["planting"] = {
            "candidateTiles": {
                "tilledEmptyTiles": [{"x": 64, "y": 14}],
                "tillableTiles": [{"x": 65, "y": 14}],
            }
        }
        scheduler = CompanionScheduler(client=mock_transport_client)
        hoe_ids: list[str | None] = []
        plant_ids: list[str | None] = []
        water_ids: list[str | None] = []

        async def mock_hoe(tiles, location_id, command_id=None):
            hoe_ids.append(command_id)
            return {
                "terminalState": "succeeded",
                "completedCount": len(tiles),
                "effects": [{"tile": t, "state": "hoed"} for t in tiles],
            }

        async def mock_plant(seed_item_id, tiles, location_id, command_id=None):
            plant_ids.append(command_id)
            return {
                "terminalState": "succeeded",
                "completedCount": len(tiles),
                "details": {"plantedTiles": tiles},
                "effects": [{"tile": t, "state": "planted"} for t in tiles],
            }

        async def mock_water(tiles, location_id, command_id=None):
            water_ids.append(command_id)
            return {
                "terminalState": "succeeded",
                "completedCount": len(tiles),
                "effects": [{"tile": t, "state": "watered"} for t in tiles],
            }

        scheduler.execute_hoe_tiles = AsyncMock(side_effect=mock_hoe)
        scheduler.execute_plant_seeds = AsyncMock(side_effect=mock_plant)
        scheduler.execute_tiles = AsyncMock(side_effect=mock_water)

        base = "plan:Save1:t1:s1:attempt-1"
        await scheduler.plant_crop_workflow(
            crop_name_or_id="Parsnip",
            count=2,
            target_tiles=[{"x": 64, "y": 14}, {"x": 65, "y": 14}],
            auto_till=True,
            auto_water=True,
            command_id=base,
        )
        assert hoe_ids == [f"{base}:hoe"]
        assert plant_ids == [f"{base}:plant"]
        assert water_ids == [f"{base}:water"]

    asyncio.run(run())






