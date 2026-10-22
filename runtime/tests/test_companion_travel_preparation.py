"""A player-confirmed walking proposal must reach the existing work scheduler."""

from stardew_ai_runtime.companion_milestones import CompanionMilestoneStore
from stardew_ai_runtime.work_state import WorkStore


def test_walking_proposal_can_gain_travel_preparation_without_changing_its_destination(tmp_path):
    milestones = CompanionMilestoneStore(tmp_path / "milestones.json")
    work = WorkStore(tmp_path / "work.json")
    node = milestones.propose("save", title="去海边看看", target_date="1:spring:1",
        summary="只走路去 Beach(30,15)，不购买、不采集，途中可暂停和取消。", preparation=[],
        execution_scope={"locationId": "Beach", "tiles": [[30, 15]]})
    assert not work.state("save").goals
    node = milestones.revise("save", node["id"], preparation=["travel"], work_store=work)
    assert not work.state("save").goals
    node = milestones.adopt("save", node["id"], work_store=work)
    state = work.state("save")
    assert len(state.goals) == 1
    goal = state.goals[0]
    assert goal.id == node["goalId"]
    assert goal.constraints["objectiveScope"]["preparation"] == ["travel"]
    assert goal.constraints["objectiveScope"]["executionScope"] == {
        "locationId": "Beach", "tiles": [{"x": 30, "y": 15}]}
    due = work.evaluate_todos("save", snapshot={}, game_date={"year": 1, "season": "spring", "day": 1})
    assert [todo["id"] for todo in due] == [node["todoIds"]["travel"]]
    assert "只沿可通行路线" in due[0]["intent"]
    milestones.defer("save", node["id"], work_store=work)
    assert work.state("save").goals[0].status != "active"
    assert not work.evaluate_todos("save", snapshot={}, game_date={"year": 1, "season": "spring", "day": 1})
