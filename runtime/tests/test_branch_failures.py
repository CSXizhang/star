"""A native blocker survives new task IDs, without stopping other work."""
import asyncio

import pytest

from stardew_ai_runtime.chat_bridge import PlanWorker
from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def select(store, token, operation="navigate_to", **params):
    store.begin_decision("S", token)
    return store.submit_plan("S", goal_text="照料加工", decision_token=token,
        tasks=[{"title": token, "steps": [{"operation": operation,
            "params": params or {"location_id": "Farm", "tile": {"x": 12, "y": 8}}}]}])


def test_native_failure_budget_persists_and_leaves_other_branch_usable(tmp_path):
    path = tmp_path / "work.json"
    store = WorkStore(path)
    class Client:
        async def current_save_id(self):
            return "S"
        async def call_tool(self, name, args):
            return {"status": "failed", "terminalState": "failed", "reasonCode": "PATH_BLOCKED"}
    for n in range(3):
        select(store, str(n), timeout_seconds=60 + n,
               location_id="Farm", tile={"x": 12, "y": 8})
        asyncio.run(PlanWorker(store, Client()).evaluate())
        store = WorkStore(path)  # budget survives process/session/task replacement
    assert store.overview("S")["blockedBranches"][0]["failures"] == 3
    with pytest.raises(WorkStateError, match="BRANCH_BLOCKED"):
        select(store, "retry")
    select(store, "food", "eat_food", item_id="(O)403", count=1)
    assert not store.state("S").paused
    store.revoke_decision("S")
    store.observe_branch_recovery("S", operation="navigate_to",
        target={"location_id": "Farm", "tile": {"x": 12, "y": 8}},
        evidence={"reachable": True})
    select(store, "repaired")


def test_unknown_result_is_not_a_failed_attempt(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    select(store, "unknown")
    class Client:
        async def current_save_id(self):
            return "S"
        async def call_tool(self, name, args):
            return {"outcome": "unknown", "terminalState": "unknown"}
    asyncio.run(PlanWorker(store, Client()).evaluate())
    assert not store.state("S").branch_failures


def test_blocked_feed_does_not_block_another_house(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    class Client:
        async def current_save_id(self):
            return "S"
        async def call_tool(self, name, args):
            return {"terminalState": "rejected", "reasonCode": "no-hay"}
    for n in range(3):
        select(store, str(n), "feed_animals", building_name="CoopA")
        asyncio.run(PlanWorker(store, Client()).evaluate())
    with pytest.raises(WorkStateError, match="BRANCH_BLOCKED"):
        select(store, "blocked", "feed_animals", building_name="CoopA")
    select(store, "other-house", "feed_animals", building_name="CoopB")


def _commit_failure(store, token, command, reason):
    select(store, token)
    claim = store.claim_next_step("S", "worker")
    store.assign_command_id("S", task_id=claim["taskId"], step_id=claim["stepId"],
                            command_id=command)
    store.commit_step_result("S", task_id=claim["taskId"], step_id=claim["stepId"],
                             outcome="failed", command_id=command, reason_code=reason)
    store.revoke_decision("S")


def test_same_command_changing_reason_is_not_counted_again_after_restart(tmp_path):
    path = tmp_path / "work.json"
    store = WorkStore(path)
    _commit_failure(store, "first", "same-command", "PATH_BLOCKED")
    store = WorkStore(path)
    _commit_failure(store, "reconciled", "same-command", "ROUTE_UNREACHABLE")
    branches = store.state("S").branch_failures
    assert len(branches) == 1
    assert branches[0]["failures"] == 1
    _commit_failure(store, "second", "second-command", "PATH_BLOCKED")
    _commit_failure(store, "third", "third-command", "PATH_BLOCKED")
    assert store.overview("S")["blockedBranches"][0]["failures"] == 3


def test_legacy_block_without_recovery_condition_remains_blocked(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    for n in range(3):
        _commit_failure(store, str(n), f"cmd-{n}", "PATH_BLOCKED")
    store._mutate("S", lambda state: state.branch_failures[0].pop("recoveryCondition"))
    with pytest.raises(WorkStateError, match="BRANCH_BLOCKED"):
        select(WorkStore(store.state_path), "legacy-retry")


def test_route_recovery_requires_matching_target_and_real_route_evidence(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    for n in range(3):
        _commit_failure(store, str(n), f"cmd-{n}", "PATH_BLOCKED")
    target = {"location_id": "Farm", "tile": {"x": 12, "y": 8}}
    for save, destination, evidence in [
        ("Other", target, {"reachable": True}),
        ("S", {"location_id": "Farm", "tile": {"x": 13, "y": 8}}, {"reachable": True}),
        ("S", target, {"reachable": None}),
    ]:
        store.observe_branch_recovery(save, operation="navigate_to", target=destination, evidence=evidence)
        assert store.state("S").branch_failures[0]["blocked"]
    store.observe_branch_recovery("S", operation="navigate_to", target=target, evidence={"reachable": True})
    assert not store.state("S").branch_failures[0]["blocked"]


def test_ready_machine_does_not_release_unknown_native_outcome(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    target = {"location_id": "Farm", "tile": {"x": 1, "y": 2}}
    store._mutate("S", lambda state: state.branch_failures.extend([
        {"operation": "collect_machine", "target": target, "reasonCode": reason,
         "blocked": True, "failures": 3}
        for reason in ["NOT_READY", "UNKNOWN_OUTPUT_STATE"]
    ]))
    store.observe_machine_recovery("S", "Farm", [{"tile": target["tile"], "isReady": True}])
    known, unknown = store.state("S").branch_failures
    assert known["blocked"] is False
    assert unknown["blocked"] is True
