"""Saved candidate scopes bound auto selection while preserving step ownership."""

import asyncio
import os
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.chat_bridge import ChatBridge, PlanWorker
from stardew_ai_runtime.mcp_server import _PLAN_OPERATION_CALLS, create_mcp_server
from stardew_ai_runtime.plan_executor import PlanExecutor
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.work_state import WorkStore, bind_execution_scope_params


@pytest.mark.parametrize("explicit_first_goal", [False, True])
def test_accepted_preparation_and_delivery_keep_current_goal_with_other_active_goals(
    tmp_path, monkeypatch, explicit_first_goal,
):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    monkeypatch.delenv("STARDEW_MCP_SURFACE", raising=False)
    monkeypatch.delenv("STARDEW_LIFE_MODE", raising=False)
    monkeypatch.delenv("STARDEW_DECISION_TOKEN", raising=False)

    async def run():
        scheduler = MagicMock()
        scheduler.run_dir = str(tmp_path)
        scheduler.latest_snapshot = {"saveId": "S", "payload": {}}
        scheduler.get_status = AsyncMock(return_value={"saveId": "S"})
        scheduler.water_auto = AsyncMock(return_value={"terminalState": "succeeded", "effects": []})
        model_server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="full")
        worker_server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="internal")

        class Client:
            async def current_save_id(self):
                return "S"

            async def close(self):
                pass

            async def call_tool(self, name, params):
                _, result = await worker_server.call_tool(name, params)
                return result

        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=Client())
        bridge._latest_snapshot_payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 11}}
        other = bridge._work_store.add_goal("S", "另一个持续目标", source="user")
        bridge._autonomy.set_enabled("S", False, goal_scope=other.id)
        tiles = [{"x": 62, "y": 27}, {"x": 66, "y": 23}]
        before = bridge._accepted_preparation_state("S")
        node = bridge._milestone_store.propose("S", title="照料指定两格", target_date="1:spring:11",
            preparation=["water"], execution_scope={"locationId": "Farm", "tiles": tiles})
        adopted = bridge._milestone_store.adopt("S", node["id"], work_store=bridge._work_store,
                                             game_date={"year": 1, "season": "spring", "day": 11})
        goal_id = adopted["goalId"]
        selected_goals, requests = [], []

        class Provider:
            def run(self, task, cid, prompt):
                requests.append(task.request_id)
                if len(requests) <= 2:
                    if explicit_first_goal and len(requests) == 1:
                        _, result = asyncio.run(model_server.call_tool("submit_plan", {
                            "goal_id": goal_id, "tasks": [{"title": "照料", "steps": [{
                                "operation": "water_auto", "params": {"max_tiles": 100}}]}]}))
                    else:
                        _, result = asyncio.run(model_server.call_tool("water_auto", {"max_tiles": 100}))
                    selected_goals.append(result["goalId"])
                return {"success": True, "response": "按当前安排处理。"}

        bridge._backend = Provider()
        ws = AsyncMock()
        bridge._chat_ws = ws  # the outbox delivery turn replies on the chat channel
        await bridge._start_accepted_preparation(ws, "S", before)
        assert not bridge._autonomy.state("S").enabled
        # The continuation must retain its origin even if the global project changes.
        bridge._autonomy.set_goal_scope("S", other.id)
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_deliveries()
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_deliveries()
        assert requests[0].startswith("preparation-") and requests[1].startswith("delivery-")
        # Unqualified continuation retains the accepted goal and its native
        # execution scope even when another project becomes globally active.
        assert selected_goals == [goal_id, goal_id]
        assert len(bridge._work_store.state("S").goals) == 2
        assert scheduler.water_auto.await_count == 2
        assert "STARDEW_DECISION_TOKEN" not in os.environ
        # A new player request may select an unrelated goal despite the global scope.
        decisions = []

        class NewInstruction:
            def run(self, task, cid, prompt):
                decisions.append(bridge._work_store.state("S").decision)
                return {"success": True, "response": "接下新指令。"}

        bridge._backend = NewInstruction()
        await bridge.handle_chat_submit(ws, "new-player-request", "处理另一件事", "S")
        assert "goalScope" not in decisions[0] and "goalId" not in decisions[0]

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["water_auto", "harvest_auto"])
@pytest.mark.parametrize("entry_point", ["worker", "executor"])
@pytest.mark.parametrize("covered_count", [1, 2])
def test_recovery_without_saved_observation_requires_receipts_for_explicit_scope(
    tmp_path, operation, entry_point, covered_count,
):
    async def run():
        store = WorkStore(tmp_path / "work.json")
        tiles = [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
        goal = store.add_goal("farm", "约定两格", constraints={
            "objectiveScope": {"executionScope": {"locationId": "Farm", "tiles": tiles}}})
        store.begin_decision("farm", "decision", goal_scope=goal.id)
        store.submit_plan("farm", goal_id=goal.id, decision_token="decision", tasks=[{
            "title": "照料", "steps": [{"operation": operation, "params": {}},
                                        {"operation": "refill_watering_can", "params": {}}],
        }])
        claim = store.claim_next_step("farm", "old-worker", lease_seconds=0)
        store.assign_command_id("farm", claim["taskId"], claim["stepId"], "old-command")
        receipt = {"commandId": "old-command", "terminalState": "succeeded",
                   "completedCount": covered_count, "effects": [{"tile": tile,
                       "state": "watered" if operation == "water_auto" else "harvested"}
                       for tile in tiles[:covered_count]]}

        class Client:
            async def current_save_id(self):
                return "farm"

            async def reconcile(self, command_id):
                assert command_id == "old-command"
                return receipt

            async def call_tool(self, *args):
                raise AssertionError("Recovery must only reconcile the existing command")

        if entry_point == "worker":
            await PlanWorker(WorkStore(store.state_path), Client()).evaluate()
        else:
            store.mark_task_waiting("farm", claim["taskId"], step_id=claim["stepId"],
                condition={"type": "transportRetry", "params": {"backoffSeconds": 0}, "attemptsAllowed": 3})
            await PlanExecutor(WorkStore(store.state_path), Client().call_tool,
                               reconcile=Client().reconcile).run_once("farm", "new-worker")
        state = store.state("farm")
        if covered_count == 1:
            assert state.tasks[0].steps[0].status == "unknown"
            assert state.tasks[0].steps[0].reason_code == "SCOPE_OBSERVATION_UNCONFIRMED"
            assert not store.has_ready_step("farm")
        else:
            assert state.tasks[0].steps[0].status == "completed"
            assert store.has_ready_step("farm")
        assert len(state.executions) == 1
        assert state.tasks[0].steps[1].attempts == 0

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["water_auto", "harvest_auto"])
@pytest.mark.parametrize("delivery", ["synchronous", "delayed", "recovery", "retry"])
@pytest.mark.parametrize("incomplete", [False, True])
def test_auto_scope_completion_matches_synchronous_late_and_recovered_receipts(
    tmp_path, operation, delivery, incomplete,
):
    async def run():
        store = WorkStore(tmp_path / "work.json")
        tiles = [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
        goal = store.add_goal("farm", "照料约定范围", source="user", constraints={
            "objectiveScope": {"executionScope": {"locationId": "Farm", "tiles": tiles}}})
        store.begin_decision("farm", "decision", goal_scope=goal.id)
        store.submit_plan("farm", goal_id=goal.id, decision_token="decision", tasks=[{
            "title": "照料后离开", "steps": [
                {"operation": operation, "params": {"max_tiles": 100}},
                {"operation": "navigate_to", "params": {"location_id": "Farm", "tile": {"x": 5, "y": 6}}},
            ],
        }])
        scheduler = CompanionScheduler()
        published = tiles[:1] if incomplete else tiles
        scheduler.query_farm_work = AsyncMock(return_value={"companion": {"locationId": "Farm"}, "farmWork": {
            "locationId": "Farm", "cropUnwateredTiles": published, "cropUnwateredCount": 2,
            "cropUnwateredTruncated": incomplete, "matureCrops": published, "matureCropCount": 2,
            "matureCropsTruncated": incomplete,
        }})
        effects = [{"state": "watered" if operation == "water_auto" else "harvested", "tile": tile}
                   for tile in published]
        calls, receipt = [], {}

        async def native_dispatch(**kwargs):
            command_id = kwargs.get("command_id") or kwargs["requested_command_id"]
            receipt.update(commandId=command_id, terminalState="succeeded", completedCount=len(published),
                           skippedCount=0, failedCount=0, finalWorldRevision=42, effects=effects)
            if delivery == "synchronous":
                return dict(receipt)
            return {"commandId": command_id, "terminalState": "running", "status": "running",
                    "completedCount": 0, "skippedCount": 0, "failedCount": 0, "effects": []}

        scheduler.execute_tiles = AsyncMock(side_effect=native_dispatch)
        scheduler.execute_skill = AsyncMock(side_effect=native_dispatch)

        async def dispatch(op, params, command_id):
            calls.append(op)
            if op == operation:
                return await getattr(scheduler, op)(**params, command_id=command_id)
            return {"commandId": command_id, "terminalState": "succeeded"}

        async def reconcile(command_id):
            assert command_id == receipt["commandId"]
            if delivery in {"recovery", "retry"}:
                raise asyncio.CancelledError
            return dict(receipt)

        executor = PlanExecutor(store, dispatch, reconcile=reconcile, lease_seconds=0)
        if delivery in {"recovery", "retry"}:
            with pytest.raises(asyncio.CancelledError):
                await executor.run_once("farm", "worker")

            class Client:
                async def current_save_id(self):
                    return "farm"

                async def reconcile(self, command_id):
                    assert command_id == receipt["commandId"]
                    return dict(receipt)

                async def call_tool(self, name, args):
                    return await dispatch(args["operation"], args["params"], args["command_id"])

            if delivery == "retry":
                store.mark_task_waiting("farm", store.state("farm").tasks[0].id,
                    step_id=store.state("farm").tasks[0].steps[0].id,
                    condition={"type": "transportRetry", "params": {"backoffSeconds": 0}, "attemptsAllowed": 3})
                executor = PlanExecutor(WorkStore(store.state_path), dispatch,
                                        reconcile=Client().reconcile)
                await executor.run_once("farm", "retry-worker")
            else:
                worker = PlanWorker(WorkStore(store.state_path), Client())
                await worker.evaluate()
            restored = store.state("farm")
            step = restored.tasks[0].steps[0]
            outcome, reason, feedback = step.outcome, step.reason_code, step.feedback
        else:
            result = await executor.run_once("farm", "worker")
            outcome, reason = result.outcome, result.reason_code
            feedback = store.state("farm").tasks[0].steps[0].feedback
        assert outcome == ("partial" if incomplete else "completed")
        assert reason == ("OBSERVATION_TRUNCATED" if incomplete else "OK")
        assert feedback["scopeObservationIncomplete"] is incomplete
        assert feedback["goalSatisfied"] is (not incomplete)
        assert feedback["completedCount"] == len(published)
        assert store.state("farm").tasks[0].steps[0].effects == effects
        if incomplete:
            assert feedback["remaining"]["unwateredTiles" if operation == "water_auto" else "matureCrops"] is None
            assert (await executor.run_once("farm", "worker")).status == "idle"
            assert calls == [operation]
        else:
            assert (await executor.run_once("farm", "worker")).outcome == "completed"
            assert calls == [operation, "navigate_to"]
        assert (await executor.run_once("farm", "worker")).status == "idle"

    asyncio.run(run())


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
