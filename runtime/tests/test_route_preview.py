import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.work_state import WorkStore


def test_gateway_route_uses_native_planner_and_returns_bounded_summary():
    async def check():
        scheduler = CompanionScheduler.__new__(CompanionScheduler)
        native = {"reachable": True, "locations": ["AnimalShop", "Forest", "Town", "BusStop", "Farm"],
                  "walkingTiles": 160, "estimatedGameMinutes": 61}
        scheduler._execute_native_action = AsyncMock(return_value={"details": {"route": native}})
        server_scheduler = MagicMock()
        server_scheduler.query_route = scheduler.query_route
        server = create_mcp_server(scheduler=server_scheduler, surface="light")
        _, result = await server.call_tool("call_capability", {"tool": "query_route", "params": {"location_id": "Farm", "tile": {"x": 58, "y": 22}}})
        assert result["result"] == native
        scheduler._execute_native_action.assert_awaited_once_with("inspect-route", {"locationId": "Farm", "tile": {"x": 58, "y": 22}}, tiles=[])
    asyncio.run(check())


@pytest.mark.parametrize("tool,method,operation,reason", [
    ("query_route", "query_route", "navigate_to", "PATH_BLOCKED"),
    ("observe_machines", "query_machines", "collect_machine", "NOT_READY"),
])
def test_failed_recovery_state_write_preserves_observation_and_persisted_block(tmp_path, tool, method, operation, reason):
    async def check():
        store = WorkStore(tmp_path / "data" / "work-state.json")
        target = {"location_id": "Farm", "tile": {"x": 1, "y": 2}}
        store._mutate("S", lambda state: state.branch_failures.append({
            "goalId": "g", "operation": operation, "target": target,
            "reasonCode": reason, "blocked": True, "failures": 3,
        }))
        scheduler = MagicMock()
        native = {"saveId": "S", "reachable": True, "machines": [{"tile": target["tile"], "isReady": True}]}
        setattr(scheduler, method, AsyncMock(return_value=native))
        scheduler.get_status = AsyncMock(return_value={"saveId": "S"})
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, full=True)
        params = {"location_id": "Farm"}
        if tool == "query_route":
            params["tile"] = target["tile"]
        with patch.object(WorkStore, "_write_unlocked", side_effect=OSError("read-only disk")) as write:
            _, result = await server.call_tool(tool, params)
        assert result == native
        write.assert_called_once()
        assert WorkStore(store.state_path).state("S").branch_failures[0]["blocked"]
    asyncio.run(check())


@pytest.mark.parametrize("tool,method,recovery", [
    ("query_route", "query_route", "observe_branch_recovery"),
    ("observe_machines", "query_machines", "observe_machine_recovery"),
])
@pytest.mark.parametrize("failure", ["disconnected", "write-failed", "other-save", "unknown-save", "missing-result-save"])
def test_successful_observation_survives_recovery_bookkeeping_failure(tmp_path, tool, method, recovery, failure):
    async def check():
        native = {"saveId": "S", "reachable": True, "machines": [{"tile": {"x": 1, "y": 2}, "isReady": True}]}
        if failure == "missing-result-save":
            native.pop("saveId")
        scheduler = MagicMock()
        setattr(scheduler, method, AsyncMock(return_value=native))
        status_save = {"other-save": "Other", "unknown-save": "unknown"}.get(failure, "S")
        scheduler.get_status = AsyncMock(return_value={"saveId": status_save})
        if failure == "disconnected":
            scheduler.get_status.side_effect = RuntimeError("transport disconnected after read")
        params = {"location_id": "Farm"}
        if tool == "query_route":
            params["tile"] = {"x": 1, "y": 2}
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, full=True)
        with patch.object(WorkStore, recovery, side_effect=OSError("state file read-only")) as record:
            _, result = await server.call_tool(tool, params)
        assert result == native
        if failure == "write-failed":
            record.assert_called_once()
            assert record.call_args.args[0] == "S"
        else:
            record.assert_not_called()
    asyncio.run(check())
