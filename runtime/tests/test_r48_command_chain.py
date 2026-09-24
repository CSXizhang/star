import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge, CommandChain
from stardew_ai_runtime.plan_executor import StepExecution
from stardew_ai_runtime.protocol import Envelope


class FakeClient:
    def __init__(self, save_id="Save1"):
        self.save_id = save_id
        self.dispatched = []

    async def current_save_id(self):
        return self.save_id

    async def call_tool(self, name: str, params: dict):
        if name == "dispatch_plan_operation":
            self.dispatched.append(params.get("operation"))
            return {"status": "completed", "effects": [{"kind": "done"}]}
        return {"status": "completed"}

    async def close(self):
        pass


def test_job_completion_triggers_command_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)

    async def run():
        client = FakeClient("Save1")
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
        mock_ws = AsyncMock()

        turn_count = 0
        prompts = []
        request_ids = []

        class FakeBackend:
            def run(self, active_task, cid, prompt):
                nonlocal turn_count
                turn_count += 1
                prompts.append(prompt)
                request_ids.append(active_task.request_id)
                token = os.environ.get("STARDEW_DECISION_TOKEN")
                if turn_count == 1:
                    goal = bridge._work_store.add_goal("Save1", "种植胡萝卜", source="user")
                    bridge._work_store.submit_plan(
                        "Save1",
                        goal_id=goal.id,
                        decision_token=token,
                        tasks=[
                            {
                                "id": "t1",
                                "title": "锄地",
                                "steps": [{"id": "s1", "operation": "hoe_tiles", "params": {}}],
                            }
                        ],
                    )
                    return {"success": True, "response": "第一步：锄地已规划"}
                else:
                    return {"success": True, "response": "胡萝卜已种植完毕"}

        bridge._backend = FakeBackend()

        await bridge.handle_chat_submit(mock_ws, "req-player-1", "种植胡萝卜", "Save1")
        assert turn_count == 1
        assert "Save1" in bridge._command_chains
        assert bridge._command_chains["Save1"].instruction == "种植胡萝卜"

        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()

        assert turn_count == 2
        assert request_ids[1].startswith("chain-")
        assert "始终用中文回复玩家" in prompts[0]
        assert "chat-continuation" in prompts[1]
        assert "这是对玩家指令『种植胡萝卜』的继续" in prompts[1]
        assert "始终用中文回复玩家" in prompts[1]
        assert "上一步作业结果（lastResult）" in prompts[1]
        assert "Save1" not in bridge._command_chains

    asyncio.run(run())


def test_command_chain_terminates_when_model_does_not_submit_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)

    async def run():
        client = FakeClient("Save1")
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
        mock_ws = AsyncMock()

        class FakeBackend:
            def run(self, active_task, cid, prompt):
                return {"success": True, "response": "今天天气很好，无需额外操作。"}

        bridge._backend = FakeBackend()

        await bridge.handle_chat_submit(mock_ws, "req-chat-only", "在吗", "Save1")
        assert "Save1" not in bridge._command_chains

        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert "Save1" not in bridge._command_chains

    asyncio.run(run())


def test_command_chain_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    monkeypatch.setenv("STARDEW_COMMAND_CHAIN_MAX", "2")

    async def run():
        client = FakeClient("Save1")
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
        mock_ws = AsyncMock()

        turn_count = 0

        class FakeBackend:
            def run(self, active_task, cid, prompt):
                nonlocal turn_count
                turn_count += 1
                token = os.environ.get("STARDEW_DECISION_TOKEN")
                goal = bridge._work_store.add_goal("Save1", "持续开荒", source="user")
                bridge._work_store.submit_plan(
                    "Save1",
                    goal_id=goal.id,
                    decision_token=token,
                    tasks=[
                        {
                            "id": f"t{turn_count}",
                            "title": f"步骤{turn_count}",
                            "steps": [{"id": f"s{turn_count}", "operation": "clear_debris", "params": {}}],
                        }
                    ],
                )
                return {"success": True, "response": f"已选择步骤{turn_count}"}

        bridge._backend = FakeBackend()

        await bridge.handle_chat_submit(mock_ws, "req-loop", "开荒", "Save1")
        assert turn_count == 1

        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert turn_count == 2

        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert turn_count == 3

        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert turn_count == 3
        assert "Save1" not in bridge._command_chains

        replies = [json.loads(call[0][0]) for call in mock_ws.send_text.call_args_list]
        limit_replies = [r for r in replies if "上限" in r.get("payload", {}).get("replyText", "")]
        assert len(limit_replies) == 1
        assert "2次" in limit_replies[0]["payload"]["replyText"]

    asyncio.run(run())


