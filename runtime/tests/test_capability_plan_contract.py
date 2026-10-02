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
    with pytest.raises(Exception, match="MULTIPLE_BUSINESSES"):
        WorkStore.validate_short_job([{"steps": [*steps, {"operation": "water_auto", "params": {}}]}])
    steps[-1]["params"]["tiles"] = [{"x": n, "y": 2} for n in range(64)]
    with pytest.raises(Exception, match="64 native targets"):
        WorkStore.validate_short_job([{"steps": steps}])


def test_all_discovered_step_keys_share_dispatch_contract_and_shop_docs():
    async def check():
        server = create_mcp_server(scheduler=MagicMock(), surface="light")
        _, result = await server.call_tool("discover_capabilities", {})
        entries = {e["name"]: e for group in result["groups"].values() for e in group}
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
