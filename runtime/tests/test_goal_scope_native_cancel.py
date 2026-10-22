"""A revised paused goal must retire its old native command before resuming."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.mcp_server import INTERNAL_TOOLS, LIFE_TOOLS, LIGHT_TOOLS
from stardew_ai_runtime.protocol import Envelope
from stardew_ai_runtime.work_state import WorkStateError


def _setup(tmp_path, *, revise=True, cancellation_confirmed=True, waiting=False):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    store = bridge._work_store
    node = bridge._milestone_store.propose("save", title="收获三株", target_date="1:spring:1",
        preparation=["harvest"], execution_scope={"locationId": "Farm", "tiles": [[61, 20], [62, 20], [66, 20]]})
    node = bridge._milestone_store.adopt("save", node["id"], planned_count=3, work_store=store)
    store.begin_decision("save", "test-turn", goal_scope=node["goalId"])
    store.submit_plan("save", goal_id=node["goalId"], decision_token="test-turn", tasks=[{
        "id": "old", "title": "旧范围收获", "steps": [{"id": "step", "operation": "harvest_auto", "params": {}}]}])
    store.claim_next_step("save", "worker")
    store.assign_command_id("save", "old", "step", "old-native")
    if waiting:
        store.mark_task_waiting("save", "old", step_id="step", condition={"type": "transportRetry",
            "params": {"attemptsUsed": 1, "backoffSeconds": 30}, "attemptsAllowed": 3})
    store.set_paused("save", True)
    bridge._autonomy.control("save", "pause")
    if revise:
        bridge._milestone_store.revise("save", node["id"], planned_count=2,
            execution_scope={"locationId": "Farm", "tiles": [[61, 20], [62, 20]]}, work_store=store)
    requested = []
    effects = [{"state": "harvested", "tile": {"x": 65, "y": 20}, "stack": 1}]

    async def cancel(name, arguments):
        requested.append((name, arguments))
        if not cancellation_confirmed:
            raise RuntimeError("Native control unavailable")
        return {"status": "cancellation-requested"}

    async def reconcile(command_id):
        assert command_id == "old-native"
        if requested and cancellation_confirmed:
            return {"found": True, "native": {"terminalState": "cancelled", "commandId": command_id,
                                               "effects": effects, "reasonCode": "CANCELLED"}}
        return {"found": False}

    client = SimpleNamespace(call_tool=AsyncMock(side_effect=cancel), reconcile=AsyncMock(side_effect=reconcile))
    bridge._plan_worker = SimpleNamespace(client=client, notify=MagicMock(), supplied_save_id="save",
                                         last_result=None, pending_reasons=[])
    ws = SimpleNamespace(send_text=AsyncMock())
    return bridge, client, ws, requested, effects


def test_resume_retires_only_revoked_command_and_keeps_partial_effects(tmp_path):
    bridge, client, ws, requested, effects = _setup(tmp_path)
    accepted = asyncio.run(bridge._handle_autonomy_control(ws,
        Envelope.create_autonomy_control(bridge.instance_id, "continue", "save", "resume"), "save"))
    assert accepted
    assert requested == [("cancel_plan_command", {"command_id": "old-native"})]
    state = bridge._work_store.state("save")
    assert not state.paused
    assert state.tasks[0].status == "cancelled"
    assert state.tasks[0].steps[0].outcome == "cancelled"
    assert state.executions[0].effects == effects
    assert state.goals[0].constraints["objectiveScope"]["executionScope"]["tiles"] == [
        {"x": 61, "y": 20}, {"x": 62, "y": 20}]
    assert client.reconcile.await_count == 2
    assert json.loads(ws.send_text.call_args.args[0])["payload"]["status"] == "confirmed"


def test_failed_native_retirement_keeps_pause_and_rejects_resume(tmp_path):
    bridge, _, ws, _, _ = _setup(tmp_path, cancellation_confirmed=False)
    accepted = asyncio.run(bridge._handle_autonomy_control(ws,
        Envelope.create_autonomy_control(bridge.instance_id, "continue", "save", "resume"), "save"))
    assert not accepted
    assert bridge._work_store.state("save").paused
    assert bridge._autonomy.state("save").paused
    reply = json.loads(ws.send_text.call_args.args[0])["payload"]
    assert reply["status"] == "rejected"
    assert "NATIVE_SCOPE_CHANGE_UNCONFIRMED" in reply["reason"]


def test_unchanged_paused_work_resumes_without_native_cancellation(tmp_path):
    bridge, client, ws, requested, _ = _setup(tmp_path, revise=False)
    assert asyncio.run(bridge._handle_autonomy_control(ws,
        Envelope.create_autonomy_control(bridge.instance_id, "continue", "save", "resume"), "save"))
    assert requested == []
    client.reconcile.assert_not_awaited()
    assert bridge._work_store.state("save").tasks[0].status == "running"


def test_targeted_retirement_is_harness_only():
    assert "cancel_plan_command" in INTERNAL_TOOLS
    assert "cancel_plan_command" not in LIGHT_TOOLS | LIFE_TOOLS


@pytest.mark.parametrize("conflict", [None, "command", "effects", "outcome"])
def test_revoked_command_duplicate_terminal_is_idempotent_only_for_same_result(tmp_path, conflict):
    bridge, _, _, _, effects = _setup(tmp_path)
    store = bridge._work_store
    result = dict(task_id="old", step_id="step", command_id="old-native",
                  outcome="partial", effects=effects, reason_code="CANCELLED")
    first = store.commit_step_result("save", **result)
    if conflict == "command":
        result["command_id"] = "different-native"
    elif conflict == "effects":
        result["effects"] = effects + [{"state": "harvested", "tile": {"x": 66, "y": 20}}]
    elif conflict == "outcome":
        result["outcome"] = "completed"
    if conflict:
        with pytest.raises(WorkStateError):
            store.commit_step_result("save", **result)
    else:
        assert store.commit_step_result("save", **result, feedback={"actualSummary": "one crop"}) == first
        assert store.state("save").tasks[0].steps[0].feedback == {"actualSummary": "one crop"}
    state = store.state("save")
    assert state.tasks[0].status == "cancelled"
    assert len(state.executions) == 1
    assert state.executions[0].effects == effects


def test_timed_out_revoked_work_reconciles_partial_effects_without_replay(tmp_path):
    bridge, client, ws, requested, effects = _setup(tmp_path, waiting=True)
    step = bridge._work_store.state("save").tasks[0].steps[0]
    assert step.status == "unknown"
    # Accept the legacy waiting state observed before this cancellation fix too.
    step.status = "waiting"
    client.reconcile.side_effect = None
    client.reconcile.return_value = {"found": True, "native": {"terminalState": "partially-succeeded",
        "commandId": "old-native", "effects": effects, "reasonCode": "IPC_FAILURE"}}
    assert asyncio.run(bridge._handle_autonomy_control(ws,
        Envelope.create_autonomy_control(bridge.instance_id, "continue", "save", "resume"), "save"))
    state = bridge._work_store.state("save")
    assert requested == []
    assert state.tasks[0].status == "cancelled"
    assert state.tasks[0].steps[0].outcome == "partial"
    assert state.executions[0].effects == effects
    assert not state.paused


@pytest.mark.parametrize("adopted", [True, False])
def test_accepted_work_context_distinguishes_current_adoption_from_saved_confirmation_words(tmp_path, adopted):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    node = bridge._milestone_store.propose("save", title="浇两株（待确认）", target_date="1:spring:1",
        summary="先只准备，等待确认和继续", preparation=["water"],
        execution_scope={"locationId": "Farm", "tiles": [[60, 21], [75, 22]]})
    if adopted:
        node = bridge._milestone_store.adopt("save", node["id"], planned_count=2, work_store=bridge._work_store)
        goal_id = node["goalId"]
    else:
        goal_id = bridge._work_store.add_goal("save", "另一项玩家目标", source="user").id
    bridge._autonomy.set_goal_scope("save", goal_id)
    bridge._autonomy.next_candidate = MagicMock(return_value={"kind": "decision", "reason": "resume"})
    snapshot = Envelope.from_mapping({"protocolVersion": "0.1", "messageType": "world.snapshot",
        "messageId": "snapshot-1", "senderInstanceId": bridge.instance_id, "sequenceNumber": 1,
        "worldRevision": 1, "sentAt": "2026-10-05T03:00:00Z", "payload": {
            "world": {"year": 1, "season": "spring", "dayOfMonth": 1, "timeOfDay": 900},
            "companion": {"location": "Farm", "tile": {"x": 60, "y": 20}}}})
    _, prompt = bridge._prepare_autonomy_decision(snapshot, "save")
    context = json.loads(prompt.split("实时上下文：", 1)[1].splitlines()[0])
    if adopted:
        assert context["acceptedArrangement"] == {"nodeId": node["id"], "goalId": goal_id,
            "status": "adopted", "paused": False}
        assert "待确认" in context["goals"][0]["text"]
    else:
        assert "acceptedArrangement" not in context
    bridge._autonomy.control("save", "pause")
    assert bridge._prepare_autonomy_decision(snapshot, "save") is None
