"""Regression tests for chat-bridge autonomy settlement and command-chain teardown.

Two confirmed defects:

* The autonomy turn-final settlement fell back to ``job_state.last_job``
  without checking that the stored job feedback belongs to the *current*
  decision, so a stale result from the previous decision settled (or masked)
  the current one.  See ``ChatBridge.handle_chat_submit``'s
  ``autonomy_fingerprint`` branch.

* ``ChatBridge._break_command_chain`` cancelled every task in
  ``_chain_tasks`` — including the chain-continuation task when it broke its
  own chain at end of turn, which dropped the final reply (the next real
  await raised ``CancelledError``) and mis-recorded the turn as
  ``interrupted``.

All tests use fake backends/clients/WebSockets and isolated ``tmp_path``
run dirs; no real provider process, save file, or game is touched.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.plan_executor import StepExecution


class _ScriptedClient:
    """Internal plan client whose next native dispatch outcome is scripted."""

    def __init__(self, save_id: str = "Save1"):
        self.save_id = save_id
        self.dispatched: list[str] = []
        self.fail_dispatch = False

    async def current_save_id(self):
        return self.save_id

    async def call_tool(self, name: str, params: dict):
        if name == "dispatch_plan_operation":
            self.dispatched.append(params.get("operation"))
            if self.fail_dispatch:
                return {"status": "failed", "reasonCode": "OUT_OF_ENERGY", "message": "体力不足，作业失败"}
            return {"status": "completed", "effects": [{"kind": "watered", "tile": [1, 2]}]}
        return {"status": "completed"}

    async def close(self):
        pass


class _SelectingBackend:
    """Fake provider that selects one short job per turn (like a real decision)."""

    def __init__(self, bridge: ChatBridge):
        self.bridge = bridge
        self.turns = 0

    def run(self, active_task, cid, prompt):
        self.turns += 1
        token = os.environ.get("STARDEW_DECISION_TOKEN")
        goal = self.bridge._work_store.add_goal("Save1", "自主工作", source="agent")
        self.bridge._work_store.submit_plan(
            "Save1",
            goal_id=goal.id,
            decision_token=token,
            tasks=[
                {
                    "id": f"auto-task-{self.turns}",
                    "title": f"自主作业{self.turns}",
                    "steps": [{"id": f"auto-step-{self.turns}", "operation": "water_auto", "params": {}}],
                }
            ],
        )
        return {"success": True, "response": f"已选择自主作业{self.turns}"}


class _YieldingWs:
    """WebSocket double whose send truly yields control.

    ``AsyncMock.send_text`` returns without ever suspending, so a pending
    ``Task.cancel()`` never injects ``CancelledError`` and the production bug
    (lost final reply) stays invisible.  ``await asyncio.sleep(0)`` makes the
    send a real suspension point, exactly like a live socket write.
    """

    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str):
        await asyncio.sleep(0)
        self.sent.append(text)

    def reply_texts(self) -> list[str]:
        return [json.loads(m).get("payload", {}).get("replyText", "") for m in self.sent]


def _autonomy_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: _ScriptedClient) -> ChatBridge:
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
    bridge._autonomy.set_enabled("Save1", True)
    bridge._backend = _SelectingBackend(bridge)
    return bridge


async def _autonomy_turn(bridge: ChatBridge, ws, index: int) -> str:
    """Run one free-mode autonomy decision turn and return its fingerprint."""
    fingerprint = f"autonomy-fp-{index}"
    assert bridge._autonomy.record_world_event("Save1", fingerprint, revision=index)
    request_id = f"autonomy-{index}"
    bridge._autonomy_requests[request_id] = fingerprint
    await bridge.handle_chat_submit(ws, request_id, "自主规划工作", "Save1")
    return fingerprint


# ---------------------------------------------------------------- defect 1


def test_stale_last_job_success_does_not_mask_new_failure(tmp_path, monkeypatch):
    """旧成功 last_job 不能替当前决策结算；当前作业的失败必须计入 failure_count。"""
    client = _ScriptedClient()
    bridge = _autonomy_bridge(tmp_path, monkeypatch, client)
    ws = AsyncMock()

    async def run():
        await _autonomy_turn(bridge, ws, 1)
        await bridge._plan_worker.evaluate()
        assert bridge._autonomy.state("Save1").failure_count == 0

        client.fail_dispatch = True
        await _autonomy_turn(bridge, ws, 2)
        # 当前任务未到终态、且 last_job 属于上一决策 → 只能走 pending 延迟结算。
        assert bridge._autonomy_pending_task_fingerprints, (
            "current decision must defer to pending fingerprint settlement"
        )
        await bridge._plan_worker.evaluate()
        state = bridge._autonomy.state("Save1")
        assert state.failure_count == 1, "new failure must be counted, not masked by the stale success"

    asyncio.run(run())


def test_stale_last_job_failure_does_not_pollute_new_success(tmp_path, monkeypatch):
    """旧失败 last_job 不能污染当前决策；当前作业成功后 failure_count 必须清零。"""
    client = _ScriptedClient()
    bridge = _autonomy_bridge(tmp_path, monkeypatch, client)
    ws = AsyncMock()

    async def run():
        client.fail_dispatch = True
        await _autonomy_turn(bridge, ws, 1)
        await bridge._plan_worker.evaluate()
        assert bridge._autonomy.state("Save1").failure_count == 1

        client.fail_dispatch = False
        await _autonomy_turn(bridge, ws, 2)
        assert bridge._autonomy_pending_task_fingerprints, (
            "current decision must defer to pending fingerprint settlement"
        )
        await bridge._plan_worker.evaluate()
        state = bridge._autonomy.state("Save1")
        assert state.failure_count == 0, "fresh success must reset the count instead of inheriting the stale failure"

    asyncio.run(run())


def test_three_consecutive_failures_trip_breaker(tmp_path, monkeypatch):
    """先一次成功，再三连失败：failure_count 必须为 3 且熔断触发。"""
    client = _ScriptedClient()
    bridge = _autonomy_bridge(tmp_path, monkeypatch, client)
    ws = AsyncMock()

    async def run():
        await _autonomy_turn(bridge, ws, 1)
        await bridge._plan_worker.evaluate()
        assert bridge._autonomy.state("Save1").failure_count == 0

        client.fail_dispatch = True
        for index in (2, 3, 4):
            await _autonomy_turn(bridge, ws, index)
            await bridge._plan_worker.evaluate()

        state = bridge._autonomy.state("Save1")
        assert state.failure_count == 3, "each of the three real failures must be counted"
        assert state.breaker_tripped is True, "breaker must trip at the configured threshold"
        assert bridge._autonomy.is_cooling_down("Save1")

    asyncio.run(run())


def test_duplicate_terminal_settles_once(tmp_path, monkeypatch):
    """同一终态到达两次只能结算一次（重复终态不得重复计数）。"""
    client = _ScriptedClient()
    bridge = _autonomy_bridge(tmp_path, monkeypatch, client)
    ws = AsyncMock()

    async def run():
        client.fail_dispatch = True
        await _autonomy_turn(bridge, ws, 1)
        await bridge._plan_worker.evaluate()
        assert bridge._autonomy.state("Save1").failure_count == 1
        assert not bridge._autonomy_pending_task_fingerprints, "terminal must consume the pending fingerprint"

        execution = StepExecution(
            status="executed",
            task_id="auto-task-1",
            outcome="partial",
            task_status="partial",
            reason_code="OUT_OF_ENERGY",
        )
        await bridge._on_job_terminal("Save1", execution, "SHORT_JOB_TERMINAL")
        await bridge._on_job_terminal("Save1", execution, "SHORT_JOB_TERMINAL")
        assert bridge._autonomy.state("Save1").failure_count == 1, "duplicate terminal must not double-count"

    asyncio.run(run())


# ---------------------------------------------------------------- defect 2


def test_chain_continuation_delivers_final_reply(tmp_path, monkeypatch):
    """续链正常终止时，最终总结回复必须真正送达（调用者不能被自身取消）。"""
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    client = _ScriptedClient()
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
    ws = _YieldingWs()

    class Backend:
        def __init__(self):
            self.turns = 0

        def run(self, active_task, cid, prompt):
            self.turns += 1
            if self.turns == 1:
                token = os.environ.get("STARDEW_DECISION_TOKEN")
                goal = bridge._work_store.add_goal("Save1", "种植胡萝卜", source="user")
                bridge._work_store.submit_plan(
                    "Save1",
                    goal_id=goal.id,
                    decision_token=token,
                    tasks=[{"id": "t1", "title": "锄地", "steps": [{"id": "s1", "operation": "hoe_tiles", "params": {}}]}],
                )
                return {"success": True, "response": "第一步：锄地已规划"}
            return {"success": True, "response": "胡萝卜已种植完毕"}

    backend = Backend()
    bridge._backend = backend

    async def run():
        await bridge.handle_chat_submit(ws, "req-player-1", "种植胡萝卜", "Save1")
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()

        assert backend.turns == 2
        texts = ws.reply_texts()
        assert any("胡萝卜已种植完毕" in t for t in texts), f"final summary reply missing: {texts}"

        commands_file = tmp_path / "chat_commands.jsonl"
        recorded = [json.loads(line) for line in commands_file.read_text(encoding="utf-8").splitlines()]
        interrupted = [c for c in recorded if c.get("status") == "interrupted" and str(c.get("request_id", "")).startswith("chain-")]
        assert not interrupted, f"completed chain turn mis-recorded as interrupted: {interrupted}"

    asyncio.run(run())


def test_chain_limit_reply_delivered(tmp_path, monkeypatch):
    """续链达到上限时，上限提示回复必须真正送达。"""
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    monkeypatch.setenv("STARDEW_COMMAND_CHAIN_MAX", "1")
    client = _ScriptedClient()
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
    ws = _YieldingWs()

    class Backend:
        def __init__(self):
            self.turns = 0

        def run(self, active_task, cid, prompt):
            self.turns += 1
            token = os.environ.get("STARDEW_DECISION_TOKEN")
            goal = bridge._work_store.add_goal("Save1", "持续开荒", source="user")
            bridge._work_store.submit_plan(
                "Save1",
                goal_id=goal.id,
                decision_token=token,
                tasks=[{"id": f"t{self.turns}", "title": f"步骤{self.turns}", "steps": [{"id": f"s{self.turns}", "operation": "clear_debris", "params": {}}]}],
            )
            return {"success": True, "response": f"已选择步骤{self.turns}"}

    backend = Backend()
    bridge._backend = backend

    async def run():
        await bridge.handle_chat_submit(ws, "req-loop", "开荒", "Save1")
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()

        assert backend.turns == 2
        assert "Save1" not in bridge._command_chains
        texts = ws.reply_texts()
        assert any("上限" in t for t in texts), f"chain-limit reply missing: {texts}"

    asyncio.run(run())


def test_external_cancel_interrupts_pending_chain(tmp_path, monkeypatch):
    """外部取消仍须中断续链：任务被取消，且不会发出续链最终总结。"""
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    client = _ScriptedClient()
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
    ws = _YieldingWs()

    class Backend:
        def __init__(self):
            self.turns = 0

        def run(self, active_task, cid, prompt):
            self.turns += 1
            token = os.environ.get("STARDEW_DECISION_TOKEN")
            goal = bridge._work_store.add_goal("Save1", "任务", source="user")
            bridge._work_store.submit_plan(
                "Save1",
                goal_id=goal.id,
                decision_token=token,
                tasks=[{"id": f"t{self.turns}", "title": "任务", "steps": [{"id": f"s{self.turns}", "operation": "clear_debris", "params": {}}]}],
            )
            return {"success": True, "response": "第一作业已选择" if self.turns == 1 else "续链作业已选择"}

    backend = Backend()
    bridge._backend = backend

    async def run():
        await bridge.handle_chat_submit(ws, "req-1", "任务1", "Save1")
        await bridge._plan_worker.evaluate()
        chain_tasks = list(bridge._chain_tasks)
        assert chain_tasks, "chain continuation must be scheduled after the first job terminal"

        await bridge.handle_chat_cancel(ws, "req-1", "player_cancelled", "Save1")
        # ``_break_command_chain`` clears the live set, so ``wait_for_chains``
        # would no-op; drive the captured tasks to their cancelled end directly.
        await asyncio.gather(*chain_tasks, return_exceptions=True)

        assert all(t.cancelled() for t in chain_tasks), "external cancel must still cancel the chain task"
        assert backend.turns == 1, "cancelled chain must not run another provider turn"
        texts = ws.reply_texts()
        assert not any("续链作业已选择" in t for t in texts), f"cancelled chain leaked a final summary: {texts}"

    asyncio.run(run())
