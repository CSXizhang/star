"""Legacy wrappers cannot hide multiple native commands in one persisted step."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.mcp_server import _PLAN_OPERATION_CALLS, create_mcp_server
from stardew_ai_runtime.work_state import ALLOWED_OPERATIONS, WorkStateError, WorkStore


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    for key in ("STARDEW_LIFE_MODE", "STARDEW_TURN_CONTEXT", "STARDEW_EXTERNAL_CODEX",
                "STARDEW_MCP_SURFACE", "STARDEW_MCP_FULL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision")
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.latest_snapshot = {}
    scheduler.get_status = AsyncMock(return_value={"saveId": "farm"})
    scheduler.plant_crop_workflow = AsyncMock()
    store = WorkStore(tmp_path / "data/work-state.json")
    store.begin_decision("farm", "decision")
    server = create_mcp_server(scheduler=scheduler, run_dir=tmp_path, surface="full")
    return scheduler, store, server


@pytest.mark.parametrize("entry_point", ["submit_plan", "call_capability", "plant_crop_workflow",
                                         "dispatch_plan_operation"])
def test_legacy_wrapper_returns_unsaved_unexecuted_repair_guidance(boundary, entry_point):
    scheduler, store, server = boundary
    params = {"crop_name_or_id": "Potato", "count": 100}
    args = {
        "submit_plan": {"goal_text": "种好", "tasks": [{"title": "种植", "steps": [
            {"operation": "plant_crop_workflow", "params": params}]}]},
        "call_capability": {"tool": "plant_crop_workflow", "params": params},
        "plant_crop_workflow": params,
        "dispatch_plan_operation": {"operation": "plant_crop_workflow", "params": params,
                                    "command_id": "legacy-persisted-command"},
    }
    with pytest.raises(ToolError, match="COMPOSITE_STEP_UNSUPPORTED") as error:
        asyncio.run(server.call_tool(entry_point, args[entry_point]))
    text = str(error.value)
    assert '"saved": false' in text and '"execution": "not_started"' in text
    assert all(operation in text for operation in (
        "withdraw_from_chest", "hoe_tiles", "plant_seeds", "water_tiles",
    ))
    assert store.state("farm").tasks == []
    assert not store.state("farm").decision["selected"]
    scheduler.plant_crop_workflow.assert_not_awaited()


def test_native_combinations_remain_unrestricted_after_wrapper_correction(boundary):
    _, store, server = boundary
    with pytest.raises(WorkStateError, match="COMPOSITE_STEP_UNSUPPORTED"):
        store.submit_plan("farm", goal_text="种好", decision_token="decision", tasks=[{
            "title": "旧包装器", "steps": [{"operation": "plant_crop_workflow", "params": {}}],
        }])
    tiles = [{"x": x, "y": 20} for x in range(100)]
    steps = [
        {"operation": "navigate_to", "params": {"location_id": "FarmHouse", "tile": {"x": 1, "y": 1}}},
        {"operation": "withdraw_from_chest", "params": {"location_id": "FarmHouse",
         "chest_x": 1, "chest_y": 1, "items": [{"itemId": "(O)475", "count": 100}]}},
        {"operation": "navigate_to", "params": {"location_id": "Farm", "tile": {"x": 10, "y": 10}}},
        {"operation": "hoe_tiles", "params": {"location_id": "Farm", "tiles": tiles}},
        {"operation": "plant_seeds", "params": {"location_id": "Farm", "seed_item_id": "(O)475", "tiles": tiles}},
        {"operation": "water_tiles", "params": {"location_id": "Farm", "tiles": tiles}},
        {"operation": "ship_items", "params": {"items": [{"itemId": "(O)24", "count": 1}]}},
    ]
    asyncio.run(server.call_tool("submit_plan", {"goal_text": "种好并发货", "tasks": [{
        "title": "连续行动", "steps": steps,
    }]}))
    saved = store.state("farm").tasks[0].steps
    assert [step.operation for step in saved] == [step["operation"] for step in steps]
    assert saved[1].params["location_id"] == "FarmHouse"
    assert saved[5].params["tiles"] == tiles
    assert "plant_crop_workflow" not in _PLAN_OPERATION_CALLS
    assert "plant_crop_workflow" not in ALLOWED_OPERATIONS
