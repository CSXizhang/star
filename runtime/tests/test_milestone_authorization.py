"""Independent acceptance checks for milestone authorization and evidence."""

from stardew_ai_runtime.companion_milestones import CompanionMilestoneStore
from stardew_ai_runtime.work_state import WorkStore

SAVE = "SolAcceptanceSave"
NODE = "spring-egg-festival-strawberry:y1"
SPRING_11 = {"year": 1, "season": "spring", "day": 11}


def _stores(tmp_path):
    data = tmp_path / "data"
    return (CompanionMilestoneStore(data / "companion-milestones.json"),
            WorkStore(data / "work-state.json"))


def _active_todos(work):
    return [todo for todo in work.list_todos(SAVE)
            if todo["status"] in {"pending", "due"}]


def test_revise_replaces_authorized_quantity_and_budget(tmp_path):
    milestones, work = _stores(tmp_path)
    first = milestones.adopt(SAVE, NODE, planned_count=10, reserved_funds=1000,
                             work_store=work, game_date=SPRING_11)
    old_ids = set(first["todoIds"].values())

    revised = milestones.revise(SAVE, NODE, planned_count=5, reserved_funds=500,
                                work_store=work, game_date=SPRING_11)
    assert revised["status"] == "adopted"
    assert revised["plannedCount"] == 5
    assert revised["reservedFunds"] == 500
    goal = next(goal for goal in work.list_goals(SAVE)
                if goal["id"] == revised["goalId"])
    assert goal["status"] == "active"
    assert goal["constraints"]["plannedCount"] == 5
    assert goal["constraints"]["reservedFunds"] == 500
    assert len(work.list_goals(SAVE)) == 1
    assert all(todo["id"] not in old_ids for todo in _active_todos(work))
    assert len(_active_todos(work)) == 2
    assert all("计划数量=5" in todo["intent"] for todo in _active_todos(work))


def test_defer_and_reopen_do_not_restore_authorization(tmp_path):
    milestones, work = _stores(tmp_path)
    milestones.adopt(SAVE, NODE, planned_count=10, reserved_funds=1000,
                     work_store=work, game_date=SPRING_11)
    deferred = milestones.defer(SAVE, NODE, reason="先不准备", work_store=work,
                                game_date=SPRING_11)
    assert deferred["status"] == "deferred"
    assert _active_todos(work) == []
    assert work.list_goals(SAVE, active_only=True) == []

    reopened = milestones.reopen(SAVE, NODE, reason="重新商量", work_store=work,
                                 game_date=SPRING_11)
    assert reopened["status"] == "suggested"
    assert _active_todos(work) == []
    assert work.list_goals(SAVE, active_only=True) == []

    adopted = milestones.adopt(SAVE, NODE, planned_count=3, reserved_funds=300,
                               work_store=work, game_date=SPRING_11)
    assert adopted["status"] == "adopted"
    assert len(work.list_goals(SAVE)) == 1
    assert len(_active_todos(work)) == 2
    assert all("计划数量=3" in todo["intent"] for todo in _active_todos(work))
