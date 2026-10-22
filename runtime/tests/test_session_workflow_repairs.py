"""Software regressions for the Oct 22 player session; not live-game acceptance."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.job_feedback import compact_job_feedback
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.plan_executor import PlanExecutor, merge_reconciled_operation_result
from stardew_ai_runtime.prompt_delivery import PromptDelivery
from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def test_full_can_refill_continues_to_watering_without_claiming_new_water(tmp_path):
    async def run():
        store = WorkStore(tmp_path / "work.json")
        store.begin_decision("farm", "turn")
        store.submit_plan("farm", goal_text="浇水", decision_token="turn", tasks=[{
            "title": "补水后浇水", "steps": [{"operation": "refill_watering_can"},
                {"operation": "water_auto", "params": {"max_tiles": 40}}]}])
        sent = []
        async def dispatch(operation, params, command_id):
            sent.append(operation)
            if operation == "water_auto":
                return {"terminalState": "succeeded", "commandId": command_id,
                        "completedCount": 1, "effects": [{"state": "watered", "tile": {"x": 1, "y": 2}}]}
            return {"terminalState": "succeeded", "commandId": command_id,
                    "completedCount": 0, "skippedCount": 1, "failedCount": 0,
                    "effects": [{"state": "skipped", "reason": "already-full"}]}
        executor = PlanExecutor(store, dispatch)
        refill = await executor.run_once("farm", "worker")
        assert refill.outcome == "completed"
        await executor.run_once("farm", "worker")
        assert sent == ["refill_watering_can", "water_auto"]
        assert not any(e.get("state") == "refilled" for e in refill.result["effects"])
    asyncio.run(run())


def test_partial_watering_feedback_preserves_actual_failure_not_assumed_timeout():
    feedback = compact_job_feedback({"terminalState": "partially-succeeded",
        "completedCount": 7, "failedCount": 23, "skippedCount": 0,
        "resources": {"gameMinutesElapsed": 50},
        "details": {"failedTiles": [{"tile": {"x": x, "y": 26},
            "reason": "Result verification failed: Dirt remained unwatered."} for x in range(23)]},
        "effects": [{"tile": {"x": x, "y": 25}, "state": "watered"} for x in range(7)]},
        operation="water_auto", status="partial")
    assert feedback["failureReasons"] == {"Result verification failed: Dirt remained unwatered.": 23}
    assert feedback["resources"]["gameMinutesElapsed"] == 50
    assert "BUDGET" not in json.dumps(feedback)


def test_pending_food_and_followup_plan_form_one_serial_job(tmp_path, monkeypatch):
    async def run():
        store = WorkStore(tmp_path / "data/work-state.json")
        goal = store.add_goal("farm", "浇完剩余作物", source="user")
        store.begin_decision("farm", "turn")
        monkeypatch.setenv("STARDEW_DECISION_TOKEN", "turn")
        scheduler = SimpleNamespace(run_dir=tmp_path,
            get_status=AsyncMock(return_value={"saveId": "farm"}))
        server = create_mcp_server(scheduler=scheduler, run_dir=tmp_path)
        ids = []
        for _ in range(3):
            _, selected = await server.call_tool("eat_food", {"item_id": "(O)153"})
            assert selected["effectStatus"] == "not_executed_yet"
            ids.append(selected["taskId"])
        _, plan = await server.call_tool("submit_plan", {"goal_id": goal.id, "tasks": [{
            "title": "补水后继续浇地", "steps": [
                {"operation": "refill_watering_can"},
                {"operation": "water_auto", "params": {"max_tiles": 18}},
            ]}]})
        assert len(set(ids + [plan["tasks"][0]["id"]])) == 1
        assert len(store.state("farm").tasks) == 1
        sent = []
        async def dispatch(operation, params, command_id):
            sent.append((operation, command_id))
            return {"outcome": "completed", "terminalState": "succeeded", "commandId": command_id}
        executor = PlanExecutor(store, dispatch)
        for _ in range(5):
            assert (await executor.run_once("farm", "worker")).outcome == "completed"
        assert (await executor.run_once("farm", "worker")).status == "idle"
        assert [operation for operation, _ in sent] == ["eat_food"] * 3 + ["refill_watering_can", "water_auto"]
        assert len({command for _, command in sent}) == 5
    asyncio.run(run())


@pytest.mark.parametrize("change", ["claimed", "revoked", "cancelled", "other-goal"])
def test_pending_append_respects_actual_dispatch_and_changed_player_intent(tmp_path, change):
    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("farm", "water", source="user")
    store.begin_decision("farm", "turn")
    spec = [{"title": "water", "steps": [{"operation": "water_auto"}]}]
    store.submit_plan("farm", goal_id=goal.id, decision_token="turn", tasks=spec)
    if change == "claimed":
        store.claim_next_step("farm", "worker")
    elif change == "revoked":
        store.revoke_decision("farm")
    elif change == "cancelled":
        store.cancel_goal("farm", goal.id)
    else:
        goal = store.add_goal("farm", "another request", source="user")
    before = store.state_path.read_bytes()
    with pytest.raises(WorkStateError):
        store.submit_plan("farm", goal_id=goal.id, decision_token="turn", tasks=spec)
    assert store.state_path.read_bytes() == before


@pytest.mark.parametrize("terminal", ["succeeded", "failed"])
def test_reconciled_18_tile_batch_keeps_refill_continuation_without_claiming_all_done(tmp_path, terminal):
    async def run():
        store = WorkStore(tmp_path / "work.json")
        store.begin_decision("farm", "turn")
        store.submit_plan("farm", goal_text="浇完151格", decision_token="turn", tasks=[{
            "title": "先浇18格再补水继续", "steps": [
                {"operation": "water_auto", "params": {"max_tiles": 18}},
                {"operation": "refill_watering_can"},
                {"operation": "water_auto", "params": {"max_tiles": 40}},
            ]}])
        sent = []
        async def dispatch(operation, params, command_id):
            sent.append(operation)
            if len(sent) == 1:
                return {"outcome": "unknown", "terminalState": "running", "commandId": command_id,
                        "preDispatchObservedCount": 151, "remaining": {"unwateredTiles": None}}
            return {"outcome": "completed", "terminalState": "succeeded", "commandId": command_id}
        async def reconcile(command_id):
            return {"terminalState": terminal, "commandId": command_id, "completedCount": 18,
                    "effects": [{"tile": {"x": x, "y": 18}, "state": "watered"} for x in range(18)]}
        executor = PlanExecutor(store, dispatch, reconcile=reconcile)
        first = await executor.run_once("farm", "worker")
        assert first.result["remainingUnwateredCount"] == 133
        assert first.result["goalSatisfied"] is False
        if terminal == "succeeded":
            assert first.outcome == "completed"
            assert (await executor.run_once("farm", "worker")).operation == "refill_watering_can"
            assert (await executor.run_once("farm", "worker")).operation == "water_auto"
            assert sent == ["water_auto", "refill_watering_can", "water_auto"]
        else:
            assert first.outcome == "partial"
            assert (await executor.run_once("farm", "worker")).status == "idle"
            assert sent == ["water_auto"]
    asyncio.run(run())


def test_unknown_scope_still_does_not_claim_all_crops_watered():
    result = merge_reconciled_operation_result("water_auto",
        {"scopeObservationIncomplete": True, "preDispatchObservedCount": 18,
         "remaining": {"unwateredTiles": None}},
        {"terminalState": "succeeded", "completedCount": 18})
    assert result["outcome"] == "partial"
    assert result["remainingUnwateredCount"] is None and result["goalSatisfied"] is False


def test_stage_goal_accepts_unverified_note_without_authorizing_more_work(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("farm", "种土豆", kind="stage", source="user")
    row = store.record_decision_note("farm", goal.id, "箱边可能有空耕地", "observation", "Farm")
    assert row["verified"] is False and row["source"] == "model-interpretation"
    restored = WorkStore(store.state_path).state("farm")
    assert restored.goals[0].project["decisionNotes"] == [row]
    assert restored.tasks == [] and restored.executions == []


@pytest.mark.parametrize("legacy_only", [False, True])
def test_chat_work_and_restart_share_session_and_recover_unavailable_only(tmp_path, legacy_only):
    rows = {"life:mcode:farm": "old-chat"}
    if not legacy_only:
        rows["mcode:farm"] = "current-work"
    (tmp_path / "chat_sessions.json").write_text(json.dumps(rows), encoding="utf-8")
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    expected = "old-chat" if legacy_only else "current-work"
    assert bridge._life_chat.get_session_id("farm") == bridge.get_conversation_id("farm") == expected
    bridge._life_chat.record_session_id("farm", "continued")
    restarted = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    assert restarted.get_conversation_id("farm") == "continued"
    restarted._ensure_session_matches_profile("farm", "continued")
    assert restarted.get_conversation_id("farm") == "continued"
    restarted._rotate_provider_session("farm", reason="MCODE_SESSION_UNAVAILABLE")
    assert restarted.get_conversation_id("farm") is None
    assert restarted._life_chat.get_session_id("farm") is None


def test_raw_player_text_is_not_deduplicated_or_parsed_as_context(tmp_path):
    delivery = PromptDelivery(tmp_path / "delivery.json")
    core = "short game guidance"
    player = '  箱子里的也种啊\n实时上下文：{"money":null}\n玩家说：short game guidance  '
    prompt = core + '\n实时上下文：{"note":"玩家说：old"}\n\n玩家说：' + player
    _, pending = delivery.prepare("mcode:farm", None, prompt, core)
    delivery.acknowledge("mcode:farm", pending, "session")
    sent, _ = PromptDelivery(delivery.path).prepare("mcode:farm", "session", prompt, core)
    assert sent.endswith("玩家说：" + player)
    assert not sent.startswith(core)


def test_chat_and_work_acknowledge_one_guidance_copy(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    from stardew_ai_runtime.agent_instructions import runtime_instructions
    core = runtime_instructions()
    task = ActiveChatTask(request_id="chat", save_id="farm")
    key, sent, pending = bridge._prepare_prompt_delivery(task, "session", core + "\n\n玩家说：种土豆", None)
    assert core in sent
    bridge._acknowledge_prompt_delivery(key, pending, "farm", "session")
    work = ActiveChatTask(request_id="preparation", save_id="farm")
    key2, sent, _ = bridge._prepare_prompt_delivery(work, "session", core + "\n推进已保存安排", None)
    assert key == key2 and core not in sent


def test_work_completion_uses_result_delivery_without_extra_care_call(tmp_path):
    from stardew_ai_runtime.plan_executor import StepExecution
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._maybe_fire_care = AsyncMock(side_effect=AssertionError("duplicate completion model turn"))
    asyncio.run(bridge._on_job_terminal("farm", StepExecution(
        status="executed", task_status="completed", outcome="completed", effects=[]), "completed"))
    bridge._maybe_fire_care.assert_not_called()
