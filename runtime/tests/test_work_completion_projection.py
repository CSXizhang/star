"""Completion and cancellation must reach the player as settled work."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.work_state import Task


def test_terminal_model_turn_publishes_work_after_clearing_active_slot(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._await_work_provider = AsyncMock(return_value={"success": True, "response": "已完成"})
    bridge._claim_execution = AsyncMock()
    bridge._release_execution = AsyncMock()
    socket = MagicMock()
    socket.send_text = AsyncMock()
    asyncio.run(bridge.handle_chat_submit(socket, "req-test", "检查已完成的单株任务", "save"))
    profiles = [json.loads(call.args[0])["payload"] for call in socket.send_text.await_args_list
                if json.loads(call.args[0])["messageType"] == "life.profile.state"]
    assert profiles
    assert profiles[-1]["work"]["activity"]["phase"] != "planning"
    assert bridge._active_task is None


def test_cancelled_milestone_does_not_claim_future_preparation(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    node = bridge._milestone_store.propose("save", title="只浇一株", target_date="1:spring:1",
                                           preparation=["water"])
    adopted = bridge._milestone_store.adopt("save", node["id"], work_store=bridge._work_store)
    bridge._work_store.cancel_goal("save", adopted["goalId"])
    activity = bridge._player_activity("save")
    assert activity["phase"] == "idle"
    assert "准备时间" not in activity["summary"]


def test_completed_preparation_cannot_cover_a_later_cancelled_job(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    store = bridge._work_store
    node = bridge._milestone_store.propose("save", title="取放防风草", target_date="1:spring:1",
                                           preparation=["store"])
    adopted = bridge._milestone_store.adopt("save", node["id"], work_store=store)
    store.revise_goal("save", adopted["goalId"], status="completed")
    newer = store.add_goal("save", "补水后浇作物", source="user")
    store._mutate("save", lambda state: (
        state.tasks.append(Task("cancelled-task", newer.id, "补水后浇作物", status="cancelled")),
        setattr(state, "last_job", {"taskId": "cancelled-task", "taskTitle": "补水后浇作物",
                                   "goalId": newer.id, "status": "cancelled", "reasonCode": "CANCELLED"}),
    ))
    store.cancel_goal("save", newer.id)
    activity = bridge._player_activity("save")
    assert "补水后浇作物" in activity["summary"]
    assert "取消" in activity["summary"]
    assert "取放防风草" not in activity["summary"]
    assert "准备时间" not in activity["summary"]


def test_dispatched_native_work_remains_visible_after_model_decision_record_rotates(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    store = bridge._work_store
    store.begin_decision("save", "turn")
    store.submit_plan("save", goal_text="浇水", decision_token="turn", tasks=[{
        "id": "task", "title": "浇玩家指定菜地", "steps": [{"id": "step", "operation": "water_auto", "params": {}}]}])
    store.claim_next_step("save", "worker")
    store.assign_command_id("save", "task", "step", "native-command")
    store.revoke_decision("save")
    bridge._active_task = SimpleNamespace(save_id="save", cancelled=False)
    activity = bridge._player_activity("save")
    assert activity["phase"] == "working"
    assert "浇玩家指定菜地" in activity["summary"]
    assert "暂未开始" not in activity["summary"]
