"""Saved candidate scopes bound auto selection while preserving step ownership."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.mcp_server import _PLAN_OPERATION_CALLS, create_mcp_server
from stardew_ai_runtime.work_state import WorkStore, bind_execution_scope_params


@pytest.mark.parametrize("operation", ["water_auto", "harvest_auto"])
def test_scope_projection_is_pure_idempotent_and_intersects_requested_candidates(operation):
    scope = {"objectiveScope": {"executionScope": {"locationId": "Farm",
        "tiles": [{"x": 62, "y": 27}, {"x": 66, "y": 23}]}}}
    params = {"max_tiles": 100, "target_tiles": [{"x": 65, "y": 15}, {"x": 66, "y": 23}]}
    old_scope, old_params = deepcopy(scope), deepcopy(params)
    result = bind_execution_scope_params(operation, params, scope)
    assert result == {"max_tiles": 100, "location_id": "Farm", "target_tiles": [{"x": 66, "y": 23}]}
    assert bind_execution_scope_params(operation, result, scope) == result
    assert params == old_params and scope == old_scope
    assert bind_execution_scope_params(operation, {"target_tiles": []}, scope)["target_tiles"] == []
    assert bind_execution_scope_params(operation, {}, scope)["target_tiles"] == scope["objectiveScope"]["executionScope"]["tiles"]
    assert bind_execution_scope_params(operation, params, {}) == params
    assert bind_execution_scope_params("plant_seeds", params, scope) == params


@pytest.mark.parametrize("operation", ["water_auto", "harvest_auto"])
def test_worker_dispatch_and_authorization_apply_identical_saved_scope(tmp_path, monkeypatch, operation):
    async def run():
        monkeypatch.delenv("STARDEW_MCP_SURFACE", raising=False)
        scheduler = MagicMock()
        scheduler.run_dir = str(tmp_path)
        scheduler.latest_snapshot = {"saveId": "scope-save", "payload": {}}
        scheduler.get_status = AsyncMock(return_value={"saveId": "scope-save"})
        native = AsyncMock(return_value={"terminalState": "succeeded", "completedCount": 2,
                                        "skippedCount": 0, "failedCount": 0, "effects": []})
        setattr(scheduler, operation, native)
        scope = {"objectiveScope": {"executionScope": {"locationId": "Farm",
            "tiles": [{"x": 62, "y": 27}, {"x": 66, "y": 23}]}}}
        store = WorkStore(tmp_path / "data/work-state.json")
        goal_id = store.add_goal("scope-save", "selected area", source="user", constraints=scope).id
        store.begin_decision("scope-save", "selected")
        store.submit_plan("scope-save", goal_id=goal_id, decision_token="selected", tasks=[{
            "id": "t", "title": "auto within saved area", "steps": [{"id": "s", "operation": operation,
                                                                        "params": {"max_tiles": 100}}]}])
        assert store.claim_next_step("scope-save", "worker")
        store.assign_command_id("scope-save", "t", "s", "native-id")
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="internal")
        effective = bind_execution_scope_params(operation, {"max_tiles": 100}, scope)
        await server.call_tool("dispatch_plan_operation", {
            "operation": operation, "params": effective, "command_id": "native-id",
        })
        assert native.await_args.kwargs == effective
        with pytest.raises(ToolError, match="UNAUTHORIZED_JOB_COMMAND"):
            await server.call_tool("dispatch_plan_operation", {
                "operation": operation, "params": {**effective, "max_tiles": 101}, "command_id": "native-id",
            })
        assert native.await_count == 1
        tools = {tool.name: tool for tool in await server.list_tools()}
        schema = tools[operation].inputSchema["properties"]
        assert {"target_tiles", "location_id"} <= set(schema)
        _, capabilities = await server.call_tool("discover_capabilities", {"group": "farm"})
        entry = next(row for row in capabilities["groups"]["farm"] if row["name"] == operation)
        assert set(entry["planStep"]["parameters"]) == _PLAN_OPERATION_CALLS[operation][1]
        shape = entry["planStep"]["parameters"]["target_tiles"]
        assert shape["anyOf"][0]["minItems"] == 0
        assert "[] selects no work" in shape["description"]

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["water_auto", "harvest_auto"])
@pytest.mark.parametrize("targets", [None, []])
@pytest.mark.parametrize("entry_point", ["direct", "submit_plan"])
def test_auto_new_optional_fields_omit_none_but_preserve_empty_scope(
    tmp_path, monkeypatch, operation, targets, entry_point,
):
    async def run():
        monkeypatch.delenv("STARDEW_MCP_SURFACE", raising=False)
        monkeypatch.setenv("STARDEW_DECISION_TOKEN", "selection")
        scheduler = MagicMock()
        scheduler.run_dir = str(tmp_path)
        scheduler.latest_snapshot = {"saveId": "scope-save", "payload": {}}
        scheduler.get_status = AsyncMock(return_value={"saveId": "scope-save"})
        native = AsyncMock()
        setattr(scheduler, operation, native)
        store = WorkStore(tmp_path / "data/work-state.json")
        store.begin_decision("scope-save", "selection")
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="full")
        params = {"max_tiles": 100, "target_tiles": targets, "location_id": None}
        if entry_point == "direct":
            await server.call_tool(operation, params)
        else:
            await server.call_tool("submit_plan", {"goal_text": "selected area", "tasks": [{
                "title": "scope", "steps": [{"operation": operation, "params": params}]}]})
        saved = store.state("scope-save").tasks[0].steps[0].params
        assert "location_id" not in saved
        if targets is None:
            assert "target_tiles" not in saved
        else:
            assert saved["target_tiles"] == []
        native.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["plant_seeds", "water_auto", "harvest_auto"])
@pytest.mark.parametrize("hard_scope", [False, True])
def test_atomic_tool_reuses_bound_user_goal_and_keeps_its_execution_scope(
    tmp_path, monkeypatch, operation, hard_scope,
):
    async def run():
        monkeypatch.delenv("STARDEW_MCP_SURFACE", raising=False)
        monkeypatch.setenv("STARDEW_DECISION_TOKEN", "selection")
        scheduler = MagicMock()
        scheduler.run_dir = str(tmp_path)
        scheduler.latest_snapshot = {"saveId": "scope-save", "payload": {}}
        scheduler.get_status = AsyncMock(return_value={"saveId": "scope-save"})
        native = AsyncMock(return_value={"terminalState": "succeeded", "completedCount": 2,
                                        "skippedCount": 0, "failedCount": 0, "effects": []})
        setattr(scheduler, _PLAN_OPERATION_CALLS[operation][0], native)
        tiles = [{"x": 62, "y": 27}, {"x": 66, "y": 23}]
        constraints = {"objectiveScope": {"executionScope": {"locationId": "Farm", "tiles": tiles}}}
        store = WorkStore(tmp_path / "data/work-state.json")
        goal_id = store.add_goal("scope-save", "accepted planting and watering", source="user",
                                 constraints=constraints).id
        store.begin_decision("scope-save", "selection", goal_scope=goal_id if hard_scope else None)
        assert store.state("scope-save").decision["goalId"] == goal_id
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="full")
        params = ({"seed_item_id": "(O)472", "tiles": tiles} if operation == "plant_seeds"
                  else {"max_tiles": 100})
        _, selected = await server.call_tool(operation, params)
        state = store.state("scope-save")
        task = state.tasks[0]
        assert selected["goalId"] == task.goal_id == goal_id
        assert len(state.goals) == 1 and state.goals[0].source == "user"
        assert state.goals[0].constraints == constraints
        native.assert_not_awaited()
        assert store.claim_next_step("scope-save", "worker")
        store.assign_command_id("scope-save", task.id, task.steps[0].id, "native-id")
        worker_server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="internal")
        dispatch_params = {key: value for key, value in task.steps[0].params.items()
                           if key in _PLAN_OPERATION_CALLS[operation][1]}
        await worker_server.call_tool("dispatch_plan_operation", {
            "operation": operation, "params": dispatch_params, "command_id": "native-id"})
        assert native.await_count == 1
        if operation in {"water_auto", "harvest_auto"}:
            assert native.await_args.kwargs["target_tiles"] == tiles
            assert native.await_args.kwargs["location_id"] == "Farm"

    asyncio.run(run())
