"""Discriminating tests for the persistent outbox delivery contract.

A player-owned native terminal is persisted as an outbox event attributed to
its owning request and delivered at the next model-slot boundary as one
delivery turn of the same session.  These tests lock the semantics the
command-chain mechanism used to carry:

* terminal -> event -> one delivery turn carrying the facts JSON and the
  original request text, consumed only after the reply reached the player;
* a failed delivery turn leaves the facts ``sent`` and the next boundary
  redelivers them -- native work is never re-dispatched by a delivery;
* duplicate terminals dedupe by the stable plan command id;
* a late terminal attributes to the task's own request, never to whatever
  player request happens to be active now;
* an ``unknown`` fact followed by a confirmed receipt for the same command
  id is stored as a ``correction`` and delivered again;
* the three crash windows (pending / sent-unconfirmed / consumed) survive a
  bridge restart, and redelivery only re-states facts, never re-dispatches.

Fake clients/WebSockets and isolated ``tmp_path`` run dirs only; the model
turn is a ``MagicMock`` replacement of ``_execute_turn`` (the same seam as
``test_runtime_dispatch_regressions``), so no provider process ever runs.
"""

import asyncio
import json
from unittest.mock import MagicMock

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.plan_executor import StepExecution

COMMAND_ID = "plan:Save1:t1:s1:attempt-1"


class FakeClient:
    """Internal plan client double; records native dispatches and closes."""

    def __init__(self, save_id="Save1"):
        self.save_id = save_id
        self.dispatched = []
        self.closed = 0

    async def current_save_id(self):
        return self.save_id

    async def call_tool(self, name, params):
        if name == "dispatch_plan_operation":
            self.dispatched.append(params.get("operation"))
            return {"status": "completed", "effects": [{"kind": "done"}]}
        return {"status": "completed"}

    async def close(self):
        self.closed += 1


class FakeWs:
    """Chat-channel double recording every wire payload."""

    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)

    def replies(self):
        return [json.loads(m)["payload"] for m in self.sent
                if json.loads(m).get("messageType") == "chat.reply"]


def make_bridge(tmp_path, monkeypatch, client=None, enable_plan_worker=False):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=enable_plan_worker,
                        internal_plan_client=client or FakeClient())
    # These delivery tests begin after the native connection has supplied its
    # initial snapshot; reconnect-before-observation is tested separately.
    bridge._latest_snapshot_payload = {"world": {}}
    return bridge


def prime_player_job(bridge, save_id="Save1", request_id="req-1", text="种植胡萝卜"):
    """Record the player request and bind it to a task, as job_selected would."""
    store = bridge._work_store
    store.record_request(save_id, request_id, text, origin="player")
    goal = store.add_goal(save_id, text, source="user")
    store.begin_decision(save_id, "tok-1")
    store.submit_plan(save_id, goal_id=goal.id, decision_token="tok-1", tasks=[{
        "id": "t1", "title": "锄地",
        "steps": [{"id": "s1", "operation": "hoe_tiles", "params": {}}],
    }])
    store.bind_request_task(save_id, request_id, "t1", goal.id)
    bridge._active_player_requests[save_id] = request_id
    return goal


def terminal_execution(command_id=COMMAND_ID, outcome="completed", reason_code=None,
                       effects=None, task_id="t1"):
    return StepExecution(status="executed", task_id=task_id, command_id=command_id,
                         outcome=outcome, task_status=outcome,
                         effects=effects if effects is not None else [{"state": "hoed"}],
                         reason_code=reason_code)


def facts_payload(prompt):
    line = next(line for line in prompt.splitlines() if line.startswith("终态事实："))
    return json.loads(line.removeprefix("终态事实："))


