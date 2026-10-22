"""A waiting navigation step must not dispatch its later farm action."""

import asyncio
import time

from stardew_ai_runtime.plan_executor import PlanExecutor
from stardew_ai_runtime.work_state import WorkStore


def _ordered_plan(tmp_path):
    store = WorkStore(tmp_path / "work-state.json")
    goal = store.add_goal("save", "回农场再收获一株", source="user")
    store.begin_decision("save", "turn", goal_scope=goal.id)
    store.submit_plan("save", goal_id=goal.id, decision_token="turn", tasks=[{
        "id": "job", "title": "返回后收获", "steps": [
            {"id": "walk", "operation": "navigate_to",
             "params": {"location_id": "Farm", "tile": {"x": 60, "y": 25}}},
            {"id": "harvest", "operation": "harvest_auto",
             "params": {"location_id": "Farm", "max_tiles": 1,
                        "target_tiles": [{"x": 61, "y": 20}]}},
        ]}])
    return store


def test_later_action_waits_for_timed_out_navigation_receipt(tmp_path):
    store = _ordered_plan(tmp_path)
    sent = []
    reconciled = []

    async def dispatch(operation, params, command_id):
        sent.append((operation, command_id))
        if operation == "navigate_to":
            # Native walking has begun, but its RPC result has not arrived.
            raise TimeoutError("IPC_FAILURE awaiting native navigation")
        return {"terminalState": "succeeded", "commandId": command_id,
                "effects": [{"state": "harvested", "tile": {"x": 61, "y": 20}, "stack": 1}]}

    async def reconcile(command_id):
        reconciled.append(command_id)
        return {"terminalState": "succeeded", "commandId": command_id,
                "effects": [{"location": "Farm", "pathLength": 4}]}

    executor = PlanExecutor(store, dispatch, reconcile=reconcile)
    first = asyncio.run(executor.run_once("save", "worker"))
    assert first.outcome == "waiting"
    assert asyncio.run(executor.run_once("save", "worker")).status == "idle"
    assert [operation for operation, _ in sent] == ["navigate_to"]
    assert store.state("save").tasks[0].steps[1].status == "pending"
    assert store.execution_log("save") == []

    def make_reconciliation_due(state):
        state.tasks[0].steps[0].wait.retry_after = time.time() - 1

    store._mutate("save", make_reconciliation_due)
    arrived = asyncio.run(executor.run_once("save", "worker"))
    assert arrived.outcome == "completed"
    assert reconciled == [sent[0][1]]
    assert len(sent) == 1  # Reconcile the original command, never restart it.
    harvested = asyncio.run(executor.run_once("save", "worker"))
    assert harvested.outcome == "completed"
    assert harvested.task_status == "completed"
    assert [operation for operation, _ in sent] == ["navigate_to", "harvest_auto"]


def test_later_action_waits_for_first_steps_native_precondition(tmp_path):
    store = _ordered_plan(tmp_path)
    calls = []
    opened = False

    async def dispatch(operation, params, command_id):
        calls.append(operation)
        if operation == "navigate_to" and not opened:
            return {"status": "blocked", "reasonCode": "SHOP_CLOSED",
                    "waitCondition": {"type": "shopOpen", "params": {"shopId": "SeedShop"}}}
        return {"terminalState": "succeeded", "commandId": command_id, "effects": []}

    executor = PlanExecutor(store, dispatch,
        snapshot_provider=lambda: ({"payload": {"shop": {"shopId": "SeedShop", "isOpen": opened}}}, None))
    assert asyncio.run(executor.run_once("save", "worker")).outcome == "waiting"
    for _ in range(3):
        assert asyncio.run(executor.run_once("save", "worker")).status == "idle"
    assert calls == ["navigate_to"]
    opened = True
    assert asyncio.run(executor.run_once("save", "worker")).operation == "navigate_to"
    assert asyncio.run(executor.run_once("save", "worker")).operation == "harvest_auto"
    assert calls == ["navigate_to", "navigate_to", "harvest_auto"]
