"""Provider wire arguments must be validated before selecting or dispatching a job."""

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from support.validated_game import game as game

from stardew_ai_runtime.mcp_server import create_mcp_server


# Exercise each wire form and both adapters without their Cartesian product.
@pytest.mark.parametrize("wire_number,entry_point", [
    (55, "call_capability"), ("55", "submit_plan"), ("55.0", "call_capability"),
])
def test_chest_wire_numbers_are_persisted_and_dispatched_as_integers(game, wire_number, entry_point):
    scheduler, store, server = game
    params = {"chest_x": wire_number, "chest_y": "17", "count": "3",
              "item_id": "(O)153", "location_id": "Farm"}

    async def run():
        if entry_point == "call_capability":
            await server.call_tool(entry_point, {"tool": "withdraw_from_chest", "params": params})
        else:
            await server.call_tool(entry_point, {"goal_text": "恢复体力", "tasks": [{
                "title": "拿绿藻", "steps": [{"operation": "withdraw_from_chest", "params": params}]}]})
        step = store.state("save").tasks[0].steps[0]
        assert step.params == {**params, "chest_x": 55, "chest_y": 17, "count": 3}
        assert all(type(step.params[key]) is int for key in ("chest_x", "chest_y", "count"))
        scheduler.withdraw_from_chest.assert_not_awaited()
        worker = create_mcp_server(run_dir=scheduler.run_dir, scheduler=scheduler, surface="full")
        await worker.call_tool("run_next_step", {})
        scheduler.withdraw_from_chest.assert_awaited_once()
        sent = scheduler.withdraw_from_chest.await_args.kwargs
        assert all(type(sent[key]) is int for key in ("chest_x", "chest_y", "count"))
        assert sent["command_id"] == store.state("save").tasks[0].steps[0].command_id
        assert store.state("save").tasks[0].steps[0].status == "completed"

    asyncio.run(run())


@pytest.mark.parametrize("wrap_tasks", [False, True])
def test_item_wrapped_steps_preserve_order_and_canonical_array_schema(game, wrap_tasks):
    scheduler, store, server = game
    steps = [{"operation": "navigate_to", "params": {"location_id": "Farm", "tile": {"x": 54, "y": 17}}},
             {"operation": "withdraw_from_chest", "params": {
                 "chest_x": "55", "chest_y": "17", "item_id": "(O)153", "count": "3"}}]
    tasks = [{"title": "拿绿藻", "steps": {"item": steps}}]

    async def run():
        await server.call_tool("submit_plan", {"goal_text": "恢复体力",
                                              "tasks": {"item": tasks} if wrap_tasks else tasks})
        stored = store.state("save").tasks[0].steps
        assert [step.operation for step in stored] == [step["operation"] for step in steps]
        assert stored[1].params["chest_x"] == 55
        scheduler.withdraw_from_chest.assert_not_awaited()
        schema = next(tool for tool in await server.list_tools() if tool.name == "submit_plan").inputSchema
        assert schema["properties"]["tasks"]["type"] == "array"
        assert schema["properties"]["tasks"]["maxItems"] == 1
        assert schema["$defs"]["ShortJobTask"]["properties"]["steps"]["maxItems"] == 32

    asyncio.run(run())


@pytest.mark.parametrize("field,value,entry_point", [
    ("chest_x", True, "call_capability"),
    ("chest_x", "55.5", "submit_plan"),
    ("chest_x", "not-a-coordinate", "call_capability"),
    ("chest_x", -1, "submit_plan"),
    ("chest_y", False, "submit_plan"),
    ("count", True, "call_capability"),
    ("count", "3.5", "submit_plan"),
    ("count", 0, "call_capability"),
])
def test_invalid_coordinates_or_counts_do_not_consume_decision_or_dispatch(game, field, value, entry_point):
    scheduler, store, server = game
    params = {"chest_x": 55, "chest_y": 17, "count": 3, "item_id": "(O)153", field: value}

    async def run():
        with pytest.raises(ToolError):
            if entry_point == "call_capability":
                await server.call_tool(entry_point, {"tool": "withdraw_from_chest", "params": params})
            else:
                await server.call_tool(entry_point, {"goal_text": "恢复体力", "tasks": [{
                    "title": "拿绿藻", "steps": [{"operation": "withdraw_from_chest", "params": params}]}]})
        assert not store.state("save").decision["selected"]
        assert not store.list_tasks("save")
        assert not store.list_goals("save")
        scheduler.withdraw_from_chest.assert_not_awaited()

    asyncio.run(run())


def test_plan_parameter_validation_preserves_allowed_target_location(game):
    _, store, server = game
    asyncio.run(server.call_tool("submit_plan", {"goal_text": "翻地", "tasks": [{
        "title": "翻地", "steps": [{"operation": "hoe_tiles", "params": {
            "location_id": "IslandWest", "tiles": [{"x": "12", "y": "8"}]}}]}]}))
    assert store.state("save").tasks[0].steps[0].params == {
        "location_id": "IslandWest", "tiles": [{"x": 12, "y": 8}]}


@pytest.mark.parametrize('operation,params,entry_point', [
    ('water_zone', {'center_x': True, 'center_y': 17}, 'call_capability'),
    ('water_auto', {'max_tiles': True}, 'submit_plan'),
    ('navigate_to', {'location_id': 'Farm', 'tile': {'x': True, 'y': 17}}, 'call_capability'),
    ('hoe_tiles', {'tiles': [{'x': 55, 'y': False}]}, 'submit_plan'),
])
def test_shared_parser_never_turns_boolean_targets_into_integers(game, operation, params, entry_point):
    _, store, server = game
    with pytest.raises(ToolError, match="Boolean"):
        if entry_point == "call_capability":
            asyncio.run(server.call_tool(entry_point, {"tool": operation, "params": params}))
        else:
            asyncio.run(server.call_tool(entry_point, {"goal_text": "农活", "tasks": [{
                "title": "农活", "steps": [{"operation": operation, "params": params}]}]}))
    assert not store.state("save").decision["selected"]
    assert not store.list_tasks("save")
