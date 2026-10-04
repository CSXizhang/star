"""Independent acceptance at the conversation / work / schedule boundary.

These tests do not start a paid model or pretend to establish native gameplay.
The isolated game driver provides the separate physical-action evidence.
"""

import asyncio
import json
import os
import threading
import time
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.protocol import Envelope
from stardew_ai_runtime.scheduler import CompanionScheduler


@pytest.mark.parametrize("paused", [False, True])
def test_idle_help_toggle_preserves_explicit_pause_across_restart(tmp_path, paused):
    path = tmp_path / "autonomy.json"
    controller = AutonomyController(path)
    controller.set_paused("farm", paused)
    for mode in ("free", "command", "free", "command"):
        controller.control("farm", "set_mode", mode=mode)
        restored = AutonomyController(path).state("farm")
        assert restored.paused is paused
        assert restored.enabled is (mode == "free")


def test_assigned_goal_continues_with_idle_help_disabled(tmp_path):
    controller = AutonomyController(tmp_path / "autonomy.json")
    controller.set_mode("farm", "command", goal_scope="player-assigned")
    controller.set_preferences("farm", goal="先种地再养鸡")
    snapshot = {"world": {"year": 1, "season": "spring", "dayOfMonth": 2},
                "farmWork": {"matureCropCount": 1, "cropUnwateredTiles": [{"x": 1, "y": 2}]}}
    candidate = controller.next_candidate("farm", snapshot)
    assert candidate is not None
    assert candidate["kind"] == "decision"
    assert candidate["goal"] == "先种地再养鸡"
    controller.set_paused("farm", True)
    assert controller.next_candidate("farm", snapshot) is None
    controller.set_paused("farm", False)
    assert controller.next_candidate("farm", snapshot) is not None
    assert controller.state("farm").enabled is False


@pytest.mark.parametrize("rest_state", ["winding-down", "returning-home", "waiting-for-bed", "sleeping"])
def test_native_rest_snapshot_suppresses_new_planning_without_failure_budget(tmp_path, rest_state):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        bridge._autonomy.set_mode("farm", "free")
        bridge.handle_chat_submit = AsyncMock(side_effect=AssertionError("planning resumed during native rest"))
        envelope = Envelope(protocol_version="1.0", message_type="snapshot", save_id="farm", world_revision=7,
            message_id="native-rest", sender_instance_id="native", sequence_number=7, sent_at="2026-10-02T06:00:00Z",
            payload={"world": {"year": 1, "season": "spring", "dayOfMonth": 2, "timeOfDay": 2000},
                     "companion": {"restState": rest_state, "bedtime": 2400},
                     "farmWork": {"cropUnwateredCount": 1}})
        await bridge._maybe_schedule_autonomy(envelope, "farm", {}, AsyncMock(), force=True)
        bridge.handle_chat_submit.assert_not_called()
        assert bridge._autonomy.state("farm").failure_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["chat", "plan"])
