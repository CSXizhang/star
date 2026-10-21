"""Real mcode wire shapes must survive both client and MCP validation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.work_state import WorkStore


def setup_game(tmp_path, monkeypatch):
    monkeypatch.setenv("STARDEW_MCP_WIRE_STYLE", "mcode")
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision")
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.latest_snapshot = {}
    scheduler.get_status = AsyncMock(return_value={"saveId": "save"})
    store = WorkStore(tmp_path / "data" / "work-state.json")
    store.begin_decision("save", "decision")
    return store, create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="light")


@pytest.mark.parametrize("wrapped_steps", [True, False])
def test_real_singleton_item_job_selects_exact_tile(tmp_path, monkeypatch, wrapped_steps):
    store, server = setup_game(tmp_path, monkeypatch)
    step = {"operation": "water_tiles", "params": {"location_id": "Farm",
            "tiles": {"item": {"x": "65", "y": "15"}}, "include_empty_tiles": "false"}}
    task = {"title": "单株浇水", "steps": {"item": step} if wrapped_steps else [step]}
    asyncio.run(server.call_tool("submit_plan", {"goal_text": "只浇这一株", "tasks": [task]}))
    assert store.state("save").decision["selected"] is True
    assert store.state("save").tasks[0].steps[0].params == {
        "location_id": "Farm", "tiles": [{"x": 65, "y": 15}], "include_empty_tiles": False}


def test_mcode_schema_inlines_references_and_advertises_only_supported_wrappers(tmp_path, monkeypatch):
    _, server = setup_game(tmp_path, monkeypatch)

    async def run():
        tools = {tool.name: tool for tool in await server.list_tools()}
        plan = tools["submit_plan"].inputSchema
        assert "$ref" not in str(plan)
        canonical, wrapper = plan["properties"]["tasks"]["anyOf"]
        assert canonical["minItems"] == canonical["maxItems"] == 1
        assert wrapper["additionalProperties"] is False
        steps = canonical["items"]["properties"]["steps"]["anyOf"][0]
        assert steps["minItems"] == 1 and steps["maxItems"] == 32
        assert canonical["items"]["properties"]["dependencies"]["maxItems"] == 0
        observation = tools["observe_farm_space"].inputSchema
        assert "$ref" not in str(observation)
        region = observation["properties"]["region"]["anyOf"][0]
        assert region["properties"]["width"]["minimum"] == 1
        assert region["properties"]["x"]["type"] == "integer"

    asyncio.run(run())


@pytest.mark.parametrize("steps", [
    {"item": []}, {"item": [{"operation": "water_auto"}] * 33},
    {"item": {"operation": "water_auto"}, "extra": 1},
    {"item": {"operation": "water_tiles", "params": {"tiles": {"item": {"x": True, "y": "15"}}}}},
    {"item": {"operation": "water_zone", "params": {"center_x": "65.5", "center_y": "15"}}},
])
def test_unsafe_or_ambiguous_item_shapes_select_nothing(tmp_path, monkeypatch, steps):
    store, server = setup_game(tmp_path, monkeypatch)
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("submit_plan", {"goal_text": "浇水",
            "tasks": [{"title": "浇水", "steps": steps}]}))
    assert not store.state("save").decision["selected"]
    assert not store.state("save").tasks
    assert not store.state("save").goals


@pytest.mark.parametrize("wrapped", [False, True])
def test_withdraw_item_counts_reach_native_validation_as_integers(tmp_path, monkeypatch, wrapped):
    store, server = setup_game(tmp_path, monkeypatch)
    items = [{"itemId": "(O)472", "count": "6"}]
    params = {"chest_x": "66", "chest_y": "20", "items": {"item": items[0]} if wrapped else items}
    asyncio.run(server.call_tool("submit_plan", {"goal_text": "Plant available seeds", "tasks": [
        {"title": "Fetch seeds", "steps": [{"operation": "withdraw_from_chest", "params": params}]}]}))
    stored = store.state("save").tasks[0].steps[0].params["items"]
    assert stored == [{"itemId": "(O)472", "count": 6}]
    assert CompanionScheduler._validate_withdraw_items(items=stored) == stored


@pytest.mark.parametrize("count", [True, 0, -1, 1.5, "1.5", "0", "-1", "six"])
def test_invalid_withdraw_quantity_rejects_whole_job_before_navigation(tmp_path, monkeypatch, count):
    store, server = setup_game(tmp_path, monkeypatch)
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("submit_plan", {"goal_text": "Plant available seeds", "tasks": [
            {"title": "Fetch seeds", "steps": [
                {"operation": "navigate_to", "params": {"location_id": "Farm", "tile": {"x": 66, "y": 21}}},
                {"operation": "withdraw_from_chest", "params": {"chest_x": 66, "chest_y": 20,
                 "items": [{"itemId": "(O)472", "count": count}]}}]}]}))
    assert not store.state("save").decision["selected"]
    assert not store.state("save").tasks
    assert not store.state("save").goals


def test_singleton_string_item_wrapper_selects_seed_return(tmp_path, monkeypatch):
    store, server = setup_game(tmp_path, monkeypatch)
    asyncio.run(server.call_tool("submit_plan", {"goal_text": "Return unused seeds", "tasks": [
        {"title": "Return seeds", "steps": [{"operation": "deposit_to_chest", "params": {
            "chest_x": "66", "chest_y": "20", "item_ids": {"item": "(O)472"}}}]}]}))
    assert store.state("save").tasks[0].steps[0].params["item_ids"] == ["(O)472"]


@pytest.mark.parametrize("value", [True, None, 472, {"item": "(O)472", "extra": "ambiguous"}])
def test_invalid_seed_return_wrapper_selects_nothing(tmp_path, monkeypatch, value):
    store, server = setup_game(tmp_path, monkeypatch)
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("submit_plan", {"goal_text": "Return unused seeds", "tasks": [
            {"title": "Return seeds", "steps": [{"operation": "deposit_to_chest", "params": {
                "chest_x": 66, "chest_y": 20, "item_ids": {"item": value}}}]}]}))
    assert not store.state("save").decision["selected"]
    assert not store.state("save").tasks


def test_real_mixed_item_wrappers_preserve_two_withdraw_quantities(tmp_path, monkeypatch):
    store, server = setup_game(tmp_path, monkeypatch)
    items = {"item": {"item": [{"count": "1", "itemId": "(O)472"},
                              {"item": {"count": "1", "itemId": "(O)24"}}]}}
    asyncio.run(server.call_tool("submit_plan", {"goal_text": "Check two supplies", "tasks": [
        {"title": "Fetch both", "steps": [{"operation": "withdraw_from_chest", "params": {
            "chest_x": 66, "chest_y": 20, "items": items}}]}]}))
    stored = store.state("save").tasks[0].steps[0].params["items"]
    assert stored == [{"itemId": "(O)472", "count": 1}, {"itemId": "(O)24", "count": 1}]
    assert CompanionScheduler._validate_withdraw_items(items=stored) == stored


def test_multi_item_payload_cannot_hide_in_one_item_slot(tmp_path, monkeypatch):
    store, server = setup_game(tmp_path, monkeypatch)
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("submit_plan", {"goal_text": "Check supplies", "tasks": [
            {"title": "Fetch supplies", "steps": [{"operation": "withdraw_from_chest", "params": {
                "chest_x": 66, "chest_y": 20, "items": [{"item": [
                    {"itemId": "(O)472", "count": 1}, {"itemId": "(O)24", "count": 1}]},
                    {"itemId": "(O)472", "count": 1}]}}]}]}))
    assert not store.state("save").decision["selected"]
    assert not store.state("save").tasks
