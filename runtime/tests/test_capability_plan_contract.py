import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stardew_ai_runtime.mcp_server import (
    _PLAN_OPERATION_CALLS,
    CAPABILITY_GROUPS,
    create_mcp_server,
)
from stardew_ai_runtime.work_state import WorkStore


def test_discovery_separates_direct_navigation_aliases_from_step_contract():
    async def check():
        server = create_mcp_server(scheduler=MagicMock(), surface="light")
        _, result = await server.call_tool("discover_capabilities", {"group": "movement"})
        entry = next(e for e in result["groups"]["movement"] if e["name"] == "navigate_to")
        assert {"tile_x", "tile_y", "x", "y"}.issubset(entry["directCall"]["parameters"])
        assert set(entry["planStep"]["parameters"]) == _PLAN_OPERATION_CALLS["navigate_to"][1]
        assert set(entry["planStep"]["parameters"]) == {"location_id", "tile", "landmark"}
        assert entry["planStep"]["unknownKeys"] == "rejected"
        assert result["availableGroups"] == sorted(CAPABILITY_GROUPS)
        assert set(result["groups"]) == {"movement"}

    asyncio.run(check())


def test_missing_destination_is_rejected_before_job_selection(tmp_path):
    async def check():
        sched = MagicMock()
        sched.run_dir = str(tmp_path)
        sched.get_status = AsyncMock(return_value={"saveId": "S"})
        sched.latest_snapshot = {"payload": {"shop": {"locationId": "SeedShop",
                                                      "interactionTile": {"x": 4, "y": 5}}}}
        store = WorkStore(tmp_path / "data/work-state.json")
        store.begin_decision("S", "nav")
        job = {"goal_text": "照料鸡", "tasks": [{"title": "进鸡舍", "steps": [{"operation": "navigate_to",
                                "params": {"location_id": "Farm", "landmark": "counter"}}]}]}
        with patch.dict("os.environ", {"STARDEW_DECISION_TOKEN": "nav"}):
            server = create_mcp_server(run_dir=tmp_path, scheduler=sched, surface="light")
            with pytest.raises(Exception, match="observed destination tile"):
                await server.call_tool("submit_plan", job)
            assert not store.state("S").tasks
            job["tasks"][0]["steps"][0]["params"] = {"location_id": "SeedShop"}
            _, result = await server.call_tool("submit_plan", job)
        assert result["executionScope"] == "one_short_job"
        assert result["handoff"] == "end_decision"
        assert store.state("S").tasks[0].steps[0].params["tile"] == {"x": 4, "y": 5}
    asyncio.run(check())


def test_animal_care_trip_is_one_bounded_business():
    steps = [{"operation": "feed_animals", "params": {"building_name": "CoopA"}},
             {"operation": "navigate_to", "params": {"location_id": "Farm", "tile": {"x": 12, "y": 8}}},
             *[{"operation": "pet_animal", "params": {"location_id": "Farm", "animal_id": str(n)}}
               for n in range(10)],
             {"operation": "pickup_items", "params": {"location_id": "CoopA", "tiles": [{"x": 1, "y": 2}]}}]
    WorkStore.validate_short_job([{"steps": steps}])
    WorkStore.validate_short_job([{"steps": [*steps, {"operation": "water_auto", "params": {}}]}])
    steps[-1]["params"]["tiles"] = [{"x": n, "y": 2} for n in range(100)]
    WorkStore.validate_short_job([{"steps": steps}])


def test_all_discovered_step_keys_share_dispatch_contract_and_shop_docs():
    async def check():
        server = create_mcp_server(scheduler=MagicMock(), surface="light")
        entries = {}
        for group in CAPABILITY_GROUPS:
            _, result = await server.call_tool("discover_capabilities", {"group": group})
            entries.update({e["name"]: e for e in result["groups"][group]})
        for name, entry in entries.items():
            if name in _PLAN_OPERATION_CALLS:
                assert set(entry["planStep"]["parameters"]) == _PLAN_OPERATION_CALLS[name][1]
            else:
                assert entry["planStep"] is None
        assert "animal catalog: observe_building_services" in entries["query_shop"]["description"]
        assert "ordinary items: query_shop" in entries["observe_building_services"]["description"]
        assert "revise" in entries["manage_goal"]["directCall"]["allowedValues"]["action"]
        assert "update" not in entries["manage_goal"]["directCall"]["allowedValues"]["action"]

    asyncio.run(check())


def test_discovery_gives_nested_required_shapes_and_valid_examples():
    async def check():
        server = create_mcp_server(scheduler=MagicMock(), surface="light")
        _, index = await server.call_tool("discover_capabilities", {})
        assert index["groups"] == {}
        _, result = await server.call_tool("discover_capabilities", {"group": "farm"})
        entries = {e["name"]: e for e in result["groups"]["farm"]}
        plant = entries["plant_seeds"]["planStep"]
        assert set(plant["required"]) == {"seed_item_id", "tiles"}
        assert plant["parameters"]["tiles"]["items"]["required"] == ["x", "y"]
        assert plant["parameters"]["tiles"]["items"]["properties"]["x"]["minimum"] == 0
        assert entries["water_tiles"]["planStep"]["example"]["params"]["tiles"] == [{"x": 69, "y": 18}, {"x": 64, "y": 23}]
        assert "maxItems" not in entries["water_tiles"]["planStep"]["parameters"]["tiles"]
    asyncio.run(check())



def test_mixed_cross_map_job_is_saved_with_each_operation_contract(tmp_path, monkeypatch):
    async def check():
        sched = MagicMock()
        sched.run_dir = str(tmp_path)
        sched.get_status = AsyncMock(return_value={"saveId": "S"})
        store = WorkStore(tmp_path / "data/work-state.json")
        store.begin_decision("S", "choice")
        monkeypatch.setenv("STARDEW_DECISION_TOKEN", "choice")
        server = create_mcp_server(run_dir=tmp_path, scheduler=sched, surface="light")
        tiles = [{"x": i, "y": 5} for i in range(100)]
        steps = [{"operation": "navigate_to", "params": {"location_id": "FarmHouse", "tile": {"x": 1, "y": 1}}},
                 {"operation": "withdraw_from_chest", "params": {"location_id": "FarmHouse", "chest_x": 1, "chest_y": 1, "items": [{"itemId": "(O)475", "count": 100}]}},
                 {"operation": "navigate_to", "params": {"location_id": "Farm", "tile": {"x": 5, "y": 5}}},
                 {"operation": "hoe_tiles", "params": {"location_id": "Farm", "tiles": tiles}},
                 {"operation": "plant_seeds", "params": {"location_id": "Farm", "seed_item_id": "(O)475", "tiles": tiles}},
                 {"operation": "water_tiles", "params": {"location_id": "Farm", "tiles": tiles}}]
        _, result = await server.call_tool("submit_plan", {"goal_text": "种完箱中种子", "tasks": [{"title": "取种开垦种浇", "steps": steps}]})
        assert result["executionScope"] == "one_short_job"
        saved = store.state("S").tasks[0].steps
        assert [step.operation for step in saved] == [step["operation"] for step in steps]
        assert saved[1].params["location_id"] == "FarmHouse"
        assert saved[-1].params["tiles"] == tiles
    asyncio.run(check())
