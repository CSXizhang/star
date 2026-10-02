"""Native facts and service failures reach an already-open player panel."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.job_feedback import compact_job_feedback
from stardew_ai_runtime.plan_executor import StepExecution
from stardew_ai_runtime.work_state import ExecutionEntry, Task


def test_native_terminal_pushes_dated_reason_without_another_chat(tmp_path):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._chat_ws = AsyncMock()
        store = bridge._work_store
        store._mutate("farm", lambda state: state.executions.extend([
            ExecutionEntry("legacy", "old", "old-step", "water_zone", "failed", reason_code="WAITING_TIMEOUT"),
            ExecutionEntry("new", "new-task", "new-step", "water_zone", "partial", reason_code="WATER_INCOMPLETE",
                           game_date="1:spring:16", game_time=1630),
        ]))
        await bridge._on_job_terminal("farm", StepExecution(status="executed", task_id="new-task", outcome="partial"), "terminal")
        frames = [json.loads(call.args[0]) for call in bridge._chat_ws.send_text.call_args_list]
        work = next(frame["payload"]["work"] for frame in frames if frame["messageType"] == "life.profile.state")
        old, new = work["recentExecutions"]
        assert old["gameDate"] is None and old["recordedAt"] and old["reason"] == "等待条件超过时限"
        assert new["gameDate"] == "1:spring:16" and new["gameTime"] == 1630
        assert new["reasonCode"] == "WATER_INCOMPLETE" and new["reason"] == "还有作物待浇水"
        before = bridge._chat_ws.send_text.call_count
        await bridge._push_work_state(bridge._chat_ws, "farm")
        assert bridge._chat_ws.send_text.call_count == before
    asyncio.run(scenario())


def test_breaker_projection_reports_current_provider_failure(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._autonomy.set_mode("farm", "free")
    for _ in range(3):
        bridge._autonomy.record_action_result("farm", success=False, reason="DSH_SESSION_OWNED")
    work = bridge._life_work_projection("farm")
    assert "模型服务连续失败" in work["pauseReason"] and "DSH_SESSION_OWNED" in work["pauseReason"]
    assert work["updatedAt"] == bridge._life_work_projection("farm")["updatedAt"]


@pytest.mark.parametrize("code,expected", [
    ("OUT_OF_SEEDS", "没有可用种子"),
    ("OUT_OF_WATER", "水壶没水了"),
    ("WAITING_TIMEOUT", "等待条件超过时限"),
    ("NEW_NATIVE_REASON", "原因类别：NEW_NATIVE_REASON"),
])
def test_failure_reason_is_visible_in_current_activity_ledger_and_feedback(tmp_path, code, expected):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    store = bridge._work_store
    goal = store.add_goal("farm", "继续种植", source="user")
    store._mutate("farm", lambda state: (
        state.tasks.append(Task("failed-task", goal.id, "播种当前地块", status="partial")),
        state.executions.append(ExecutionEntry("command", "failed-task", "step", "plant_zone", "partial", reason_code=code)),
        setattr(state, "last_job", {"taskId": "failed-task", "taskTitle": "播种当前地块", "status": "partial", "reasonCode": code}),
    ))
    work = bridge._life_work_projection("farm")
    assert expected in work["activity"]["summary"]
    assert expected in work["recentExecutions"][-1]["summary"]
    assert expected in work["recentExecutions"][-1]["reason"]
    assert "原因已记录" not in json.dumps(work, ensure_ascii=False)
    assert expected in compact_job_feedback({"status": "partial", "reasonCode": code})["reason"]


def test_legacy_last_job_uses_matching_recorded_reason_not_an_unrelated_result(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    store = bridge._work_store
    store._mutate("farm", lambda state: (
        state.executions.extend([
            ExecutionEntry("old", "actual-task", "s1", "plant_zone", "partial", reason_code="OUT_OF_SEEDS"),
            ExecutionEntry("other", "other-task", "s2", "water_zone", "partial", reason_code="OUT_OF_WATER"),
        ]),
        setattr(state, "last_job", {"taskId": "actual-task", "taskTitle": "播种", "status": "partial"}),
    ))
    result = bridge._player_activity("farm")
    assert "没有可用种子" in result["summary"]
    assert "水壶没水" not in result["summary"]
