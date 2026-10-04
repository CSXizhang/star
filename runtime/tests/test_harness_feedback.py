"""Actual native progress survives composite work, truncation and persistence."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge, PlanWorker
from stardew_ai_runtime.job_feedback import compact_job_feedback, compact_task_feedback
from stardew_ai_runtime.work_state import WorkStore


def plant_receipt(command_id="plant", *, targets=19, completed=2):
    return {"commandId": command_id, "status": "completed", "terminalState": "succeeded",
            "targetCount": targets, "completedCount": completed,
            "skippedCount": targets - completed, "failedCount": 0,
            "unprocessedTargetCount": 0, "worldRevision": 77,
            "effects": [{"state": "planted", "tile": {"x": x, "y": 20}}
                        for x in range(completed)],
            "details": {"skippedTiles": [{"tile": {"x": x, "y": 20}, "reason": "already-occupied"}
                                         for x in range(completed, targets)]}}


@pytest.mark.parametrize("targets,completed", [(19, 2), (2, 1), (100, 83)])
def test_native_counts_and_skip_reasons_survive_detail_truncation(targets, completed):
    raw = plant_receipt(targets=targets, completed=completed)
    raw["inventoryDelta"] = {"large": "x" * 10000}
    feedback = compact_job_feedback(raw, operation="plant_seeds")
    assert feedback["detailsTruncated"]
    assert feedback["targetCount"] == targets
    assert feedback["completedCount"] == completed
    assert feedback["skippedCount"] == targets - completed
    assert feedback["skipReasons"] == {"already-occupied": targets - completed}
    assert feedback["actualSummary"] == f"已种下 {completed} 格"
    assert feedback["worldRevision"] == 77
    assert f"已种下 {targets} 格" not in feedback["progressSummary"]


def test_composite_worker_uses_same_actual_facts_for_prompt_reply_memory_and_care(tmp_path):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        store = bridge._work_store
        store.begin_decision("farm", "token")
        selected = store.submit_plan("farm", goal_text="全部种完并浇水", tasks=[{
            "title": "种下19包种子并浇水", "steps": [
                {"operation": "plant_seeds", "params": {"seed_item_id": "(O)472",
                 "tiles": [{"x": x, "y": 20} for x in range(19)]}},
                {"operation": "water_tiles", "params": {"tiles": [{"x": x, "y": 20} for x in range(2)]}},
            ]}], decision_token="token")
        task_id = selected["tasks"][0]["id"]
        ws = AsyncMock()
        bridge._job_reply_binding = ("player-request", "farm", task_id, ws)
        bridge._maybe_fire_care = AsyncMock()
        calls = []

        class Client:
            async def current_save_id(self):
                return "farm"

            async def call_tool(self, name, arguments):
                calls.append(arguments["operation"])
                command_id = arguments["command_id"]
                if arguments["operation"] == "plant_seeds":
                    return plant_receipt(command_id)
                return {"commandId": command_id, "status": "completed", "terminalState": "succeeded",
                        "targetCount": 2, "completedCount": 2, "skippedCount": 0, "failedCount": 0,
                        "unprocessedTargetCount": 0, "worldRevision": 78,
                        "effects": [{"state": "watered", "tile": {"x": x, "y": 20}} for x in range(2)]}

        worker = PlanWorker(store, Client())
        worker.progress_callback = bridge._publish_job_progress
        worker.terminal_callback = bridge._on_job_terminal
        await worker.evaluate()
        assert calls == ["plant_seeds", "water_tiles"]
        task = next(task for task in WorkStore(store.state_path).state("farm").tasks if task.id == task_id)
        assert task.steps[0].feedback["skippedCount"] == 17
        feedback = store.state("farm").last_job
        assert feedback["status"] == "completed"
        assert feedback["stepResults"][0]["completedCount"] == 2
        assert feedback["stepResults"][0]["skipReasons"] == {"already-occupied": 17}
        assert feedback["actualSummary"] == "已种下 2 格，已浇水 2 格"
        assert "跳过 17" in feedback["progressSummary"]
        assert feedback["remainingSteps"] == []
        replies = [json.loads(call.args[0])["payload"].get("replyText", "")
                   for call in ws.send_text.call_args_list]
        assert any("已种下 2 格，已浇水 2 格" in reply and "跳过 17" in reply for reply in replies)
        events = [row for row in bridge._memory_store.list("farm")["entries"] if row["kind"] == "event"]
        assert len(events) == 1
        fact = events[0]["text"]
        assert "已种下 2 格，已浇水 2 格" in fact and "跳过 17" in fact
        assert "完成了「种下19" not in fact
        assert bridge._maybe_fire_care.await_args.kwargs["fact"] == fact
        assert bridge._decision_context("farm")["lastResult"]["progressSummary"] == feedback["progressSummary"]
        await worker.evaluate()
        assert len(calls) == 2

    asyncio.run(scenario())


def test_large_partial_receipt_returns_unprocessed_work_and_stops_later_steps(tmp_path):
    async def scenario():
        store = WorkStore(tmp_path / "work.json")
        store.begin_decision("farm", "token")
        store.submit_plan("farm", goal_text="浇完100格后入箱", tasks=[{"title": "浇水再入箱", "steps": [
            {"operation": "water_tiles", "params": {"tiles": [{"x": x, "y": 20} for x in range(100)]}},
            {"operation": "deposit_to_chest", "params": {"chest_x": 1, "chest_y": 1}},
        ]}], decision_token="token")
        calls = []

        class Client:
            async def current_save_id(self):
                return "farm"

            async def call_tool(self, name, arguments):
                calls.append(arguments["operation"])
                return {"commandId": arguments["command_id"], "terminalState": "partially-succeeded",
                        "targetCount": 100, "completedCount": 40, "skippedCount": 0, "failedCount": 0,
                        "unprocessedTargetCount": 60, "reasonCode": "OUT_OF_WATER",
                        "effects": [{"state": "watered", "tile": {"x": x, "y": 20}} for x in range(40)]}

        worker = PlanWorker(store, Client())
        await worker.evaluate()
        await worker.evaluate()
        assert calls == ["water_tiles"]
        feedback = WorkStore(store.state_path).state("farm").last_job
        assert feedback["status"] == "partial"
        assert feedback["completedCount"] == 40 and feedback["unprocessedTargetCount"] == 60
        assert "还有 60 个目标未处理" in feedback["progressSummary"]
        assert len(feedback["remainingSteps"]) == 2
        assert feedback["stepResults"][0]["unprocessedTargetCount"] == 60

    asyncio.run(scenario())


def test_reconciled_refill_candidates_are_not_unfinished_farm_work():
    raw = {"terminalState": "succeeded", "completedCount": 1, "skippedCount": 0, "failedCount": 0,
           "effects": [{"state": "refilled"}], "finalWorldRevision": 22}
    feedback = compact_job_feedback(raw, operation="refill_watering_can", status="completed",
        params={"tiles": [{"x": x, "y": 3} for x in range(4)]})
    assert feedback["candidateCount"] == 4
    assert feedback["targetCount"] == 1 and feedback["unprocessedTargetCount"] == 0
    assert feedback["progressSummary"] == "水壶已补满"
    assert feedback["nativeCountsScope"] == "waterSourceCandidates"
    unknown = compact_job_feedback({**raw, "terminalState": "unknown"},
        operation="refill_watering_can", status="unknown", params={"tiles": [{"x": 1, "y": 3}]})
    assert "unprocessedTargetCount" not in unknown


def test_auto_scope_candidates_do_not_establish_a_native_target_count():
    feedback = compact_job_feedback({"terminalState": "succeeded", "completedCount": 2,
                                     "skippedCount": 0, "failedCount": 0,
                                     "effects": [{"state": "watered", "tile": {"x": x, "y": 2}}
                                                 for x in range(2)]},
                                    operation="water_auto", status="completed",
                                    params={"target_tiles": [{"x": x, "y": 2} for x in range(19)]})
    assert feedback["completedCount"] == 2
    assert "targetCount" not in feedback and "unprocessedTargetCount" not in feedback
    assert feedback["actualSummary"] == "已浇水 2 格"


def test_success_code_does_not_add_a_missing_error_explanation():
    feedback = compact_job_feedback({"status": "completed", "reasonCode": "OK",
                                     "effects": [{"state": "watered", "tile": {"x": 1, "y": 2}}]},
                                    operation="water_tiles")
    assert feedback["reasonCode"] == "OK"
    assert "reason" not in feedback
    assert feedback["progressSummary"] == "已浇水 1 格"


def no_work_receipt():
    return {"status": "no-work", "outcome": "completed", "goalSatisfied": True,
            "reasonCode": "NO_WORK", "terminalState": "none", "targetCount": 0,
            "remaining": {"unwateredTiles": 0}, "effects": [], "snapshotRevision": 2}


def test_known_empty_scope_survives_compaction_and_step_projection_without_inventing_effects():
    raw = no_work_receipt()
    raw["inventoryDelta"] = {"large": "x" * 10000}
    receipt = compact_job_feedback(raw, operation="water_auto", status="completed")
    assert receipt["detailsTruncated"] is True
    assert receipt["goalSatisfied"] is True and receipt["knownNoWork"] is True
    assert receipt["actualSummary"] == receipt["progressSummary"] == "所选范围当前无待处理工作"
    assert "reason" not in receipt and receipt["effectCount"] == 0
    task = SimpleNamespace(steps=[SimpleNamespace(id="water", operation="water_auto", status="completed",
                                                effects=[], feedback=receipt)])
    feedback = compact_task_feedback(task, no_work_receipt(), operation="water_auto", status="completed")
    assert feedback["effects"] == [] and feedback["knownNoWork"] is True
    assert feedback["stepResults"][0]["goalSatisfied"] is True
    assert feedback["stepResults"][0]["knownNoWork"] is True
    assert feedback["progressSummary"] == "所选范围当前无待处理工作"


@pytest.mark.parametrize("changes,normalized_status", [
    ({"goalSatisfied": False}, "completed"),
    ({"scopeObservationIncomplete": True}, "completed"),
    ({"isTruncated": True}, "completed"),
    ({"outcome": "partial"}, "partial"),
    ({"terminalState": "unknown"}, "unknown"),
    ({"remaining": {"unwateredTiles": None}}, "completed"),
    ({"remaining": {}}, "completed"),
    ({"targetCount": False}, "completed"),
    ({"failedCount": 1}, "completed"),
    ({"effects": [{"state": "watered", "tile": {"x": 1, "y": 1}}]}, "completed"),
])
def test_incomplete_or_unknown_observations_are_never_known_no_work(changes, normalized_status):
    raw = {**no_work_receipt(), **changes, "knownNoWork": True}
    feedback = compact_job_feedback(raw, operation="water_auto", status=normalized_status)
    assert feedback["knownNoWork"] is False
    assert feedback["goalSatisfied"] is raw["goalSatisfied"]
    assert feedback["actualSummary"] != "所选范围当前无待处理工作"


def test_last_empty_step_does_not_hide_composite_effects_or_accumulate_other_jobs():
    planted = [{"state": "planted", "tile": {"x": 1, "y": 1}}]
    no_work = compact_job_feedback(no_work_receipt(), operation="water_auto", status="completed")
    task = SimpleNamespace(steps=[
        SimpleNamespace(id="plant", operation="plant_seeds", status="completed", effects=planted,
                        feedback=compact_job_feedback({"status": "completed", "effects": planted}, operation="plant_seeds")),
        SimpleNamespace(id="water", operation="water_auto", status="completed", effects=[], feedback=no_work),
    ])
    composite = compact_task_feedback(task, no_work_receipt(), operation="water_auto", status="completed")
    assert composite["knownNoWork"] is False and composite["goalSatisfied"] is True
    assert composite["actualSummary"] == "已种下 1 格"
    assert composite["stepResults"][1]["knownNoWork"] is True
    independent = compact_task_feedback(SimpleNamespace(steps=[task.steps[1]]), no_work_receipt(),
                                        operation="water_auto", status="completed")
    assert independent["knownNoWork"] is True and independent["effects"] == []
    assert independent["actualSummary"] == "所选范围当前无待处理工作"