def test_terminal_fact_is_delivered_with_request_text_and_consumed(tmp_path, monkeypatch):
    client = FakeClient()
    bridge = make_bridge(tmp_path, monkeypatch, client)
    ws = FakeWs()
    bridge._chat_ws = ws
    prime_player_job(bridge)

    prompts = []

    def fake_turn(active_task, conversation_id, prompt):
        prompts.append(prompt)
        return {"success": True, "response": "胡萝卜种好了。", "conversation_id": "sess-1"}

    bridge._execute_turn = MagicMock(side_effect=fake_turn)

    async def run():
        await bridge._on_job_terminal("Save1", terminal_execution(), "SHORT_JOB_TERMINAL")
        events = bridge._work_store.pending_outbox("Save1")
        assert len(events) == 1
        assert events[0]["requestId"] == "req-1"
        assert events[0]["commandId"] == COMMAND_ID

        await bridge.wait_for_deliveries()

        # Exactly one model turn: the delivery prompt carries the terminal
        # facts JSON plus the original request text.
        assert len(prompts) == 1
        payload = facts_payload(prompts[0])
        assert len(payload["terminalFacts"]) == 1
        fact = payload["terminalFacts"][0]
        assert fact["kind"] == "terminal"
        assert fact["commandId"] == COMMAND_ID
        assert fact["outcome"] == "completed"
        assert fact["requestText"] == "种植胡萝卜"
        assert fact["effects"] == [{"state": "hoed"}]

        # The reply reached the player channel on the owning request's
        # command id, and only then is the fact consumed.
        event = bridge._work_store.state("Save1").outbox[0]
        assert event["delivery"]["state"] == "consumed"
        assert not bridge._work_store.pending_outbox("Save1")
        final = ws.replies()[-1]
        assert final["replyText"].startswith("胡萝卜种好了")
        assert final["commandId"] == "req-1"
        assert final["commandComplete"] is True
        assert final["requestId"].startswith("delivery-")
        # A delivery turn reports facts; it never dispatches native work.
        assert client.dispatched == []

    asyncio.run(run())


