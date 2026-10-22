import asyncio
from unittest.mock import AsyncMock

from stardew_ai_runtime.decision_context import build_decision_context
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.work_state import WorkStore


def test_current_plan_and_region_survive_partial_update_restart(tmp_path):
    path = tmp_path / "work.json"
    store = WorkStore(path)
    goal = store.add_goal("farm", "养鸡并累计种植200颗", source="user")
    region = {"locationId": "Farm", "bounds": {"x": 18, "y": 3, "width": 8, "height": 8}}
    store.revise_goal("farm", goal.id, project={"farmRegion": region, "nextAction": "购入当季种子"})
    store.revise_goal("farm", goal.id, project={"phase": "seed-supply", "blocker": "春季种子不适用"})
    restarted = WorkStore(path)
    context = build_decision_context({"world": {"season": "summer"}, "farmWork": {
                                        "locationId": "Farm", "observationStatus": "observed", "deadCropCount": 50}},
                                     work=restarted.overview("farm"))
    project = context["goals"][0]["project"]
    assert project["farmRegion"] == region
    assert project["nextAction"] == "购入当季种子"
    assert context["farmWork"]["deadCropCount"] == 50
    assert context["farmWork"]["cropUnwateredCount"] == "unknown"


def test_region_query_after_travel_uses_saved_bounds_and_keeps_clear_facts():
    async def run():
        sched = CompanionScheduler.__new__(CompanionScheduler)
        bounds = {"x": 18, "y": 3, "width": 8, "height": 8}
        facts = {"regions": [{"bounds": bounds, "usableCapacity": 60, "deadCropCount": 40,
                             "clearWithScytheTiles": [{"x": 18, "y": 3}]}]}
        sched._execute_native_action = AsyncMock(return_value={"details": {"planting": facts}})
        result = await sched.query_planting_options(location_id="Farm", region=bounds)
        sched._execute_native_action.assert_awaited_once_with("inspect-planting", {"locationId": "Farm", "region": bounds}, tiles=[])
        assert result["regions"][0]["deadCropCount"] == 40
        assert "clearWithScytheTiles" not in result["regions"][0]
        detailed = await sched.query_planting_options(detail=True, location_id="Farm", region=bounds)
        assert detailed["regions"][0]["clearWithScytheTiles"] == [{"x": 18, "y": 3}]
    asyncio.run(run())
