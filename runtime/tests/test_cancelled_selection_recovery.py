"""A revised player arrangement must neither strand work nor replay native actions."""

import json

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def arrangement(store, text):
    return store.sync_milestone_work("S", "patch", text=text,
        constraints={"objectiveScope": {"summary": text}}, active=True,
        todos=[{"key": "harvest", "intent": "harvest：" + text,
                "trigger": {"type": "calendar", "year": 1, "season": "fall", "day": 20}}])[0]


def select(store, goal, token="first", task="old"):
    store.begin_decision("S", token)
    return store.submit_plan("S", goal_id=goal, decision_token=token, tasks=[{
        "id": task, "title": "harvest this patch", "steps": [
            {"id": "harvest", "operation": "harvest_auto"},
            {"id": "later", "operation": "navigate_to", "params": {"location_id": "FarmHouse", "tile": {"x": 3, "y": 4}}},
        ]}])


def test_revised_undispatched_arrangement_releases_selection_without_completion(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = arrangement(store, "harvest only this patch")
    select(store, goal)
    arrangement(store, "harvest only this patch")
    assert store.has_ready_step("S")
    arrangement(store, "harvest only this patch then stop")
    recovered = WorkStore(store.state_path)
    state = recovered.state("S")
    assert state.decision["finished"]
    assert state.tasks[0].status == "cancelled"
    assert all(s.attempts == 0 and s.command_id is None for s in state.tasks[0].steps)
    assert not state.executions and not state.last_job and not state.outbox
    select(recovered, goal, "replacement", "new")
    assert recovered.has_ready_step("S")


def test_recovery_repairs_existing_stale_selection_without_replaying_it(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = arrangement(store, "old")
    select(store, goal)
    arrangement(store, "new")
    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    raw["S"]["decision"].pop("finished")
    store.state_path.write_text(json.dumps(raw), encoding="utf-8")
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    bridge._work_store = WorkStore(store.state_path)
    assert not bridge._job_in_flight("S")
    bridge._work_store.recover("S")
    assert bridge._work_store.state("S").decision["finished"]
    assert not bridge._work_store.has_ready_step("S")
    select(bridge._work_store, goal, "replacement", "new")


def test_revised_dispatched_work_waits_for_native_terminal_and_late_receipt_is_safe(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    store = bridge._work_store
    goal = arrangement(store, "old")
    select(store, goal)
    store.claim_next_step("S", "worker")
    store.assign_command_id("S", "old", "harvest", "native-1")
    arrangement(store, "new")
    assert bridge._job_in_flight("S")
    store.recover("S")
    assert not store.state("S").decision.get("finished")
    with pytest.raises(WorkStateError, match="GAME_BUSY"):
        select(store, goal, "replacement", "new")
    receipt = dict(task_id="old", step_id="harvest", command_id="native-1",
        outcome="partial", reason_code="CANCELLED", effects=[{"state": "harvested", "stack": 1}])
    store.commit_step_result("S", **receipt)
    assert store.state("S").decision["finished"]
    assert not bridge._job_in_flight("S")
    select(store, goal, "replacement", "new")
    store.commit_step_result("S", **receipt)
    state = store.state("S")
    assert state.decision["taskId"] == "new" and not state.decision.get("finished")
    assert len(state.executions) == 1
    assert state.tasks[0].steps[1].status == "cancelled"