def test_command_chain_broken_by_pause_cancel_new_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)

    async def run():
        client = FakeClient("Save1")
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
        mock_ws = AsyncMock()

        turn_count = 0

        class FakeBackend:
            def run(self, active_task, cid, prompt):
                nonlocal turn_count
                turn_count += 1
                token = os.environ.get("STARDEW_DECISION_TOKEN")
                goal = bridge._work_store.add_goal("Save1", "任务", source="user")
                bridge._work_store.submit_plan(
                    "Save1",
                    goal_id=goal.id,
                    decision_token=token,
                    tasks=[
                        {
                            "id": f"t{turn_count}",
                            "title": "任务",
                            "steps": [{"id": "s1", "operation": "clear_debris", "params": {}}],
                        }
                    ],
                )
                return {"success": True, "response": "已选择短作业"}

        bridge._backend = FakeBackend()

        await bridge.handle_chat_submit(mock_ws, "req-1", "任务1", "Save1")
        assert "Save1" in bridge._command_chains

        pause_env = Envelope.create_autonomy_control(bridge.instance_id, "p1", "Save1", "pause")
        await bridge._handle_autonomy_control(mock_ws, pause_env, "Save1")
        assert "Save1" not in bridge._command_chains

        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert turn_count == 1

        bridge._work_store.set_paused("Save1", False)
        await bridge.handle_chat_submit(mock_ws, "req-2", "任务2", "Save1")
        assert "Save1" in bridge._command_chains
        assert turn_count == 2

        await bridge.handle_chat_cancel(mock_ws, "req-2", "player_cancelled", "Save1")
        assert "Save1" not in bridge._command_chains

        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert turn_count == 2

        await bridge.handle_chat_submit(mock_ws, "req-3", "任务3", "Save1")
        assert bridge._command_chains["Save1"].instruction == "任务3"
        old_gen = bridge._command_chains["Save1"].generation
        assert turn_count == 3

        await bridge.handle_chat_submit(mock_ws, "req-4", "任务4覆盖", "Save1")
        assert bridge._command_chains["Save1"].instruction == "任务4覆盖"
        assert bridge._command_chains["Save1"].generation > old_gen
        assert turn_count == 4

    asyncio.run(run())


def test_continuation_does_not_unpause_and_paused_skips_continuation(tmp_path: Path):
    async def run():
        client = FakeClient("Save1")
        bridge = ChatBridge(run_dir=tmp_path, backend="agy", internal_plan_client=client)
        mock_ws = AsyncMock()

        bridge._work_store.set_paused("Save1", True)
        assert bridge._work_store.state("Save1").paused is True

        bridge._chain_requests.add("chain-mock-1")
        bridge._command_chains["Save1"] = CommandChain(
            instruction="工作",
            save_id="Save1",
            started_at=0.0,
            chain_count=1,
            generation=1,
            ws=mock_ws,
        )

        class FakeBackend:
            def run(self, active_task, cid, prompt):
                return {"success": True, "response": "已暂停不应被执行"}

        bridge._backend = FakeBackend()

        await bridge._on_job_terminal("Save1", StepExecution(status="executed", outcome="completed"), "SHORT_JOB_TERMINAL")
        await bridge.wait_for_chains()

        assert "Save1" not in bridge._command_chains
        assert bridge._work_store.state("Save1").paused is True

        await bridge.handle_chat_submit(mock_ws, "chain-mock-direct", "续链", "Save1")
        assert bridge._work_store.state("Save1").paused is True

    asyncio.run(run())


def test_autonomy_does_not_trigger_command_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)

    async def run():
        client = FakeClient("Save1")
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
        mock_ws = AsyncMock()

        turn_count = 0

        class FakeBackend:
            def run(self, active_task, cid, prompt):
                nonlocal turn_count
                turn_count += 1
                token = os.environ.get("STARDEW_DECISION_TOKEN")
                goal = bridge._work_store.add_goal("Save1", "自主工作", source="agent")
                bridge._work_store.submit_plan(
                    "Save1",
                    goal_id=goal.id,
                    decision_token=token,
                    tasks=[
                        {
                            "id": "t_auto",
                            "title": "自动浇水",
                            "steps": [{"id": "s_auto", "operation": "water_auto", "params": {}}],
                        }
                    ],
                )
                return {"success": True, "response": "自主决策完成"}

        bridge._backend = FakeBackend()

        auto_req = "autonomy-test-123"
        bridge._autonomy_requests[auto_req] = "fingerprint-abc"
        await bridge.handle_chat_submit(mock_ws, auto_req, "自主规划工作", "Save1")
        assert "Save1" not in bridge._command_chains

        # The autonomy decision actually selected its short job (the old
        # "system" goal source raised WorkStateError and the whole backend run
        # was swallowed, so nothing below was ever exercised).
        decision = bridge._work_store.state("Save1").decision
        assert decision.get("selected") is True
        assert decision.get("taskId") == "t_auto"

        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()

        # Native dispatch actually ran and the job actually reached a terminal
        # state, recorded as this decision's last_job.
        assert client.dispatched == ["water_auto"]
        last_job = bridge._work_store.state("Save1").last_job
        assert last_job.get("status") == "completed"
        assert last_job.get("decisionId") == decision.get("token")

        # An autonomy request never starts a player-instruction command chain.
        assert turn_count == 1
        assert "Save1" not in bridge._command_chains

    asyncio.run(run())


def test_error_breaks_command_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)

    async def run():
        client = FakeClient("Save1")
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=client)
        mock_ws = AsyncMock()

        class FakeBackend:
            def run(self, active_task, cid, prompt):
                return {"success": False, "error": "RESOURCE_EXHAUSTED", "response": ""}

        bridge._backend = FakeBackend()

        await bridge.handle_chat_submit(mock_ws, "req-quota", "种地", "Save1")
        assert "Save1" not in bridge._command_chains

    asyncio.run(run())
