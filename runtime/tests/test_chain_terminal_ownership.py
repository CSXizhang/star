"""Regression tests: a stale job terminal must not advance a new command chain.

Defect: ``ChatBridge._on_job_terminal`` looked up the command chain by
``save_id`` only, without checking that the arriving ``StepExecution`` belongs
to the task/decision the chain is currently waiting on.  A late terminal from
a revoked previous job therefore fired an extra ``chain-*`` model turn for the
player's new instruction.

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
    """Fake provider selecting one scripted task per turn."""

    def __init__(self, bridge: ChatBridge, task_ids: list[str]):
        self.bridge = bridge
        self.task_ids = task_ids
        self.turns = 0
        self.request_ids: list[str] = []

    def run(self, active_task, cid, prompt):
        self.turns += 1
        self.request_ids.append(active_task.request_id)
        token = os.environ.get("STARDEW_DECISION_TOKEN")
        task_id = f"{self.task_ids[min(self.turns - 1, len(self.task_ids) - 1)]}-turn{self.turns}"
        goal = self.bridge._work_store.add_goal("Save1", f"作业{self.turns}", source="user")
        self.bridge._work_store.submit_plan(
            "Save1",
            goal_id=goal.id,
            decision_token=token,
            tasks=[
                {
                    "id": task_id,
                    "title": f"作业{self.turns}",
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


def test_stale_terminal_does_not_advance_new_instruction_chain(tmp_path, monkeypatch):
    """旧作业终态晚于新玩家指令到达：不启动新链、不抢占新决策、旧效果仍入账。"""
    client = _BlockingClient()

    async def scenario():
        backend = _SelectingBackend(None, ["t-old", "t-new", "t-chain"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()

        # Turn 1: player instruction selects task t-old; chain registered.
        await bridge.handle_chat_submit(ws, "req-1", "清杂物", "Save1")
        assert backend.turns == 1
        assert "Save1" in bridge._command_chains

        # Old dispatch goes out but the native side holds the result.
        client.hold_dispatch = True
        eval_task = asyncio.create_task(bridge._plan_worker.evaluate())
        await asyncio.wait_for(client.dispatch_started.wait(), timeout=2)

        # Player interrupts with a new instruction: old decision revoked, new
        # chain registered, turn 2 selects t-new.
        await bridge.handle_chat_submit(ws, "req-2", "继续清理", "Save1")
        assert backend.turns == 2
        decision = bridge._work_store.state("Save1").decision
        assert decision.get("taskId") == "t-new-turn2"

        # The old dispatch finally returns; its terminal must settle to history
        # but must NOT advance the new instruction's chain.
        client.release.set()
        await asyncio.wait_for(eval_task, timeout=2)
        await bridge.wait_for_chains()

        assert backend.turns == 2, (
            f"stale terminal of t-old fired a chain turn for the new instruction: {backend.request_ids}"
        )
        assert not any(rid.startswith("chain-") for rid in backend.request_ids)
        decision = bridge._work_store.state("Save1").decision
        assert decision.get("taskId") == "t-new-turn2" and not decision.get("finished"), (
            "stale terminal must not preempt or overwrite the new decision"
        )
        old_task = next(t for t in bridge._work_store.state("Save1").tasks if t.id == "t-old-turn1")
        assert old_task.status == "completed", "old job's real outcome must still settle into task history"
        assert any(step.effects for step in old_task.steps), "old job's real effects must be recorded"

        # The new task's own terminal advances the chain exactly once.
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert backend.turns == 3
        assert backend.request_ids[2].startswith("chain-")

    asyncio.run(scenario())


def test_duplicate_terminal_advances_chain_once(tmp_path, monkeypatch):
    """所属终态推进一次；同一终态重复到达不得再次推进。"""
    client = _BlockingClient()

    async def scenario():
        backend = _SelectingBackend(None, ["t-1", "t-2"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()

        await bridge.handle_chat_submit(ws, "req-1", "清理", "Save1")
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert backend.turns == 2, "owned terminal must advance the chain once"
        assert backend.request_ids[1].startswith("chain-")

        # Replay the same terminal: no extra advance.
        execution = StepExecution(status="executed", task_id="t-1-turn1", outcome="completed", task_status="completed")
        await bridge._on_job_terminal("Save1", execution, "SHORT_JOB_TERMINAL")
        await bridge.wait_for_chains()
        assert backend.turns == 2, f"duplicate terminal advanced the chain again: {backend.request_ids}"

    asyncio.run(scenario())


def test_paused_state_still_blocks_chain_on_stale_terminal(tmp_path, monkeypatch):
    """A terminal received while paused is retained for continuation on resume."""
    client = _BlockingClient()

    async def scenario():
        backend = _SelectingBackend(None, ["t-1", "t-2"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()

        await bridge.handle_chat_submit(ws, "req-1", "清理", "Save1")
        client.hold_dispatch = True
        eval_task = asyncio.create_task(bridge._plan_worker.evaluate())
        await asyncio.wait_for(client.dispatch_started.wait(), timeout=2)

        bridge._work_store.set_paused("Save1", True)
        client.release.set()
        await asyncio.wait_for(eval_task, timeout=2)
        await bridge.wait_for_chains()

        assert backend.turns == 1, "paused save must not run a chain turn"
        assert bridge._command_chains["Save1"].pending_continuation is True

    asyncio.run(scenario())


def test_root_command_reply_survives_job_and_final_continuation(tmp_path, monkeypatch):
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
        await bridge.handle_chat_submit(ws, "root-1", "清理", "Save1")
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        replies = [json.loads(call.args[0])["payload"] for call in ws.send_text.call_args_list
                   if json.loads(call.args[0]).get("messageType") == "chat.reply"]
        assert backend.turns == 2
        assert all(reply["commandId"] == "root-1" for reply in replies)
        assert any(reply["status"] == "job-completed" and reply["commandComplete"] is False for reply in replies)
        assert replies[-1]["requestId"].startswith("chain-")
        assert replies[-1]["commandComplete"] is True

    asyncio.run(scenario())


def test_resume_advances_terminal_recorded_during_pause(tmp_path, monkeypatch):
    client = _BlockingClient()

    async def scenario():
        class OneJobBackend(_SelectingBackend):
            def run(self, active_task, cid, prompt):
                if self.turns:
                    self.turns += 1
                    self.request_ids.append(active_task.request_id)
                    return {"success": True, "response": "已完成"}
                return super().run(active_task, cid, prompt)

        backend = OneJobBackend(None, ["t-1"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()
        await bridge.handle_chat_submit(ws, "root-1", "清理", "Save1")
        client.hold_dispatch = True
        evaluation = asyncio.create_task(bridge._plan_worker.evaluate())
        await asyncio.wait_for(client.dispatch_started.wait(), timeout=2)
        pause = Envelope.create_autonomy_control(bridge.instance_id, "pause-1", "Save1", "pause")
        await bridge._handle_autonomy_control(ws, pause, "Save1")
        client.release.set()
        await asyncio.wait_for(evaluation, timeout=2)
        assert backend.turns == 1
        assert bridge._command_chains["Save1"].pending_continuation
        resume = Envelope.create_autonomy_control(bridge.instance_id, "resume-1", "Save1", "resume")
        await bridge._handle_autonomy_control(ws, resume, "Save1")
        await bridge.wait_for_chains()
        assert backend.turns == 2
        assert client.dispatched.count("clear_debris") == 1

    asyncio.run(scenario())


def test_stale_cancel_command_id_cannot_cancel_new_root(tmp_path, monkeypatch):
    client = _BlockingClient()

    async def scenario():
        backend = _SelectingBackend(None, ["old", "new"])  # type: ignore[arg-type]
        bridge = _bridge(tmp_path, monkeypatch, client, backend)
        backend.bridge = bridge
        ws = AsyncMock()
        await bridge.handle_chat_submit(ws, "root-old", "旧指令", "Save1")
        await bridge.handle_chat_submit(ws, "root-new", "新指令", "Save1")
        assert bridge._command_chains["Save1"].root_request_id == "root-new"
        for action in ("pause", "resume", "cancel"):
            control = Envelope.create_autonomy_control(
                bridge.instance_id, f"{action}-old", "Save1", action, commandId="root-old"
            )
            await bridge._handle_autonomy_control(ws, control, "Save1")
            assert bridge._command_chains["Save1"].root_request_id == "root-new"
            assert bridge._work_store.state("Save1").decision.get("selected") is True
            assert bridge._work_store.state("Save1").paused is False
            state_reply = json.loads(ws.send_text.call_args_list[-1].args[0])["payload"]
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
        assert json.loads(ws.send_text.call_args_list[-1].args[0])["payload"]["status"] == "confirmed"

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
        await bridge.handle_chat_submit(ws, "root-1", "清理", "Save1")
        claim = bridge._work_store.claim_next_step("Save1", "worker")
        bridge._work_store.assign_command_id("Save1", claim["taskId"], claim["stepId"], "native-1")

        control = Envelope.create_autonomy_control(
            bridge.instance_id, "cancel-1", "Save1", "cancel", commandId="root-1"
        )
        await bridge._handle_autonomy_control(ws, control, "Save1")
        replies = [json.loads(call.args[0])["payload"] for call in ws.send_text.call_args_list]
        assert replies[-1]["status"] == "rejected"
        assert not any(reply.get("commandComplete") is True for reply in replies)
        assert bridge._pending_native_cancels["Save1"] == ("root-1", "native-1")

        client.terminal = "cancelled"
        retry = Envelope.create_autonomy_control(
            bridge.instance_id, "cancel-2", "Save1", "cancel", commandId="root-1"
        )
        await bridge._handle_autonomy_control(ws, retry, "Save1")
        replies = [json.loads(call.args[0])["payload"] for call in ws.send_text.call_args_list]
        assert replies[-1]["status"] == "confirmed"
        assert any(reply.get("status") == "cancelled" and reply.get("commandComplete") is True for reply in replies)
        assert "Save1" not in bridge._pending_native_cancels

    asyncio.run(scenario())
