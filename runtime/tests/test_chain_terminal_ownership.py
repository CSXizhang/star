"""Regression tests: a stale job terminal must not pollute a new player request.

Defect history: ``ChatBridge._on_job_terminal`` once looked up ownership by
``save_id`` only, so a late terminal from a revoked previous job fired an
extra ``chain-*`` model turn attributed to the player's new instruction.  The
persistent outbox now attributes every confirmed terminal to the owning
request (``bind_request_task``/``find_request_for_task``) and dedupes by the
stable plan command id, so a late terminal settles to history under its own
request and a duplicate terminal never delivers the fact twice.

Scenarios here drive the real PlanWorker -> PlanExecutor -> WorkStore path
(a blocking fake client holds the old dispatch while the player submits a new
instruction), not a hand-invoked callback — except the duplicate-terminal
guard, which calls the callback directly to lock the consume-once invariant.

Fake clients/backends/WebSockets and isolated ``tmp_path`` run dirs only; no
real provider process, save file, or game is touched.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.plan_executor import StepExecution
from stardew_ai_runtime.protocol import Envelope


class _BlockingClient:
    """Internal plan client that can hold one native dispatch in flight."""

    def __init__(self, save_id: str = "Save1"):
        self.save_id = save_id
        self.dispatched: list[str] = []
        self.hold_dispatch = False
        self.dispatch_started = asyncio.Event()
        self.release = asyncio.Event()

    async def current_save_id(self):
        return self.save_id

    async def call_tool(self, name: str, params: dict):
        if name == "dispatch_plan_operation":
            operation = params.get("operation")
            self.dispatched.append(operation)
            if operation == "cancel_task":
                return {"status": "completed"}
            if self.hold_dispatch:
                self.hold_dispatch = False
                self.dispatch_started.set()
                await self.release.wait()
            return {"status": "completed", "effects": [{"kind": "done"}]}
        return {"status": "completed"}

    async def close(self):
        pass


class _SelectingBackend:
    """Fake provider selecting one scripted task per decision turn.

    Read-only delivery turns carry no decision token; the backend then only
    acknowledges the facts, exactly like a real model on the life surface.
    Selection stops once every scripted task id has been used.
    """

    def __init__(self, bridge: ChatBridge, task_ids: list[str]):
        self.bridge = bridge
        self.task_ids = task_ids
        self.turns = 0
        self.selections = 0
        self.request_ids: list[str] = []

    def run(self, active_task, cid, prompt):
        self.turns += 1
        self.request_ids.append(active_task.request_id)
        token = os.environ.get("STARDEW_DECISION_TOKEN")
        if not token or self.selections >= len(self.task_ids):
            return {"success": True, "response": "事实已收到，已如实汇报。"}
        task_id = f"{self.task_ids[self.selections]}-turn{self.turns}"
        self.selections += 1
        goal = self.bridge._work_store.add_goal("Save1", f"作业{self.selections}", source="user")
        self.bridge._work_store.submit_plan(
            "Save1",
            goal_id=goal.id,
            decision_token=token,
            request_id=(active_task.work_request_ids[-1] if active_task.work_request_ids else None),
            tasks=[
                {
                    "id": task_id,
                    "title": f"作业{self.selections}",
                    "steps": [{"id": f"s-{task_id}", "operation": "clear_debris", "params": {}}],
                }
            ],
        )
        return {"success": True, "response": f"已选择{task_id}"}


def _bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: _BlockingClient, backend) -> ChatBridge:
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
    bridge._backend = backend
    return bridge


def test_stale_terminal_stays_attributed_to_superseded_request(tmp_path, monkeypatch):
    """旧作业终态晚于新玩家指令到达：事实归原请求账，中途原文随后由投递回合处理。"""
    client = _BlockingClient()

    async def scenario():
        backend = _SelectingBackend(None, ["t-old", "t-new"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()
        bridge._chat_ws = ws

        # Turn 1: player instruction selects task t-old; bound to req-1.
        await bridge.handle_chat_submit(ws, "req-1", "清杂物", "Save1")
        assert backend.turns == 1

        # Old dispatch goes out but the native side holds the result.
        client.hold_dispatch = True
        eval_task = asyncio.create_task(bridge._plan_worker.evaluate())
        await asyncio.wait_for(client.dispatch_started.wait(), timeout=2)

        # A new player instruction while the job owns the execution connection
        # is recorded verbatim (player-midflight) and answered read-only: it
        # must not preempt the in-flight native job.
        await bridge.handle_chat_submit(ws, "req-2", "继续清理", "Save1")
        assert backend.turns == 2
        midflight = bridge._work_store.pending_midflight_requests("Save1")
        assert [m["id"] for m in midflight] == ["req-2"]
        decision = bridge._work_store.state("Save1").decision
        assert decision.get("taskId") == "t-old-turn1"

        # The held dispatch finally returns; its terminal settles to history
        # as req-1's outbox fact and the boundary delivery turn also picks up
        # the unconsumed midflight request.
        client.release.set()
        await asyncio.wait_for(eval_task, timeout=2)
        await bridge.wait_for_deliveries()

        outbox = bridge._work_store.state("Save1").outbox
        assert [e["requestId"] for e in outbox] == ["req-1"]
        assert backend.turns == 3, (
            f"stale terminal of t-old must be delivered once for req-1: {backend.request_ids}"
        )
        assert backend.request_ids[2].startswith("delivery-")
        assert not bridge._work_store.pending_midflight_requests("Save1")
        old_task = next(t for t in bridge._work_store.state("Save1").tasks if t.id == "t-old-turn1")
        assert old_task.status == "completed", "old job's real outcome must still settle into task history"
        assert any(step.effects for step in old_task.steps), "old job's real effects must be recorded"
        decision = bridge._work_store.state("Save1").decision
        assert decision.get("taskId") == "t-new-turn3" and not decision.get("finished")
        assert bridge._work_store.find_request_for_task("Save1", "t-new-turn3")["id"] == "req-2"

        # The task the midflight request asked for reaches its own terminal
        # and is delivered exactly once, attributed to req-2.
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_deliveries()
        assert backend.turns == 4
        assert backend.request_ids[3].startswith("delivery-")
        outbox = bridge._work_store.state("Save1").outbox
        assert [e["requestId"] for e in outbox] == ["req-1", "req-2"]
        assert all((e.get("delivery") or {}).get("state") == "consumed" for e in outbox)

    asyncio.run(scenario())


def test_duplicate_terminal_delivers_fact_once(tmp_path, monkeypatch):
    """所属终态投递一次；同一终态重复到达不得再次投递事实。"""
    client = _BlockingClient()

    async def scenario():
        backend = _SelectingBackend(None, ["t-1", "t-2"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()
        bridge._chat_ws = ws

        await bridge.handle_chat_submit(ws, "req-1", "清理", "Save1")
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_deliveries()
        assert backend.turns == 2, "owned terminal must be delivered exactly once"
        assert backend.request_ids[1].startswith("delivery-")
        command_id = bridge._work_store.state("Save1").executions[-1].command_id

        # Replay the same terminal with its stable plan command id: the
        # consumed fact is returned unchanged and no new turn is scheduled.
        execution = StepExecution(status="executed", task_id="t-1-turn1", command_id=command_id,
                                  outcome="completed", task_status="completed")
        await bridge._on_job_terminal("Save1", execution, "SHORT_JOB_TERMINAL")
        await bridge.wait_for_deliveries()
        assert backend.turns == 2, f"duplicate terminal delivered the fact again: {backend.request_ids}"
        outbox = bridge._work_store.state("Save1").outbox
        assert len(outbox) == 1
        assert (outbox[0].get("delivery") or {}).get("state") == "consumed"

    asyncio.run(scenario())


def test_terminal_during_pause_is_delivered_read_only_without_new_dispatch(tmp_path, monkeypatch):
    """暂停期间落定的终态以只读投递送达：事实被消费，但不派生新的原生作业。"""
    client = _BlockingClient()

    async def scenario():
        class OneJobBackend(_SelectingBackend):
            def run(self, active_task, cid, prompt):
                if self.turns:
                    self.turns += 1
                    self.request_ids.append(active_task.request_id)
                    return {"success": True, "response": "已经做完"}
                return super().run(active_task, cid, prompt)

        backend = OneJobBackend(None, ["t-1"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()
        bridge._chat_ws = ws
        await bridge.handle_chat_submit(ws, "root-1", "清理", "Save1")
        client.hold_dispatch = True
        evaluation = asyncio.create_task(bridge._plan_worker.evaluate())
        await asyncio.wait_for(client.dispatch_started.wait(), timeout=2)
        pause = Envelope.create_autonomy_control(bridge.instance_id, "pause-1", "Save1", "pause")
        await bridge._handle_autonomy_control(ws, pause, "Save1")
        client.release.set()
        await asyncio.wait_for(evaluation, timeout=2)
        await bridge.wait_for_deliveries()

        # The terminal that landed while paused is delivered at the next
        # boundary as a read-only turn: the fact is consumed and pause is kept.
        assert backend.turns == 2
        assert client.dispatched.count("clear_debris") == 1
        assert not bridge._work_store.pending_outbox("Save1")
        assert bridge._work_store.state("Save1").paused is True

        # Resuming with an empty outbox must not spend another model turn.
        resume = Envelope.create_autonomy_control(bridge.instance_id, "resume-1", "Save1", "resume")
        await bridge._handle_autonomy_control(ws, resume, "Save1")
        await bridge.wait_for_deliveries()
        assert backend.turns == 2

    asyncio.run(scenario())


def test_root_command_reply_survives_job_and_final_delivery(tmp_path, monkeypatch):
    client = _BlockingClient()

    async def scenario():
        class OneJobBackend(_SelectingBackend):
            def run(self, active_task, cid, prompt):
                if self.turns:
                    self.turns += 1
                    self.request_ids.append(active_task.request_id)
                    return {"success": True, "response": "已经做完"}
                return super().run(active_task, cid, prompt)

        backend = OneJobBackend(None, ["t-1"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()
        bridge._chat_ws = ws
        await bridge.handle_chat_submit(ws, "root-1", "清理", "Save1")
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_deliveries()
        replies = [json.loads(call.args[0])["payload"] for call in ws.send_text.call_args_list
                   if json.loads(call.args[0]).get("messageType") == "chat.reply"]
        assert backend.turns == 2
        # Every reply — the progress receipts and the final delivery — stays
        # on the player's root command id.
        assert all(reply["commandId"] == "root-1" for reply in replies)
        assert any(reply["status"] == "job-completed" and reply["commandComplete"] is False for reply in replies)
        final = replies[-1]
        assert final["requestId"].startswith("delivery-")
        assert final["commandComplete"] is True
        assert final["replyText"].startswith("已经做完")

    asyncio.run(scenario())


def test_stale_cancel_command_id_cannot_cancel_new_root(tmp_path, monkeypatch):
    client = _BlockingClient()

    async def scenario():
        class PlayerSelectBackend(_SelectingBackend):
            """Select only on real player turns; delivery turns just report."""

            def run(self, active_task, cid, prompt):
                if active_task.request_id.startswith("delivery-"):
                    self.turns += 1
                    self.request_ids.append(active_task.request_id)
                    return {"success": True, "response": "事实已收到，已如实汇报。"}
                return super().run(active_task, cid, prompt)

        backend = PlayerSelectBackend(None, ["old", "new"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()
        bridge._chat_ws = ws
        await bridge.handle_chat_submit(ws, "root-old", "旧指令", "Save1")
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_deliveries()
        assert backend.turns == 2
        await bridge.handle_chat_submit(ws, "root-new", "新指令", "Save1")
        assert backend.turns == 3
        assert bridge._active_player_requests["Save1"] == "root-new"
        for action in ("pause", "resume", "cancel"):
            control = Envelope.create_autonomy_control(
                bridge.instance_id, f"{action}-old", "Save1", action, commandId="root-old"
            )
            await bridge._handle_autonomy_control(ws, control, "Save1")
            assert bridge._active_player_requests["Save1"] == "root-new"
            assert bridge._work_store.state("Save1").decision.get("selected") is True
            assert bridge._work_store.state("Save1").paused is False
            state_reply = next(
                json.loads(call.args[0])["payload"]
                for call in reversed(ws.send_text.call_args_list)
                if json.loads(call.args[0])["messageType"] == "autonomy.state"
            )
            assert state_reply["status"] == "rejected"

    asyncio.run(scenario())


def test_cancel_waits_for_old_turn_cleanup_before_ack(tmp_path, monkeypatch):
    client = _BlockingClient()

    async def scenario():
        bridge = _bridge(tmp_path, monkeypatch, client, None)
        ws = AsyncMock()
        cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()

        async def old_turn():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await finish_cleanup.wait()

        old = asyncio.create_task(old_turn())
        await asyncio.sleep(0)
        bridge._active_task = ActiveChatTask("root-1", "Save1", command_id="root-1", async_task=old)
        control = Envelope.create_autonomy_control(
            bridge.instance_id, "cancel-1", "Save1", "cancel", commandId="root-1"
        )
        cancelling = asyncio.create_task(bridge._handle_autonomy_control(ws, control, "Save1"))
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        assert not cancelling.done()
        assert ws.send_text.call_count == 0
        finish_cleanup.set()
        await asyncio.wait_for(cancelling, timeout=2)
        assert old.done()
        ack = next(
            json.loads(call.args[0])["payload"]
            for call in reversed(ws.send_text.call_args_list)
            if json.loads(call.args[0])["messageType"] == "autonomy.state"
        )
        assert ack["status"] == "confirmed"

    asyncio.run(scenario())


def test_local_native_completion_confirms_cancel_after_no_active(tmp_path, monkeypatch):
    class NoActiveClient(_BlockingClient):
        terminal = None

        async def call_tool(self, name, params):
            if name == "dispatch_plan_operation" and params.get("operation") == "cancel_task":
                raise RuntimeError("No active task")
            return await super().call_tool(name, params)

        async def reconcile(self, command_id):
            return {"found": True, "native": {"terminalState": self.terminal}} if self.terminal else {"found": False}

    async def scenario():
        client = NoActiveClient()
        backend = _SelectingBackend(None, ["t-1"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()
        bridge._chat_ws = ws
        await bridge.handle_chat_submit(ws, "root-1", "清理", "Save1")
        claim = bridge._work_store.claim_next_step("Save1", "worker")
        bridge._work_store.assign_command_id("Save1", claim["taskId"], claim["stepId"], "native-1")

        control = Envelope.create_autonomy_control(
            bridge.instance_id, "cancel-1", "Save1", "cancel", commandId="root-1"
        )
        await bridge._handle_autonomy_control(ws, control, "Save1")
        envelopes = [json.loads(call.args[0]) for call in ws.send_text.call_args_list]
        acks = [e["payload"] for e in envelopes if e["messageType"] == "autonomy.state"]
        replies = [e["payload"] for e in envelopes]
        assert acks[-1]["status"] == "rejected"
        assert not any(reply.get("commandComplete") is True for reply in replies)
        assert bridge._pending_native_cancels["Save1"] == ("root-1", "native-1")

        client.terminal = "cancelled"
        retry = Envelope.create_autonomy_control(
            bridge.instance_id, "cancel-2", "Save1", "cancel", commandId="root-1"
        )
        await bridge._handle_autonomy_control(ws, retry, "Save1")
        envelopes = [json.loads(call.args[0]) for call in ws.send_text.call_args_list]
        acks = [e["payload"] for e in envelopes if e["messageType"] == "autonomy.state"]
        replies = [e["payload"] for e in envelopes]
        assert acks[-1]["status"] == "confirmed"
        assert any(reply.get("status") == "cancelled" and reply.get("commandComplete") is True for reply in replies)
        assert "Save1" not in bridge._pending_native_cancels

    asyncio.run(scenario())
