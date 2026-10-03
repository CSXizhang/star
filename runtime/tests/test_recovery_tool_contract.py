"""Real observations and validated jobs must survive provider argument formatting."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from test_mcp_argument_validation import game as _game_fixture
from test_native_action_farming import _client, _snapshot_env

from stardew_ai_runtime.scheduler import CompanionScheduler, SchedulerError

game = _game_fixture


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
