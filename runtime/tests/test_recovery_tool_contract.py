"""Real observations and validated jobs must survive provider argument formatting."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from test_mcp_argument_validation import game as _game_fixture
from test_native_action_farming import _client, _snapshot_env

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import CompanionScheduler, SchedulerError

game = _game_fixture


@pytest.mark.parametrize("scope,observation,count", [
    ("FarmHouse", "not-observed", None), ("Farm", "unavailable", None), ("Farm", "observed", 5),
])
def test_farm_queries_from_house_preserve_real_farm_scope_and_unknown_counts(scope, observation, count):
    async def run():
        tiles = [{"x": 65 + i, "y": 15} for i in range(count or 0)]
        env = _snapshot_env({
            "companion": {"locationId": "FarmHouse", "stamina": 270, "waterCanLevel": 40},
            "farmWork": {"locationId": scope, "observationStatus": observation,
                         "tilledUnwateredTiles": tiles, "tilledUnwateredCount": count or 0,
                         "cropUnwateredTiles": tiles, "cropUnwateredCount": count or 0,
                         "matureCrops": [], "matureCropCount": 0}})
        client = _client(env)
        scheduler = CompanionScheduler(client=client)
        server = create_mcp_server(scheduler=scheduler)
        for detail in (False, True):
            _, result = await server.call_tool("query_farm_work", {"detail": detail})
            farm = result["farmWork"]
            assert farm["locationId"] == scope and farm["observationStatus"] == observation
            assert farm["cropUnwateredCount"] == count and farm["tilledUnwateredCount"] == count
            if detail:
                assert result["companion"]["locationId"] == "FarmHouse"
                assert farm["cropUnwateredTiles"] == (tiles if count is not None else None)
            else:
                assert result["currentCompanionLocationId"] == "FarmHouse"
                assert result["needsNavigation"] is True
        overview = await scheduler.get_work_overview()
        assert overview["farmWork"]["cropUnwateredCount"] == count
        assert overview["farmWork"]["missing"] is (count is None)
        client.execute_native_action.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["water_auto", "harvest_auto"])
@pytest.mark.parametrize("observed", [False, True])
def test_auto_farm_actions_reject_house_scope_before_no_work_or_coordinate_dispatch(operation, observed):
    async def run():
        env = _snapshot_env({
            "companion": {"locationId": "FarmHouse", "stamina": 270, "waterCanLevel": 40},
            "farmWork": {"locationId": "Farm" if observed else "FarmHouse",
                         "observationStatus": "observed" if observed else "not-observed",
                         "tilledUnwateredTiles": [{"x": 65, "y": 15}] if observed else [],
                         "cropUnwateredTiles": [{"x": 65, "y": 15}] if observed else [],
                         "matureCrops": [{"x": 65, "y": 15, "cropId": "(O)24"}] if observed else []}})
        client = _client(env)
        scheduler = CompanionScheduler(client=client)
        scheduler.execute_tiles = AsyncMock()
        scheduler.execute_skill = AsyncMock()
        with pytest.raises(SchedulerError, match="[Nn]avigate to Farm first"):
            await getattr(scheduler, operation)()
        scheduler.execute_tiles.assert_not_called()
        scheduler.execute_skill.assert_not_called()
        client.execute_native_action.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("wire_tiles", [
    {"item": [{"x": "55", "y": "17"}, {"x": "56", "y": "17"}]},
    [{"item": [{"x": "55", "y": "17"}, {"x": "56", "y": "17"}]}],
])
@pytest.mark.parametrize("entry_point", ["call_capability", "submit_plan", "plant_seeds"])
def test_wrapped_native_tiles_select_one_valid_planting_job(game, wire_tiles, entry_point):
    _, store, server = game
    params = {"seed_item_id": "(O)475", "tiles": wire_tiles}
    if entry_point == "call_capability":
        asyncio.run(server.call_tool(entry_point, {"tool": "plant_seeds", "params": params}))
    elif entry_point == "submit_plan":
        asyncio.run(server.call_tool(entry_point, {"tasks": [{"title": "补种", "steps": [
            {"operation": "plant_seeds", "params": params}]}], "goal_text": "补种"}))
    else:
        asyncio.run(server.call_tool(entry_point, params))
    steps = store.state("save").tasks[0].steps
    assert len(steps) == 1
    assert steps[0].params["tiles"] == [{"x": 55, "y": 17}, {"x": 56, "y": 17}]


@pytest.mark.parametrize("alias", ["tile", "tile_x", "x"])
def test_direct_navigation_aliases_persist_canonical_destination(game, alias):
    _, store, server = game
    params = {"location_id": "Farm"}
    params.update({"tile": {"x": 55, "y": 17}} if alias == "tile" else
                  {alias: 55, "tile_y" if alias == "tile_x" else "y": 17})
    asyncio.run(server.call_tool("navigate_to", params))
    persisted = store.state("save").tasks[0].steps[0].params
    assert persisted["location_id"] == "Farm"
    assert persisted["tile"] == {"x": 55, "y": 17}
    assert not {"x", "y", "tile_x", "tile_y"} & persisted.keys()


@pytest.mark.parametrize("params", [
    {"location_id": "Farm"},
    {"location_id": "Farm", "x": 55},
    {"location_id": "Farm", "x": 55, "y": 17, "tile": {"x": 56, "y": 17}},
])
def test_invalid_direct_navigation_keeps_decision_available(game, params):
    _, store, server = game
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("navigate_to", params))
    assert not store.state("save").decision["selected"]
    assert not store.state("save").tasks


@pytest.mark.parametrize("operation,params", [
    ("plant_seeds", {"seed_item_id": "(O)475", "tiles": [{"x": True, "y": False}]}),
    ("plant_seeds", {"seed_item_id": "(O)475", "tiles": [{"x": -1, "y": 17}]}),
    ("navigate_to", {"location_id": "Farm", "x": True, "y": 17}),
    ("navigate_to", {"location_id": "Farm", "tile": {"x": 55, "y": False}}),
])
def test_direct_targets_reject_boolean_coercion_before_selection(game, operation, params):
    _, store, server = game
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool(operation, params))
    assert not store.state("save").decision["selected"]
    assert not store.state("save").tasks


def test_refill_inspects_distant_native_sources_when_local_radius_is_empty(native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=_client(_snapshot_env({"farming": {
            "location": "Farm", "refillWaterTiles": [], "refillWaterMapComplete": False}})),
            run_dir=native_compatible_run_dir)
        scheduler.query_production = AsyncMock(return_value={"locationId": "Farm",
            "waterRefillTiles": [{"x": 20, "y": 30}], "waterRefillMapComplete": True})
        scheduler._execute_native_action = AsyncMock(return_value={"terminalState": "succeeded"})
        await scheduler.refill_watering_can()
        scheduler.query_production.assert_awaited_once_with(location_id="Farm")
        sent = scheduler._execute_native_action.await_args.kwargs
        assert sent["tiles"] == [{"x": 20, "y": 30}]
        assert sent["timeout_seconds"] == 120.0
    asyncio.run(run())


def test_refill_does_not_guess_sources_when_native_full_map_is_empty(native_compatible_run_dir):
    async def run():
        scheduler = CompanionScheduler(client=_client(_snapshot_env({"farming": {
            "location": "Farm", "refillWaterTiles": []}})), run_dir=native_compatible_run_dir)
        scheduler.query_production = AsyncMock(return_value={"waterRefillTiles": [], "waterRefillMapComplete": True})
        scheduler._execute_native_action = AsyncMock()
        with pytest.raises(SchedulerError, match="this map"):
            await scheduler.refill_watering_can()
        scheduler._execute_native_action.assert_not_awaited()
    asyncio.run(run())