def test_acknowledging_a_does_not_consume_b_before_later_job_selection(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    bridge._chat_ws = FakeWs()
    goal = prime_player_job(bridge)
    store = bridge._work_store
    store.record_request("Save1", "B", "After A, deposit two and report the water", origin="player-midflight")
    bridge._execute_turn = MagicMock(return_value={"success": True, "response": "A done; B noted"})

    async def deliver():
        await bridge._on_job_terminal("Save1", terminal_execution(), "SHORT_JOB_TERMINAL")
        await bridge.wait_for_deliveries()
    asyncio.run(deliver())
    assert [request["id"] for request in store.pending_midflight_requests("Save1")] == ["B"]
    assert store.state("Save1").outbox[0]["delivery"]["state"] == "consumed"

    # A later turn has no in-memory delivery candidates. B must remain offered
    # from the durable queue, so selecting its exact id cannot fall back to A.
    del bridge._execute_turn

    class Backend:
        def run(self, active_task, conversation_id, prompt):
            assert "B" in store.state("Save1").decision["requestIds"]
            store.submit_plan("Save1", goal_id=goal.id, decision_token=active_task.decision_token,
                              request_id="B", tasks=[{"id": "job-B", "title": "B deposit",
                              "steps": [{"operation": "deposit_to_chest", "params": {}}]}])
            return {"success": True, "response": "B selected"}

    bridge._backend = Backend()
    bridge._execute_turn(ActiveChatTask(request_id="autonomy-next", save_id="Save1"), None, "Continue")
    request_b = next(request for request in store.state("Save1").requests if request["id"] == "B")
    assert request_b["taskIds"] == ["job-B"]
    assert not store.pending_midflight_requests("Save1")


def test_failed_delivery_keeps_facts_sent_and_redelivers_without_dispatch(tmp_path, monkeypatch):
    client = FakeClient()
    bridge = make_bridge(tmp_path, monkeypatch, client)
    ws = FakeWs()
    bridge._chat_ws = ws
    prime_player_job(bridge)

    outcomes = [{"success": False, "response": "", "error": "PROVIDER_UNAVAILABLE"},
                {"success": True, "response": "补报：胡萝卜种好了。", "conversation_id": "sess-1"}]
    prompts = []
    bridge._execute_turn = MagicMock(side_effect=lambda task, cid, prompt: (
        prompts.append(prompt), outcomes.pop(0))[1])

    async def run():
        await bridge._on_job_terminal("Save1", terminal_execution(), "SHORT_JOB_TERMINAL")
        await bridge.wait_for_deliveries()

        # The failed turn left the fact sent, not consumed, and no new
        # native command was dispatched.
        event = bridge._work_store.state("Save1").outbox[0]
        assert event["delivery"]["state"] == "sent"
        assert bridge._work_store.pending_outbox("Save1")
        assert client.dispatched == []

        # The next boundary redelivers the same fact.
        bridge._maybe_deliver_outbox("Save1")
        await bridge.wait_for_deliveries()
        event = bridge._work_store.state("Save1").outbox[0]
        assert event["delivery"]["state"] == "consumed"
        assert len(prompts) == 2
        assert facts_payload(prompts[0]) == facts_payload(prompts[1])
        final = ws.replies()[-1]
        assert final["replyText"].startswith("补报：胡萝卜种好了")
        assert final["commandId"] == "req-1"
        assert client.dispatched == []

    asyncio.run(run())


def test_duplicate_terminal_with_same_command_id_delivers_once(tmp_path, monkeypatch):
    client = FakeClient()
    bridge = make_bridge(tmp_path, monkeypatch, client)
    ws = FakeWs()
    bridge._chat_ws = ws
    prime_player_job(bridge)
    bridge._execute_turn = MagicMock(return_value={
        "success": True, "response": "胡萝卜种好了。", "conversation_id": "sess-1"})

    async def run():
        execution = terminal_execution()
        await bridge._on_job_terminal("Save1", execution, "SHORT_JOB_TERMINAL")
        await bridge._on_job_terminal("Save1", execution, "SHORT_JOB_TERMINAL")
        await bridge.wait_for_deliveries()

        assert bridge._execute_turn.call_count == 1
        outbox = bridge._work_store.state("Save1").outbox
        assert len(outbox) == 1
        assert outbox[0]["delivery"]["state"] == "consumed"

    asyncio.run(run())


def test_late_terminal_attributes_to_its_own_request_not_the_active_one(tmp_path, monkeypatch):
    client = FakeClient()
    bridge = make_bridge(tmp_path, monkeypatch, client)
    ws = FakeWs()
    bridge._chat_ws = ws
    prime_player_job(bridge, request_id="req-1", text="先清杂物")

    # A newer player request has since become the active root.
    bridge._work_store.record_request("Save1", "req-2", "再看看鸡群", origin="player")
    bridge._active_player_requests["Save1"] = "req-2"

    prompts = []
    bridge._execute_turn = MagicMock(side_effect=lambda task, cid, prompt: (
        prompts.append(prompt), {"success": True, "response": "已汇报。", "conversation_id": "sess-1"})[1])

    async def run():
        # The late terminal of t1 lands after req-2 became active; it must
        # still be attributed to the request that owns t1.
        await bridge._on_job_terminal("Save1", terminal_execution(), "SHORT_JOB_TERMINAL")
        await bridge.wait_for_deliveries()

        event = bridge._work_store.state("Save1").outbox[0]
        assert event["requestId"] == "req-1"
        assert facts_payload(prompts[0])["terminalFacts"][0]["requestText"] == "先清杂物"
        final = ws.replies()[-1]
        assert final["commandId"] == "req-1"
        assert bridge._active_player_requests["Save1"] == "req-2"

    asyncio.run(run())


def test_unknown_fact_followed_by_confirmed_receipt_redelivers_as_correction(tmp_path, monkeypatch):
    client = FakeClient()
    bridge = make_bridge(tmp_path, monkeypatch, client)
    ws = FakeWs()
    bridge._chat_ws = ws
    prime_player_job(bridge)

    prompts = []
    bridge._execute_turn = MagicMock(side_effect=lambda task, cid, prompt: (
        prompts.append(prompt), {"success": True, "response": "如实汇报。", "conversation_id": "sess-1"})[1])

    async def run():
        await bridge._on_job_terminal("Save1", terminal_execution(
            outcome="unknown", reason_code="NATIVE_TERMINAL_UNCONFIRMED", effects=[]), "SHORT_JOB_TERMINAL")
        await bridge.wait_for_deliveries()
        event = bridge._work_store.state("Save1").outbox[0]
        assert event["outcome"] == "unknown"
        assert event["delivery"]["state"] == "consumed"

        # The confirmed receipt for the same command id corrects the fact: it
        # is stored as a new correction entry that re-enters the outbox as
        # pending, while the consumed unknown fact stays in the ledger.
        bridge._work_store.append_outbox_event("Save1", {
            "kind": "terminal", "commandId": COMMAND_ID, "taskId": "t1",
            "outcome": "completed", "effects": [{"state": "hoed"}],
            "feedback": {"progressSummary": "已开垦 1 格"}, "requestId": "req-1",
        })
        outbox = bridge._work_store.state("Save1").outbox
        assert len(outbox) == 2
        assert outbox[0]["outcome"] == "unknown"
        assert outbox[0]["delivery"]["state"] == "consumed"
        correction = outbox[1]
        assert correction["kind"] == "correction"
        assert correction["outcome"] == "completed"
        assert correction["delivery"]["state"] == "pending"

        bridge._maybe_deliver_outbox("Save1")
        await bridge.wait_for_deliveries()
        correction = bridge._work_store.state("Save1").outbox[1]
        assert correction["delivery"]["state"] == "consumed"
        assert len(prompts) == 2
        correction_fact = facts_payload(prompts[1])["terminalFacts"][0]
        assert correction_fact["kind"] == "correction"
        assert correction_fact["outcome"] == "completed"

    asyncio.run(run())


def test_pending_event_survives_restart_and_redelivers_facts_only(tmp_path, monkeypatch):
    client = FakeClient()
    bridge = make_bridge(tmp_path, monkeypatch, client)
    prime_player_job(bridge)
    # No chat channel: the boundary delivery task exits before the facts are
    # marked sent, so the event is still fully pending at the crash.

    async def run():
        await bridge._on_job_terminal("Save1", terminal_execution(), "SHORT_JOB_TERMINAL")
        # Crash window 1: the event is still pending (never sent).
        event = bridge._work_store.state("Save1").outbox[0]
        assert event["delivery"]["state"] == "pending"
    asyncio.run(run())

    restarted_client = FakeClient()
    restarted = make_bridge(tmp_path, monkeypatch, restarted_client)
    ws = FakeWs()
    restarted._chat_ws = ws
    prompts = []
    restarted._execute_turn = MagicMock(side_effect=lambda task, cid, prompt: (
        prompts.append(prompt), {"success": True, "response": "重投：胡萝卜种好了。", "conversation_id": "sess-2"})[1])

    async def run_restart():
        pending = restarted._work_store.pending_outbox("Save1")
        assert len(pending) == 1 and pending[0]["delivery"]["state"] == "pending"
        restarted._maybe_deliver_outbox("Save1")
        await restarted.wait_for_deliveries()

        assert facts_payload(prompts[0])["terminalFacts"][0]["requestText"] == "种植胡萝卜"
        event = restarted._work_store.state("Save1").outbox[0]
        assert event["delivery"]["state"] == "consumed"
        final = ws.replies()[-1]
        assert final["commandId"] == "req-1"
        # Redelivery restates facts only; no native command is re-dispatched.
        assert restarted_client.dispatched == []

    asyncio.run(run_restart())


def test_sent_unconfirmed_event_survives_restart_and_is_redelivered_once(tmp_path, monkeypatch):
    client = FakeClient()
    bridge = make_bridge(tmp_path, monkeypatch, client)
    prime_player_job(bridge)
    bridge._chat_ws = FakeWs()

    async def run():
        await bridge._on_job_terminal("Save1", terminal_execution(), "SHORT_JOB_TERMINAL")
        # Crash window 2: the delivery turn died mid-flight right after the
        # facts were marked sent, before the reply was confirmed.
        event = bridge._work_store.state("Save1").outbox[0]
        bridge._work_store.mark_outbox_sent("Save1", [event["id"]])
    asyncio.run(run())

    restarted_client = FakeClient()
    restarted = make_bridge(tmp_path, monkeypatch, restarted_client)
    ws = FakeWs()
    restarted._chat_ws = ws
    prompts = []
    restarted._execute_turn = MagicMock(side_effect=lambda task, cid, prompt: (
        prompts.append(prompt), {"success": True, "response": "重投：胡萝卜种好了。", "conversation_id": "sess-2"})[1])

    async def run_restart():
        pending = restarted._work_store.pending_outbox("Save1")
        assert len(pending) == 1 and pending[0]["delivery"]["state"] == "sent"
        restarted._maybe_deliver_outbox("Save1")
        await restarted.wait_for_deliveries()

        assert len(prompts) == 1
        assert facts_payload(prompts[0])["terminalFacts"][0]["outcome"] == "completed"
        event = restarted._work_store.state("Save1").outbox[0]
        assert event["delivery"]["state"] == "consumed"
        replies = ws.replies()
        delivery_request_ids = {r["requestId"] for r in replies if r["requestId"].startswith("delivery-")}
        assert len(delivery_request_ids) == 1
        assert restarted_client.dispatched == []

    asyncio.run(run_restart())


def test_consumed_event_survives_restart_without_redelivery(tmp_path, monkeypatch):
    client = FakeClient()
    bridge = make_bridge(tmp_path, monkeypatch, client)
    prime_player_job(bridge)
    bridge._chat_ws = FakeWs()
    bridge._execute_turn = MagicMock(return_value={
        "success": True, "response": "胡萝卜种好了。", "conversation_id": "sess-1"})

    async def run():
        await bridge._on_job_terminal("Save1", terminal_execution(), "SHORT_JOB_TERMINAL")
        await bridge.wait_for_deliveries()
        assert not bridge._work_store.pending_outbox("Save1")
    asyncio.run(run())

    restarted = make_bridge(tmp_path, monkeypatch, FakeClient())
    restarted._chat_ws = FakeWs()
    restarted._execute_turn = MagicMock(return_value={"success": True, "response": "不应被调用。"})

    async def run_restart():
        assert not restarted._work_store.pending_outbox("Save1")
        restarted._maybe_deliver_outbox("Save1")
        await restarted.wait_for_deliveries()
        assert restarted._execute_turn.call_count == 0
        assert restarted._delivery_tasks == {}

    asyncio.run(run_restart())
