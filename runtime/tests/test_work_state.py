"""Tests for per-save persistent work state (jobs, short plans, todos, log)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def _store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path / "data" / "work-state.json")


def _goal(store: WorkStore, save: str = "Save1") -> str:
    return store.add_goal(save, "照料农场", source="user").id


def test_goal_and_plan_persist_across_instances(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal_id = _goal(store)
    tasks = store.create_plan(
        "Save1",
        goal_id,
        [
            {
                "id": "t-water",
                "title": "浇水",
                "completionCondition": "作物湿润",
                "steps": [{"id": "s1", "operation": "water_auto", "params": {"max_tiles": 10}}],
            }
        ],
    )
    assert tasks[0].id == "t-water"

    reloaded = _store(tmp_path)
    goals = reloaded.list_goals("Save1")
    assert goals[0]["id"] == goal_id
    tasks_after = reloaded.list_tasks("Save1")
    assert tasks_after[0]["steps"][0]["operation"] == "water_auto"


def test_goal_source_must_be_user_or_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(WorkStateError):
        store.add_goal("Save1", "x", source="system")
    agent_goal = store.add_goal("Save1", "propose crop rotation", source="agent")
    assert agent_goal.source == "agent"


def test_plan_rejects_dependency_cycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal_id = _goal(store)
    with pytest.raises(WorkStateError, match="cycle"):
        store.create_plan(
            "Save1",
            goal_id,
            [
                {"id": "a", "title": "A", "dependencies": ["b"], "steps": [{"operation": "get_status"}]},
                {"id": "b", "title": "B", "dependencies": ["a"], "steps": [{"operation": "get_status"}]},
            ],
        )


def test_plan_rejects_disallowed_operation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal_id = _goal(store)
    with pytest.raises(WorkStateError, match="not an allowed plan operation"):
        store.create_plan(
            "Save1",
            goal_id,
            [{"id": "a", "title": "A", "steps": [{"operation": "run_shell", "params": {"cmd": "rm"}}]}],
        )


def test_claim_respects_dependencies_and_prevents_double_claim(tmp_path: Path) -> None:
    store=_store(tmp_path)
    _chosen(store)
    first=store.claim_next_step("Save1","w",now=1,lease_seconds=30)
    assert first is not None
    assert store.claim_next_step("Save1","other",now=2) is None
    store.revoke_decision("Save1")
    assert store.claim_next_step("Save1","other",now=50) is None


def test_cannot_mark_unexecuted_step_completed(tmp_path: Path) -> None:
    store=_store(tmp_path)
    _chosen(store)
    store.claim_next_step("Save1","w")
    with pytest.raises(WorkStateError,match="executed command id"):
        store.commit_step_result("Save1",task_id="a",step_id="sa",outcome="completed")


def test_cancel_goal_cascades_tasks_and_todos(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal_id = _goal(store)
    store.create_plan(
        "Save1", goal_id, [{"id": "a", "title": "A", "steps": [{"id": "sa", "operation": "water_auto"}]}]
    )
    store.add_todo("Save1", intent="明天浇水", trigger={"type": "calendar", "year": 1, "season": "spring", "day": 2}, goal_id=goal_id)
    before = store.epoch("Save1")

    store.cancel_goal("Save1", goal_id)

    assert store.list_goals("Save1")[0]["status"] == "cancelled"
    assert store.list_tasks("Save1")[0]["status"] == "cancelled"
    assert store.list_todos("Save1")[0]["status"] == "cancelled"
    assert store.epoch("Save1") != before
    assert store.claim_next_step("Save1", "worker-1") is None


def test_recover_unconfirmed_step_keeps_unknown_and_asks_once(tmp_path: Path) -> None:
    store=_store(tmp_path)
    _chosen(store)
    store.claim_next_step("Save1","w",now=1,lease_seconds=1)
    assert len(store.recover("Save1",now=3))==1
    assert store.recover("Save1",now=4)==[]
    assert store.list_tasks("Save1")[0]["steps"][0]["status"]=="unknown"


def test_recover_applies_logged_outcome(tmp_path: Path) -> None:
    state_file = tmp_path / "data" / "work-state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({
            "Save1": {
                "goals": [{"id": "g", "text": "g", "source": "user", "status": "active"}],
                "tasks": [{
                    "id": "a",
                    "goal_id": "g",
                    "title": "A",
                    "status": "running",
                    "steps": [{
                        "id": "sa",
                        "operation": "water_auto",
                        "status": "running",
                        "command_id": "cmd-1",
                        "lease_until": 0.0,
                    }],
                }],
                "executions": [{
                    "command_id": "cmd-1",
                    "task_id": "a",
                    "step_id": "sa",
                    "operation": "water_auto",
                    "outcome": "completed",
                }],
            }
        }),
        encoding="utf-8",
    )
    store = WorkStore(state_file)
    assert store.recover("Save1", now=100.0) == []
    assert store.list_tasks("Save1")[0]["steps"][0]["status"] == "completed"


def test_due_todos_use_native_snapshot_and_calendar(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add_todo("Save1", intent="今天浇水", trigger={"type": "calendar", "year": 1, "season": "spring", "day": 5})
    store.add_todo("Save1", intent="以后浇水", trigger={"type": "calendar", "year": 1, "season": "spring", "day": 20})
    store.add_todo("Save1", intent="种子够了", trigger={"type": "inventory", "itemId": "(O)472", "minCount": 3})
    store.add_todo(
        "Save1",
        intent="已过期",
        trigger={"type": "calendar", "year": 1, "season": "spring", "day": 1},
        expiry={"year": 1, "season": "spring", "day": 2},
    )

    snapshot = {"payload": {"inventory": {"slots": [{"itemId": "(O)472", "stack": 5}]}}}
    due = store.evaluate_todos(
        "Save1", snapshot=snapshot, game_date={"year": 1, "season": "spring", "day": 10}
    )
    intents = sorted(item["intent"] for item in due)
    assert intents == ["今天浇水", "种子够了"]


def test_pause_blocks_claim_and_multi_save_isolation(tmp_path: Path) -> None:
    store=_store(tmp_path)
    _chosen(store,"SaveA")
    _chosen(store,"SaveB")
    store.set_paused("SaveA",True)
    assert store.claim_next_step("SaveA","w") is None
    assert store.claim_next_step("SaveB","w") is not None
    store.set_paused("SaveA",False)
    assert store.claim_next_step("SaveA","w") is not None


def test_overview_exposes_next_step_and_anomalies(tmp_path: Path) -> None:
    store=_store(tmp_path)
    _chosen(store)
    assert store.overview("Save1")["hasExecutableWork"]
    store.revoke_decision("Save1")
    assert not store.overview("Save1")["hasExecutableWork"]
    assert store.overview("Save1")["tasks"]


def test_submit_plan_resolves_goal_and_supersedes_pending_only(tmp_path: Path) -> None:
    store=_store(tmp_path)
    first=_chosen(store,task_id="a")
    second=_chosen(store,task_id="b")
    assert first["goalId"]==second["goalId"]
    assert first["goalCreated"] and not second["goalCreated"]
    assert store.claim_next_step("Save1","w")["taskId"]=="b"


def test_submit_plan_replace_preserves_running_and_unknown_work(tmp_path: Path) -> None:
    store=_store(tmp_path)
    _chosen(store)
    store.claim_next_step("Save1","w")
    _chosen(store,task_id="b")
    assert store.list_tasks("Save1")[0]["status"]=="running"
    assert store.claim_next_step("Save1","w2")["taskId"]=="b"


def _force_partial(state, task_id: str, step_id: str) -> None:
    task = next(t for t in state.tasks if t.id == task_id)
    step = next(s for s in task.steps if s.id == step_id)
    step.status = "partial"
    step.outcome = "partial"
    step.command_id = "cmd-p"
    task.status = "partial"


def test_day_settlement_is_idempotent_and_never_replays(tmp_path: Path) -> None:
    store=_store(tmp_path)
    store.settle_game_day("Save1",year=1,season="spring",day=5)
    _chosen(store)
    assert store.settle_game_day("Save1",year=1,season="spring",day=6)["settled"]
    assert not store.has_ready_step("Save1")
    assert not store.settle_game_day("Save1",year=1,season="spring",day=6)["settled"]
    assert not store.settle_game_day("Save1",year=1,season="spring",day=3)["settled"]


def test_waiting_requires_an_explicit_supported_condition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal_id = _goal(store)
    store.create_plan(
        "Save1", goal_id, [{"id": "a", "title": "A", "steps": [{"id": "sa", "operation": "plant_seeds"}]}]
    )
    store.claim_next_step("Save1", "w")

    # No condition at all: refuse unconditional retry-style parking.
    with pytest.raises(WorkStateError, match="explicit waiting condition"):
        store.mark_task_waiting("Save1", "a")
    # A condition the runtime cannot evaluate is refused too.
    with pytest.raises(WorkStateError, match="unsupported waiting condition"):
        store.mark_task_waiting("Save1", "a", condition={"type": "vibes"})

    parked = store.mark_task_waiting(
        "Save1",
        "a",
        condition={"type": "shopOpen", "params": {"shopId": "SeedShop"}},
        reason_code="SHOP_CLOSED",
    )
    assert parked["waitingStepIds"] == ["sa"]
    assert parked["waitDescription"].startswith("wait:shopOpen")
    assert store.wait_conditions("Save1")[0]["waitCondition"]["type"] == "shopOpen"


def test_unchanged_snapshot_never_reclaims_but_condition_transition_claims_once(tmp_path: Path) -> None:
    store=_store(tmp_path)
    goal=_goal(store)
    store.create_plan("Save1",goal,[{"title":"future water","steps":[{"operation":"water_auto","wait":{"type":"inventory","params":{"itemId":"388","minCount":1}}}]}])
    assert not store.has_ready_step("Save1")
    assert not store.has_ready_step("Save1",snapshot={"inventory":{"slots":[{"itemId":"388","stack":10}]}})
    _chosen(store)
    assert store.has_ready_step("Save1")


def test_game_day_wait_releases_on_the_named_day_not_before(tmp_path: Path) -> None:
    store=_store(tmp_path)
    goal=_goal(store)
    store.create_plan("Save1",goal,[{"title":"future","steps":[{"operation":"water_auto","wait":{"type":"gameDay","params":{"year":1,"season":"spring","day":6}}}]}])
    assert not store.has_ready_step("Save1",game_date={"year":1,"season":"spring","day":6})


def test_declared_step_wait_parks_at_submit_and_never_dispatches_first(tmp_path: Path) -> None:
    store=_store(tmp_path)
    store.begin_decision("Save1","choice")
    with pytest.raises(WorkStateError,match="FUTURE_WAIT"):
        store.submit_plan("Save1",goal_text="future",decision_token="choice",tasks=[{"title":"future","steps":[{"operation":"water_auto","wait":{"type":"gameDay","params":{"day":6}}}]}])


def test_dependent_task_waits_for_completed_dependency(tmp_path: Path) -> None:
    store=_store(tmp_path)
    goal=_goal(store)
    store.create_plan("Save1",goal,[{"id":"a","title":"A","steps":[{"operation":"water_auto"}]},{"id":"b","title":"B","dependencies":["a"],"steps":[{"operation":"harvest_auto"}]}])
    assert not store.has_ready_step("Save1")
    assert len(store.list_tasks("Save1"))==2



def test_native_farm_plan_operations_persist_and_match_real_dispatch(tmp_path: Path) -> None:
    from stardew_ai_runtime.mcp_server import _PLAN_OPERATION_CALLS
    from stardew_ai_runtime.work_state import ALLOWED_OPERATIONS, READ_ONLY_OPERATIONS
    # Both admission and dispatch must agree, including newly connected farm tools.
    assert ALLOWED_OPERATIONS == set(_PLAN_OPERATION_CALLS) | {"query_wiki"}
    store = _store(tmp_path)
    tasks = store.create_plan("Save1", _goal(store), [
        {"id":"milk", "title":"collect", "steps":[{"operation":"collect_animal_produce", "params":{"animal_name":"cow"}}]},
        {"id":"store", "title":"store", "dependencies":["milk"], "steps":[{"operation":"deposit_to_chest", "params":{"chest_x":1,"chest_y":2}}]},
        {"id":"refill", "title":"refill", "dependencies":["store"], "steps":[{"operation":"refill_watering_can"}]},
    ])
    assert len(tasks) == 3
    saved = _store(tmp_path).list_tasks("Save1")
    assert {t["id"] for t in saved} == {"milk", "store", "refill"}
    assert {"observe_livestock","observe_farming_helpers","observe_machines"} <= READ_ONLY_OPERATIONS
    assert not {"collect_animal_produce","refill_watering_can"} & READ_ONLY_OPERATIONS

def _chosen(store, save="Save1", task_id="a"):
    import uuid
    token=uuid.uuid4().hex
    store.begin_decision(save,token)
    return store.submit_plan(save,goal_text="照料农场",decision_token=token,tasks=[{"id":task_id,"title":"one job","steps":[{"id":"sa","operation":"water_auto","params":{}}]}])

