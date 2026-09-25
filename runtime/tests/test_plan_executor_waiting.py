"""Tests for the shared PlanExecutor's explicit waiting-condition discipline.

A step can only
be parked behind a condition the runtime can actually evaluate from a fresh
native snapshot, the bounded transport backoff cannot spin, and an unchanged
snapshot never re-claims a waiting step (so it costs zero executions and zero
model calls).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from stardew_ai_runtime.chat_bridge import PlanWorker
from stardew_ai_runtime.plan_executor import (
    PlanExecutor,
    StepExecution,
    extract_wait_condition,
)
from stardew_ai_runtime.work_state import WorkStore


def _store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path / "data" / "work-state.json")


def _plan(store: WorkStore, save: str = "Save1", operation: str = "plant_seeds") -> None:
    goal_id = store.add_goal(save, "照料农场", source="user").id
    store.begin_decision(save,"explicit-unit-decision")
    store.submit_plan(
        save, goal_id=goal_id, decision_token="explicit-unit-decision", tasks=
        [
            {
                "id": "a",
                "title": "播种",
                "steps": [{"id": "sa", "operation": operation, "params": {}}],
            }
        ],
    )


def _run(coro):
    return asyncio.run(coro)


def test_running_result_reconciles_same_native_command_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _plan(store)
    sent = []
    reconciled = []

    async def dispatch(operation, params, command_id):
        sent.append(command_id)
        return {"status": "executing", "terminalState": "running", "commandId": command_id,
                "effects": []}

    async def reconcile(command_id):
        reconciled.append(command_id)
        if len(reconciled) == 1:
            return None
        return {"terminalState": "succeeded", "commandId": command_id,
                "effects": [{"kind": "cleared"}]}

    result = _run(PlanExecutor(store, dispatch=dispatch, reconcile=reconcile).run_once("Save1", "w"))
    assert result.outcome == "completed"
    assert result.effects == [{"kind": "cleared"}]
    assert sent == [reconciled[0]] and len(store.execution_log("Save1")) == 1


def test_unconfirmed_previous_command_never_dispatches_new_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _plan(store)

    def old_attempt(state):
        task = state.tasks[0]
        task.steps[0].command_id = "old-native-id"

    store._mutate("Save1", old_attempt)
    sent = []

    async def dispatch(operation, params, command_id):
        sent.append(command_id)
        return {"status": "completed"}

    async def reconcile(command_id):
        return None

    result = _run(PlanExecutor(store, dispatch=dispatch, reconcile=reconcile).run_once("Save1", "w"))
    assert result.outcome == "unknown"
    assert result.reason_code == "NATIVE_TERMINAL_UNCONFIRMED"
    assert sent == []
    assert store.execution_log("Save1")[0]["command_id"] == "old-native-id"


def test_pause_between_claim_and_native_send_returns_step_for_resume(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _plan(store)

    class Client:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return {"status": "completed", "terminalState": "succeeded", "effects": []}

    async def scenario():
        client = Client()
        worker = PlanWorker(store, client)
        worker.supplied_save_id = "Save1"
        started, release = asyncio.Event(), asyncio.Event()

        async def progress(execution):
            if execution.status == "dispatching":
                started.set()
                await release.wait()

        worker.progress_callback = progress
        first = asyncio.create_task(worker.evaluate())
        await asyncio.wait_for(started.wait(), timeout=2)
        store.set_paused("Save1", True)
        release.set()
        await asyncio.wait_for(first, timeout=2)
        assert client.calls == []
        step = store.list_tasks("Save1")[0]["steps"][0]
        assert step["status"] == "pending" and step["command_id"] is None
        store.set_paused("Save1", False)
        await worker.evaluate()
        assert len(client.calls) == 1
        assert store.list_tasks("Save1")[0]["status"] == "completed"

    _run(scenario())


def test_extract_wait_condition_accepts_only_supported_native_markers() -> None:
    assert extract_wait_condition({"waitCondition": {"type": "shopOpen"}}) == {
        "type": "shopOpen",
        "params": {},
        "reasonCode": None,
    }
    # Nested in details is accepted too.
    assert extract_wait_condition(
        {"details": {"waitCondition": {"type": "inventory", "params": {"itemId": "x"}}}}
    ) == {"type": "inventory", "params": {"itemId": "x"}, "reasonCode": None}
    # Invented/unsupported markers are ignored: the step stays a normal deviation.
    assert extract_wait_condition({"waitCondition": {"type": "because-i-said-so"}}) is None
    assert extract_wait_condition("nope") is None


def test_native_wait_marker_parks_step_instead_of_reporting_deviation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _plan(store)

    async def dispatch(operation: str, params: dict[str, Any], command_id: str) -> Any:
        return {
            "status": "blocked",
            "waitCondition": {"type": "shopOpen", "params": {"shopId": "SeedShop"}},
            "reasonCode": "SHOP_CLOSED",
        }

    executor = PlanExecutor(store, dispatch=dispatch)
    execution = _run(executor.run_once("Save1", "w"))

    assert isinstance(execution, StepExecution)
    assert execution.outcome == "waiting"
    assert execution.reason_code == "SHOP_CLOSED"
    # Waiting is not a deviation: no model wake and no fabricated success.
    assert execution.needs_model is False
    task = store.list_tasks("Save1")[0]
    assert task["status"] == "waiting"
    step = task["steps"][0]
    assert step["status"] == "waiting"
    assert step["wait"]["type"] == "shopOpen"
    # No execution fact was written for a step that never ran.
    assert store.execution_log("Save1") == []


def test_snapshot_gating_blocks_repeat_executions_until_condition_changes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _plan(store)
    calls = {"n": 0}

    async def dispatch(operation: str, params: dict[str, Any], command_id: str) -> Any:
        calls["n"] += 1
        return {
            "status": "blocked",
            "waitCondition": {
                "type": "inventory",
                "params": {"itemId": "(O)472", "minCount": 3},
            },
            "reasonCode": "WAIT_FOR_SEEDS",
        }

    current = {
        "snapshot": {"payload": {"inventory": {"slots": [{"itemId": "(O)472", "stack": 1}]}}}
    }
    executor = PlanExecutor(
        store, dispatch=dispatch, snapshot_provider=lambda: (current["snapshot"], None)
    )

    first = _run(executor.run_once("Save1", "w"))
    assert first.outcome == "waiting"
    assert calls["n"] == 1

    # Repeated identical snapshots: zero further claims, so zero executions.
    for _ in range(4):
        execution = _run(executor.run_once("Save1", "w"))
        assert execution.status == "idle"
    assert calls["n"] == 1
    assert executor.has_ready_work("Save1") is False

    # The named condition transition releases exactly one execution.
    current["snapshot"] = {"payload": {"inventory": {"slots": [{"itemId": "(O)472", "stack": 3}]}}}
    assert executor.has_ready_work("Save1") is True
    second = _run(executor.run_once("Save1", "w"))
    assert second.status == "executed"
    assert calls["n"] == 2


def test_transport_failure_never_replays_an_unconfirmed_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _plan(store, operation="water_auto")
    calls = {"n": 0}

    async def dispatch(operation: str, params: dict[str, Any], command_id: str) -> Any:
        calls["n"] += 1
        raise ConnectionError("transport down")

    executor = PlanExecutor(
        store, dispatch=dispatch, snapshot_provider=lambda: (None, None), max_transport_retries=3
    )

    # Attempt 1: parked behind a bounded retry backoff, never an immediate retry.
    first = _run(executor.run_once("Save1", "w"))
    assert first.outcome == "waiting"
    assert first.reason_code == "TRANSPORT_RETRY_PENDING"
    assert _run(executor.run_once("Save1", "w")).status == "idle"
    assert calls["n"] == 1

    # After the backoff window, the old id cannot be confirmed.  A new native
    # id would risk replaying a command that may have reached the Mod.
    store._mutate("Save1", lambda state: _clear_backoff(state, "a", "sa"))
    second = _run(executor.run_once("Save1", "w"))
    assert second.status == "executed"
    assert second.outcome == "unknown"
    assert second.reason_code == "NATIVE_TERMINAL_UNCONFIRMED"
    assert calls["n"] == 1
    task = store.list_tasks("Save1")[0]
    assert task["status"] == "unknown"


def _clear_backoff(state, task_id: str, step_id: str) -> None:
    import time

    task = next(t for t in state.tasks if t.id == task_id)
    step = next(s for s in task.steps if s.id == step_id)
    if step.wait is not None:
        # Pretend the bounded backoff window has already elapsed: the window is
        # measured from created_at + backoffSeconds.
        step.wait.retry_after = time.time() - 1.0
        step.wait.created_at = "2000-01-01T00:00:00+00:00"
