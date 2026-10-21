"""Native-result crash boundaries and immutable player-request ownership."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.work_state import WorkStore


class Client:
    def __init__(self):
        self.dispatches = 0

    async def current_save_id(self):
        return "S"

    async def close(self):
        pass

    async def call_tool(self, name, params):
        if name == "dispatch_plan_operation":
            self.dispatches += 1
            return {"terminalState": "succeeded", "completedCount": 1, "targetCount": 1,
                    "effects": [{"tile": {"x": 1, "y": 2}, "state": "hoed"}],
                    "resources": {"waterCanLevel": 1, "maxWaterCanLevel": 40}}
        return {"status": "completed"}


def select(store, request="A", task="task-a", token="turn-a", goal=None, offered=None):
    store.record_request("S", request, f"Original instruction {request}", origin="player")
    goal = goal or store.add_goal("S", request, source="user").id
    store.begin_decision("S", token, request_id=request, request_ids=offered)
    store.submit_plan("S", decision_token=token, goal_id=goal, tasks=[{
        "id": task, "title": "hoe", "steps": [{"id": "step-a", "operation": "hoe_tiles",
                                               "params": {"tiles": [{"x": 1, "y": 2}]}}],
    }])
    return goal


def claim(store):
    item = store.claim_next_step("S", "worker")
    store.assign_command_id("S", item["taskId"], item["stepId"], "native-a")
    return item


def test_crash_after_native_commit_before_finish_or_callback_recovers_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    original = Client()
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", internal_plan_client=original)
    select(bridge._work_store)

    async def crash_after_commit(execution):
        if execution.status == "executed":
            raise RuntimeError("process stopped before finish_job / terminal callback")

    bridge._plan_worker.progress_callback = crash_after_commit

    async def run():
        with pytest.raises(RuntimeError, match="process stopped"):
            await bridge._plan_worker.evaluate()
        persisted = WorkStore(bridge._work_store.state_path)
        assert persisted.state("S").tasks[0].status == "completed"
        assert not persisted.state("S").decision.get("finished")
        event = persisted.pending_outbox("S")[0]
        assert event["requestId"] == "A"
        assert event["feedback"]["stepResults"][0]["completedCount"] == 1
        assert original.dispatches == 1

        restarted_client = Client()
        restarted = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False,
                               internal_plan_client=restarted_client)
        ws = AsyncMock()
        restarted._chat_ws = ws
        restarted._latest_snapshot_payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 1}}
        prompts = []
        restarted._execute_turn = MagicMock(side_effect=lambda task, cid, prompt: (
            prompts.append(prompt), {"success": True, "response": "one confirmed tile"})[1])
        restarted._maybe_deliver_outbox("S")
        await restarted.wait_for_deliveries()
        assert restarted_client.dispatches == 0
        assert not restarted._work_store.pending_outbox("S")
        assert "Original instruction A" in prompts[0]
        replies = [json.loads(call.args[0]) for call in ws.send_text.call_args_list]
        assert any(r.get("payload", {}).get("commandId") == "A"
                   and r.get("payload", {}).get("replyText") == "one confirmed tile" for r in replies)

    asyncio.run(run())


def test_failed_atomic_write_publishes_neither_terminal_nor_outbox(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "work.json")
    select(store)
    item = claim(store)
    monkeypatch.setattr(store, "_write_unlocked", MagicMock(side_effect=OSError("disk stopped")))
    with pytest.raises(OSError, match="disk stopped"):
        store.commit_step_result("S", task_id=item["taskId"], step_id=item["stepId"],
                                 command_id="native-a", outcome="completed")
    restored = WorkStore(store.state_path).state("S")
    assert restored.tasks[0].steps[0].status == "running"
    assert restored.tasks[0].steps[0].command_id == "native-a"
    assert restored.executions == [] and restored.outbox == []


def test_late_confirmation_is_a_correction_for_a_and_does_not_finish_b(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    select(store)
    item = claim(store)
    store.commit_step_result("S", task_id=item["taskId"], step_id=item["stepId"],
                             command_id="native-a", outcome="unknown")
    unknown = store.pending_outbox("S")[0]
    store.mark_outbox_consumed("S", [unknown["id"]])
    store.finish_job("S", {"status": "unknown"}, task_id="task-a", token="turn-a")
    select(store, request="B", task="task-b", token="turn-b")
    store.commit_step_result("S", task_id=item["taskId"], step_id=item["stepId"],
                             command_id="native-a", outcome="completed", effects=[{"state": "hoed"}])
    corrected = store.pending_outbox("S")[0]
    assert corrected["kind"] == "correction" and corrected["requestId"] == "A"
    assert store.state("S").decision["taskId"] == "task-b"
    assert not store.state("S").decision.get("finished")


def test_midflight_job_owns_b_not_the_deliverys_previous_request_a(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = select(store)
    store.record_request("S", "B", "different instruction B", origin="player-midflight")
    # Delivery selects B only after A's native command has settled.
    item = claim(store)
    store.commit_step_result("S", task_id=item["taskId"], step_id=item["stepId"],
                             command_id="native-a", outcome="completed", effects=[{"state": "hoed"}])
    store.begin_decision("S", "delivery", goal_default=goal, request_id="A", request_ids=["B"])
    store.submit_plan("S", decision_token="delivery", request_id="B", goal_text="new B", tasks=[{
        "id": "task-b", "title": "B", "steps": [{"id": "step-b", "operation": "hoe_tiles"}],
    }])
    bound = store.find_request_for_task("S", "task-b")
    assert bound["id"] == "B" and bound["text"] == "different instruction B"
    assert bound["consumedByDelivery"] is True
    assert store.state("S").decision["goalId"] != goal
    assert "goalScope" not in store.state("S").decision


def test_request_is_not_trimmed_between_selection_and_native_terminal(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    select(store)
    for index in range(55):
        store.record_request("S", f"old-{index}", "other resolved conversation")
    assert store.find_request_for_task("S", "task-a")["text"] == "Original instruction A"
