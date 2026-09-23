"""Targeted tests for the grouped observation and native farming/husbandry actions.

Covers the shared contract only: parameter allow-lists, real dispatch shape,
snapshot projections, result mapping, discoverable memory schema and the explicit
trigger validation errors. Native game behaviour itself is verified by the C#
adapters and the isolated in-game acceptance entry, not here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.mcp_server import (
    _PLAN_OPERATION_CALLS,
    _native_action_response,
    create_mcp_server,
)
from stardew_ai_runtime.protocol import (
    NATIVE_ACTION_SKILLS,
    Envelope,
    ProtocolError,
    validate_native_action_parameters,
)
from stardew_ai_runtime.scheduler import (
    CompanionScheduler,
    PolicyViolationError,
    SchedulerError,
)
from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def _snapshot_env(payload_extra: dict | None = None) -> Envelope:
    payload = {
        "capturedRevision": 5,
        "companion": {
            "locationId": "Farm",
            "tileX": 64,
            "tileY": 15,
            "facingDirection": 2,
            "stamina": 250.0,
            "maxStamina": 270.0,
            "waterCanLevel": 3,
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
    }
    if payload_extra:
        payload.update(payload_extra)
    return Envelope.from_mapping(
        {
            "protocolVersion": "0.1",
            "messageType": "world.snapshot",
            "messageId": "snap-native-1",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 1,
            "worldRevision": 5,
            "sentAt": "2026-09-16T00:00:00Z",
            "payload": payload,
        }
    )


def _client(snapshot: Envelope | None = None) -> MagicMock:
    client = MagicMock()
    client.is_connected = True
    client.save_id = "mock-save-id-001"
    client.game_session_id = "mock-session-001"
    client.world_revision = 5
    client.connect = AsyncMock()
    client.handshake = AsyncMock()
    client.close = AsyncMock()
    env = snapshot or _snapshot_env()
    client.latest_snapshot = env
    client.wait_for_snapshot = AsyncMock(return_value=env)
    client.execute_native_action = AsyncMock(return_value="cmd-native-1")
    client.wait_for_result = AsyncMock(
        return_value=Envelope.from_mapping(
            {
                "protocolVersion": "0.1",
                "messageType": "skill.result",
                "messageId": "res-1",
                "correlationId": "msg-exec-1",
                "saveId": "mock-save-id-001",
                "gameSessionId": "mock-session-001",
                "senderInstanceId": "mod-instance-001",
                "sequenceNumber": 1,
                "worldRevision": 6,
                "sentAt": "2026-09-16T00:00:01Z",
                "payload": {
                    "commandId": "cmd-native-1",
                    "taskId": "task-native-1",
                    "terminalState": "succeeded",
                    "completedCount": 1,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "finalWorldRevision": 6,
                    "effects": [{"state": "fertilized"}],
                    "skillId": "apply-fertilizer",
                },
            }
        )
    )
    return client


def test_native_action_parameter_contract() -> None:
    assert set(NATIVE_ACTION_SKILLS) == {
        "refill-watering-can",
        "apply-fertilizer",
        "clear-debris",
        "pickup-items",
        "chop-tree",
        "insert-machine",
        "collect-machine",
        "pet-animal",
        "feed-animals",
        "toggle-animal-door",
        "collect-animal-produce",
    }
    validate_native_action_parameters("clear-debris", {"locationId": "Farm", "tiles": [{"x": 1, "y": 2}]})
    with pytest.raises(ProtocolError, match="unsupported native action skill"):
        validate_native_action_parameters("run-shell", {"locationId": "Farm"})
    with pytest.raises(ProtocolError, match="unsupported parameter"):
        validate_native_action_parameters("clear-debris", {"locationId": "Farm", "tiles": [], "script": "x"})
    with pytest.raises(ProtocolError, match="locationId"):
        validate_native_action_parameters("clear-debris", {"tiles": []})


def test_create_execute_native_action_envelope_shape() -> None:
    env = Envelope.create_execute_native_action(
        sender_instance_id="rt-1",
        sequence_number=3,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=7,
        command_id="cmd-1",
        task_id="task-1",
        skill_id="feed-animals",
        parameters={"locationId": "Coop", "buildingName": "Coop"},
        idempotency_key="save-1:task-1:attempt-1",
        expires_at=Envelope.from_mapping(
            {
                "protocolVersion": "0.1",
                "messageType": "world.snapshot",
                "messageId": "m",
                "senderInstanceId": "s",
                "sequenceNumber": 0,
                "worldRevision": 0,
                "sentAt": "2026-09-16T00:00:00Z",
                "payload": {},
            }
        ).sent_at,
    )
    assert env.message_type == "skill.execute"
    assert env.payload["skillId"] == "feed-animals"
    assert env.payload["parameters"] == {"locationId": "Coop", "buildingName": "Coop"}
    assert env.payload["budgets"]["maxGameMinutes"] == 60


def test_scheduler_apply_fertilizer_dispatches_allowlisted_command() -> None:
    async def run():
        client = _client()
        scheduler = CompanionScheduler(client=client)
        res = await scheduler.apply_fertilizer(
            tiles=[{"x": 64, "y": 14}],
            fertilizer_item_id="(O)368",
            location_id="Farm",
        )
        assert res["terminalState"] == "succeeded"
        assert res["completedCount"] == 1
        client.execute_native_action.assert_awaited_once()
        kwargs = client.execute_native_action.await_args.kwargs
        assert kwargs["skill_id"] == "apply-fertilizer"
        assert kwargs["parameters"] == {
            "locationId": "Farm",
            "tiles": [{"x": 64, "y": 14}],
            "fertilizerItemId": "(O)368",
        }

    import asyncio

    asyncio.run(run())


def test_scheduler_apply_fertilizer_rejects_missing_item_and_tiles() -> None:
    import asyncio

    async def run():
        scheduler = CompanionScheduler(client=_client())
        with pytest.raises(PolicyViolationError):
            await scheduler.apply_fertilizer(tiles=[], fertilizer_item_id="(O)368")
        with pytest.raises(PolicyViolationError, match="fertilizer_item_id"):
            await scheduler.apply_fertilizer(tiles=[{"x": 1, "y": 2}], fertilizer_item_id="  ")

    asyncio.run(run())


def test_scheduler_insert_machine_validates_count_and_item() -> None:
    import asyncio

    async def run():
        scheduler = CompanionScheduler(client=_client())
        with pytest.raises(PolicyViolationError, match="item_count"):
            await scheduler.insert_machine(tile={"x": 70, "y": 12}, item_id="(O)24", item_count=0)
        with pytest.raises(PolicyViolationError, match="item_id"):
            await scheduler.insert_machine(tile={"x": 70, "y": 12}, item_id="")

    asyncio.run(run())


def test_scheduler_chop_tree_dispatches_allowlisted_command() -> None:
    async def run():
        client = _client()
        scheduler = CompanionScheduler(client=client)
        res = await scheduler.chop_tree(tiles=[{"x": 60, "y": 12}], location_id="Farm")
        assert res["terminalState"] == "succeeded"
        client.execute_native_action.assert_awaited_once()
        kwargs = client.execute_native_action.await_args.kwargs
        assert kwargs["skill_id"] == "chop-tree"
        assert kwargs["parameters"] == {"locationId": "Farm", "tiles": [{"x": 60, "y": 12}]}

    import asyncio

    asyncio.run(run())


def test_scheduler_chop_tree_rejects_empty_tiles() -> None:
    import asyncio

    async def run():
        scheduler = CompanionScheduler(client=_client())
        with pytest.raises(PolicyViolationError):
            await scheduler.chop_tree(tiles=[])

    asyncio.run(run())


def test_scheduler_refill_uses_snapshot_refill_tiles() -> None:
    import asyncio

    async def run():
        snapshot = _snapshot_env({"farming": {"location": "Farm", "refillWaterTiles": [{"x": 66, "y": 15}]}})
        client = _client(snapshot)
        scheduler = CompanionScheduler(client=client)
        await scheduler.refill_watering_can(location_id="Farm")
        kwargs = client.execute_native_action.await_args.kwargs
        assert kwargs["skill_id"] == "refill-watering-can"
        assert kwargs["parameters"]["tiles"] == [{"x": 66, "y": 15}]

    asyncio.run(run())


def test_scheduler_refill_without_observation_is_an_actionable_error() -> None:
    import asyncio

    async def run():
        scheduler = CompanionScheduler(client=_client(_snapshot_env()))
        with pytest.raises(SchedulerError, match="no native watering-can refill tile|farming"):
            await scheduler.refill_watering_can(location_id="Farm", max_tiles=2)

    asyncio.run(run())


def test_scheduler_query_farming_projects_companion_tools_and_obstacles() -> None:
    """The farming projection must expose the companion's real tools and stone/twig facts.

    Stone/twig clearance and milk/shear collection depend on knowing both what the
    world holds and which native tools the companion really carries; both come from
    the native observation, never from a guessed tool list.
    """
    import asyncio

    async def run():
        snapshot = _snapshot_env(
            {
                "farming": {
                    "location": "Farm",
                    "refillWaterTiles": [],
                    "groundItems": [
                        {
                            "tile": {"x": 62, "y": 18},
                            "kind": "stone",
                            "itemId": "(O)2",
                            "name": "Stone",
                            "stack": 1,
                            "isWeed": False,
                            "isStone": True,
                            "isTwig": False,
                            "clearTool": "pickaxe",
                        },
                        {
                            "tile": {"x": 61, "y": 18},
                            "kind": "twig",
                            "itemId": "(O)294",
                            "name": "Twig",
                            "stack": 1,
                            "isWeed": False,
                            "isStone": False,
                            "isTwig": True,
                            "clearTool": "axe",
                        },
                    ],
                    "groundItemsTruncated": False,
                    "fertilizedTiles": [],
                    "choppableTrees": [
                        {"tile": {"x": 58, "y": 12}, "kind": "tree", "growthStage": 5, "tapped": False, "width": 1, "height": 1},
                        {"tile": {"x": 59, "y": 12}, "kind": "stump", "growthStage": 0, "tapped": False, "width": 2, "height": 2},
                    ],
                    "choppableTreesTruncated": False,
                    "companionTools": ["Axe", "Hoe", "MilkPail", "Pickaxe", "Shears", "WateringCan"],
                }
            }
        )
        scheduler = CompanionScheduler(client=_client(snapshot))
        farming = await scheduler.query_farming_helpers(location_id="Farm")
        assert farming["companionTools"] == ["Axe", "Hoe", "MilkPail", "Pickaxe", "Shears", "WateringCan"]
        kinds = {item["kind"]: item["clearTool"] for item in farming["groundItems"]}
        assert kinds == {"stone": "pickaxe", "twig": "axe"}
        assert [t["kind"] for t in farming["choppableTrees"]] == ["tree", "stump"]
        assert farming["choppableTreesTruncated"] is False

    asyncio.run(run())


def test_scheduler_query_farming_tools_are_never_invented() -> None:
    """A snapshot without companionTools yields an empty list, not a guessed tool set."""
    import asyncio

    async def run():
        snapshot = _snapshot_env(
            {"farming": {"location": "Farm", "refillWaterTiles": [], "groundItems": [], "groundItemsTruncated": False, "fertilizedTiles": []}}
        )
        scheduler = CompanionScheduler(client=_client(snapshot))
        farming = await scheduler.query_farming_helpers(location_id="Farm")
        assert farming["companionTools"] == []

    asyncio.run(run())


def test_scheduler_query_machines_and_livestock_projections() -> None:
    import asyncio

    async def run():
        snapshot = _snapshot_env(
            {
                "machines": {
                    "truncated": False,
                    "items": [
                        {
                            "tile": {"x": 70, "y": 12},
                            "itemId": "(BC)12",
                            "name": "Keg",
                            "isReady": True,
                            "minutesUntilReady": 0,
                        },
                        {
                            "tile": {"x": 71, "y": 12},
                            "itemId": "(BC)12",
                            "name": "Keg",
                            "isReady": False,
                            "minutesUntilReady": 120,
                        },
                    ],
                },
                "livestock": {
                    "buildingsTruncated": False,
                    "animalsTruncated": False,
                    "buildings": [
                        {
                            "buildingType": "Coop",
                            "indoorsName": "Coop",
                            "tile": {"x": 10, "y": 4},
                            "doorTile": {"x": 11, "y": 6},
                            "animalDoorOpen": True,
                            "doorStateKnown": True,
                            "animalCount": 1,
                            "animalLimit": 4,
                            "hayCount": 9,
                            "hayCapacity": 240,
                            "animals": [{"name": "小黄", "tile": {"x": 60, "y": 12}}],
                        }
                    ],
                    "roamingAnimals": [],
                },
            }
        )
        scheduler = CompanionScheduler(client=_client(snapshot))
        machines = await scheduler.query_machines(location_id="Farm")
        assert machines["count"] == 2
        assert machines["readyCount"] == 1
        livestock = await scheduler.query_livestock()
        assert livestock["buildingCount"] == 1
        assert livestock["animalCount"] == 1
        assert livestock["buildings"][0]["doorTile"] == {"x": 11, "y": 6}

    asyncio.run(run())


def test_scheduler_query_machines_missing_section_is_explicit() -> None:
    import asyncio

    async def run():
        scheduler = CompanionScheduler(client=_client(_snapshot_env()))
        with pytest.raises(SchedulerError, match="machines"):
            await scheduler.query_machines(location_id="Farm")
        with pytest.raises(SchedulerError, match="livestock"):
            await scheduler.query_livestock()

    asyncio.run(run())


def test_native_action_response_mapping_never_fakes_success() -> None:
    ok = _native_action_response("apply-fertilizer", {"status": "executed", "terminalState": "succeeded", "completedCount": 1})
    assert ok["outcome"] == "completed"
    assert ok["goalSatisfied"] is True
    running = _native_action_response("apply-fertilizer", {"status": "executing", "terminalState": "running"})
    assert running["outcome"] == "unknown"
    assert running["goalSatisfied"] is False
    partial = _native_action_response(
        "apply-fertilizer",
        {"status": "executed", "terminalState": "partially-succeeded", "details": {"skipped": [{"reason": "missing-item"}]}},
    )
    assert partial["outcome"] == "partial"
    assert partial["remaining"] == [{"reason": "missing-item"}]
    rejected = _native_action_response(
        "feed-animals",
        {"status": "rejected", "terminalState": "rejected", "error": {"code": "INVALID_PARAMETERS", "message": "x"}},
    )
    assert rejected["outcome"] == "rejected"
    assert rejected["reasonCode"] == "INVALID_PARAMETERS"

    # Precondition skip such as wrong-map must never be mapped to completed / goalSatisfied
    wrong_map = _native_action_response(
        "feed-animals",
        {
            "status": "executed",
            "terminalState": "succeeded",
            "completedCount": 0,
            "skippedCount": 1,
            "effects": [{"state": "skipped", "reason": "wrong-map"}],
        },
    )
    assert wrong_map["outcome"] == "rejected"
    assert wrong_map["goalSatisfied"] is False
    assert wrong_map["reasonCode"] == "wrong-map"


def test_plan_operation_allowlist_covers_native_actions_only() -> None:
    for operation in (
        "observe_farming_helpers",
        "observe_machines",
        "observe_livestock",
        "refill_watering_can",
        "apply_fertilizer",
        "clear_debris",
        "pickup_items",
        "chop_tree",
        "insert_machine",
        "collect_machine",
        "pet_animal",
        "collect_animal_produce",
        "feed_animals",
        "toggle_animal_door",
    ):
        assert operation in _PLAN_OPERATION_CALLS
    assert _PLAN_OPERATION_CALLS["apply_fertilizer"][1] == frozenset(
        {"tiles", "fertilizer_item_id", "location_id"}
    )
    assert "script" not in _PLAN_OPERATION_CALLS["clear_debris"][1]


def test_discover_capabilities_exposes_memory_write_schema(tmp_path: Path) -> None:
    import asyncio

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=MagicMock(), surface="light")
        light_names = {t.name for t in await server.list_tools()}
        # Grouped observation and refill are directly visible on the game surface.
        assert {"observe_farming_helpers", "observe_machines", "observe_livestock", "refill_watering_can"} <= light_names
        # The action tools stay behind grouped discovery (progressive disclosure).
        assert "apply_fertilizer" not in light_names
        assert "insert_machine" not in light_names

        _, discovered = await server.call_tool("discover_capabilities", {"group": "memory"})
        schema = discovered["memoryWriteSchema"]
        assert schema["remember_intent"]["trigger_shapes"]["calendar"]["example"]["type"] == "calendar"
        assert {"type": "daily"} in schema["remember_intent"]["invalid_examples"]
        assert schema["submit_plan"]["step_shape"]["operation"]

        _, farming = await server.call_tool("discover_capabilities", {"group": "farming_helpers"})
        names = {entry["name"] for entry in farming["groups"]["farming_helpers"]}
        assert {"refill_watering_can", "apply_fertilizer", "clear_debris", "pickup_items"} <= names

        _, livestock = await server.call_tool("discover_capabilities", {"group": "livestock"})
        names = {entry["name"] for entry in livestock["groups"]["livestock"]}
        assert {"observe_livestock", "pet_animal", "collect_animal_produce", "feed_animals", "toggle_animal_door"} <= names

        _, forestry = await server.call_tool("discover_capabilities", {"group": "forestry"})
        assert [entry["name"] for entry in forestry["groups"]["forestry"]] == ["chop_tree"]

    asyncio.run(run())


def test_trigger_validation_names_allowed_kinds(tmp_path: Path) -> None:
    store = WorkStore(tmp_path / "work-state.json")
    for bad in ({"type": "daily"}, {"type": "date", "date": "spring 3"}, {"type": "next_day"}):
        with pytest.raises(WorkStateError) as exc:
            store.remember_intent("save-1", intent="x", kind="todo", trigger=bad)
        message = str(exc.value)
        assert "calendar" in message and "inventory" in message and "crop" in message
        assert "not supported trigger types" in message

    with pytest.raises(WorkStateError, match="requires an explicit trigger"):
        store.remember_intent("save-1", intent="x", kind="todo")

    todo = store.remember_intent(
        "save-1",
        intent="种子够了就播种",
        kind="todo",
        trigger={"type": "inventory", "itemId": "(O)472", "minCount": 2},
    )
    assert todo["kind"] == "todo"
    assert todo["todo"]["trigger"]["type"] == "inventory"


def test_refill_public_tool_forwards_optional_explicit_native_tile(tmp_path: Path) -> None:
    import asyncio
    async def run():
        scheduler = MagicMock()
        scheduler.refill_watering_can = AsyncMock(return_value={"status": "executed", "terminalState": "rejected", "completedCount": 0,
            "skippedCount": 1, "effects": [{"state": "skipped", "reason": "not-refillable"}]})
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="light")
        tiles = [{"x": 64, "y": 19}]
        _, result = await server.call_tool("refill_watering_can", {"tiles": tiles, "location_id": "Farm"})
        scheduler.refill_watering_can.assert_awaited_once_with(location_id="Farm", max_tiles=4, tiles=tiles)
        assert result["outcome"] == "rejected" and result["reasonCode"] == "not-refillable"
    asyncio.run(run())