def test_slow_conversation_never_claims_or_releases_native_worker(tmp_path, monkeypatch, mode):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._autonomy.set_mode("farm", "command")
        store = bridge._work_store
        goal = store.add_goal("farm", "正在浇水", source="user")
        store.begin_decision("farm", "pending-token", goal_scope=goal.id)
        store.submit_plan("farm", goal_id=goal.id, decision_token="pending-token", tasks=[{
            "id": "accepted-water", "title": "浇水", "steps": [{
                "id": "native-water", "operation": "water_zone", "params": {"tiles": [{"x": 65, "y": 15}]},
            }],
        }])
        before = deepcopy(store.state("farm"))
        release = threading.Event()
        started = threading.Event()
        bridge._claim_execution = AsyncMock(side_effect=AssertionError("chat claimed the native worker"))
        bridge._release_execution = AsyncMock(side_effect=AssertionError("chat released the native worker"))

        def provider(_task, _cid, _prompt, _mode):
            started.set()
            assert release.wait(3)
            return {"success": True, "response": "好，田里交给我。"}

        monkeypatch.setattr(bridge, "_execute_life_turn", provider)
        ws = AsyncMock()
        turn = asyncio.create_task(bridge._handle_life_chat_submit(ws, {"payload": {
            "requestId": "chat-during-water", "saveId": "farm", "mode": mode, "text": "今天怎么样？",
        }}, "farm"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert not bridge._execution_lock.locked()
            assert store.state("farm") == before
            # A concurrent direct control remains responsive during model latency.
            await bridge._handle_autonomy_control(ws, Envelope.create_autonomy_control(
                "test-ui", "pause-now", "farm", "pause"), "farm")
            assert store.state("farm").paused is True
            release.set()
            await asyncio.wait_for(turn, 2)
            assert store.state("farm").paused is True
            bridge._claim_execution.assert_not_called()
            bridge._release_execution.assert_not_called()
            replies = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
            assert any(r["messageType"] == "life.chat.reply" and r["payload"]["status"] == "completed"
                       for r in replies)
        finally:
            release.set()
            if not turn.done():
                await asyncio.wait_for(turn, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("action,bedtime", [("pause", None), ("resume", None), ("bedtime", 2300)])
def test_manage_companion_real_registered_call_uses_cache_without_native_connection(tmp_path, monkeypatch, action, bedtime):
    async def scenario():
        monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
        monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "accepted-control")
        monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "farm")
        data = tmp_path / "data"
        data.mkdir()
        (data / "life-turn.json").write_text(json.dumps({"requestId": "accepted-control", "saveId": "farm"}), encoding="utf-8")
        (data / "life-snapshot.json").write_text(json.dumps({
            "saveId": "farm", "capturedAt": time.time(), "worldRevision": 1,
            "payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 2}},
        }), encoding="utf-8")
        scheduler = CompanionScheduler(run_dir=tmp_path)
        scheduler.get_status = AsyncMock(side_effect=AssertionError("conversation opened the native connection"))
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="life")
        arguments = {"action": action}
        if bedtime is not None:
            arguments["bedtime"] = bedtime
        await server.call_tool("manage_companion", arguments)
        intent = json.loads(next((data / "life-controls").glob("accepted-control--*.json")).read_text(encoding="utf-8"))
        assert intent["saveId"] == "farm"
        assert intent["action"] == action
        assert intent["bedtime"] == bedtime
        scheduler.get_status.assert_not_called()

    asyncio.run(scenario())


def test_conversation_cache_rejects_switching_to_another_loaded_save(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
        monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "old-farm-turn")
        monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "old-farm")
        data = tmp_path / "data"
        data.mkdir()
        (data / "life-snapshot.json").write_text(json.dumps({
            "saveId": "new-farm", "capturedAt": time.time(), "worldRevision": 1,
            "payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 2}},
        }), encoding="utf-8")
        server = create_mcp_server(run_dir=tmp_path, surface="life")
        with pytest.raises(ToolError, match="SAVE_CHANGED"):
            await server.call_tool("manage_companion", {"action": "pause"})
        assert not list((data / "life-controls").glob("old-farm-turn*.json"))
        with pytest.raises(ToolError, match="SAVE_CHANGED"):
            await server.call_tool("manage_milestones", {"action": "propose", "title": "浇水", "preparation": ["water"]})

    asyncio.run(scenario())


def test_disabled_idle_help_completes_second_job_after_tool_preparation(tmp_path, monkeypatch):
    """A completed prerequisite must wake the assigned chain, not stop it."""
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)

    async def scenario():
        operations = []

        class Native:
            async def current_save_id(self):
                return "farm"

            async def call_tool(self, name, params):
                assert name == "dispatch_plan_operation"
                operations.append(params["operation"])
                return {"status": "completed", "effects": [{"kind": params["operation"]}]}

            async def close(self):
                pass

        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=Native())
        bridge._autonomy.set_mode("farm", "command")
        calls = []
        assigned_goal = bridge._work_store.add_goal("farm", "种地再照料鸡", source="user")

        class Provider:
            def run(self, task, _cid, _prompt):
                calls.append(task.request_id)
                if len(calls) <= 2:
                    operation = "refill_watering_can" if len(calls) == 1 else "water_zone"
                    bridge._work_store.submit_plan("farm", goal_id=assigned_goal.id,
                        decision_token=os.environ["STARDEW_DECISION_TOKEN"], tasks=[{
                            "id": f"job-{len(calls)}", "title": operation, "steps": [{
                                "id": f"step-{len(calls)}", "operation": operation, "params": {},
                            }],
                        }])
                return {"success": True, "response": "田里交给我。"}

        bridge._backend = Provider()
        await bridge.handle_chat_submit(AsyncMock(), "assigned", "种地再照料鸡，你安排", "farm")
        assert len(calls) == 1
        for _ in range(2):
            await bridge._plan_worker.evaluate()
            await bridge.wait_for_chains()
        assert operations == ["refill_watering_can", "water_zone"]
        assert len(calls) == 3
        assert all(t.status == "completed" for t in bridge._work_store.state("farm").tasks)
        assert bridge._autonomy.state("farm").enabled is False
        assert bridge._work_store.state("farm").paused is False

    asyncio.run(scenario())
