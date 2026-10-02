"""Actual FastMCP/bridge/worker boundaries for the NPC -> work panel loop."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import jsonschema
import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.life_chat import LifeChatService
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.protocol import Envelope, LifeChatSubmitPayload, ProtocolError


def test_archived_native_result_and_new_job_have_separate_specific_names(tmp_path):
    from stardew_ai_runtime.chat_bridge import CommandChain
    from stardew_ai_runtime.work_state import ExecutionEntry, Step, Task
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._work_store._mutate("S", lambda state: (
        state.archive.append({"task": {"id": "build-old", "title": "订建鸡舍", "goal_id": "g1"}}),
        setattr(state, "last_job", {"taskId": "build-old", "status": "completed",
                                  "effects": [{"state": "building-construction-ordered"}]}),
    ))
    result = bridge._player_activity("S")
    assert result["summary"] == "订建鸡舍：建造已下单，等待施工。"
    bridge._work_store._mutate("S", lambda state: (
        state.tasks.append(Task(id="water-new", goal_id="g1", title="浇菜地", status="running")),
        state.decision.update(taskId="water-new", selected=True, finished=False),
    ))
    result = bridge._player_activity("S")
    assert result["summary"] == "正在处理：浇菜地"
    assert "鸡舍" not in result["summary"]
    bridge._work_store._mutate("S", lambda state: (
        setattr(state.tasks[-1], "status", "completed"),
        state.tasks[-1].steps.append(Step(id="water-step", operation="water_auto", effects=[{"state": "watered"}] * 20)),
        state.decision.update(finished=True),
        setattr(state, "last_job", {"taskId": "water-new", "status": "completed", "effects": [{"state": "watered"}] * 8}),
    ))
    bridge._command_chains["S"] = CommandChain(instruction="继续种植", save_id="S", started_at=0, waiting_task_id="water-new")
    result = bridge._player_activity("S")
    assert result["phase"] == "completed"
    assert result["summary"] == "浇菜地：已浇水 20 格。"
    assert bridge._work_store.effect_summary([{"state": "hoed"}] * 40) == "已开垦 40 格"
    assert bridge._work_store.effect_summary([{"state": "harvested"}] * 25) == "已收获 25 格"
    assert bridge._work_store.effect_summary([{"state": "deposited", "stack": n} for n in (36, 2, 1)]) == "已入箱 39 件"
    assert bridge._work_store.effect_summary([{"state": "refilled"}]) == "水壶已补满"
    assert bridge._work_store.effect_summary([{"state": "ate-food", "stack": 1}] * 4) == "已吃 4 份食物恢复体力"
    bridge._work_store._mutate("S", lambda state: state.executions.append(ExecutionEntry(
        command_id="hoe-partial", task_id="water-new", step_id="hoe", operation="hoe_tiles",
        outcome="partial", effects=[{"state": "hoed"}] * 2, reason_code="STEP_PARTIAL",
    )))
    summary = bridge._work_store.recent_execution_summary("S")[-1]["summary"]
    assert "已开垦 2 格" in summary
    assert "还有未完成的地块" in summary
    assert "STEP_PARTIAL" not in summary
    bridge._work_store.add_todo("S", intent="核对鸡舍施工进度", trigger={"type": "calendar", "year": 1, "season": "spring", "day": 20})
    bridge._latest_snapshot_payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 18}}
    assert "春20日" in bridge._player_activity("S")["nextStep"]
    bridge._latest_snapshot_payload["world"]["dayOfMonth"] = 21
    assert bridge._player_activity("S")["nextStep"] == "接下来核对鸡舍进度，再继续养鸡安排。"
    bridge._work_store.add_todo("S", intent="豪华鸡舍施工结束后核对容量，再继续养鸡", trigger={"type": "calendar", "year": 1, "season": "spring", "day": 23})
    assert bridge._player_activity("S")["nextStep"] == "春23日核对鸡舍进度，再继续养鸡安排。"


def test_quiet_autonomy_updates_named_work_panel_without_chat_chatter(tmp_path):
    from stardew_ai_runtime.work_state import Step, Task

    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._work_store._mutate("S", lambda state: (
            state.tasks.append(Task(id="water", goal_id="g", title="浇好新种的菜地", status="running")),
            state.decision.update(taskId="water", selected=True, finished=False),
        ))
        ws = type("Socket", (), {"send_text": AsyncMock()})()
        running = Envelope.create_chat_reply(bridge.instance_id, "autonomy-test", "job-running", "routine", save_id="S")
        assert await bridge._send_reply(ws, running)
        await bridge._send_reply(ws, running)
        assert ws.send_text.await_count == 1
        payload = json.loads(ws.send_text.call_args.args[0])
        assert payload["messageType"] == "life.profile.state"
        assert payload["payload"]["work"]["activity"]["summary"] == "正在处理：浇好新种的菜地"
        bridge._work_store._mutate("S", lambda state: (
            setattr(state.tasks[0], "status", "completed"),
            state.tasks[0].steps.append(Step(id="done", operation="water_zone", effects=[{"state": "watered"}] * 18)),
            state.decision.update(finished=True),
            setattr(state, "last_job", {"taskId": "water", "status": "completed"}),
        ))
        await bridge._send_reply(ws, Envelope.create_chat_reply(
            bridge.instance_id, "autonomy-test", "job-completed", "routine", save_id="S"))
        payload = json.loads(ws.send_text.call_args.args[0])
        assert ws.send_text.await_count == 2
        assert payload["payload"]["work"]["activity"]["summary"] == "浇好新种的菜地：已浇水 18 格。"
        assert payload["payload"]["work"]["activity"]["phase"] == "completed"
    asyncio.run(run())


def test_propose_accept_real_work_terminal_reopen_panel(tmp_path, monkeypatch):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)

    async def run():
        dispatched = []

        class Native:
            async def current_save_id(self):
                return "S"

            async def close(self):
                pass

            async def call_tool(self, name, args):
                dispatched.append(args["operation"])
                return {"status": "completed", "effects": [{"tile": {"x": 1, "y": 1}, "state": "watered"}]}

        bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=Native())
        bridge._latest_snapshot_payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 11}}

        class Scheduler:
            run_dir = tmp_path
            latest_snapshot = {"payload": bridge._latest_snapshot_payload}

            async def get_status(self):
                return {"saveId": "S"}

        server = create_mcp_server(run_dir=tmp_path, scheduler=Scheduler(), surface="full")

        class Provider:
            plan_turns = 0
            work_turns = 0

            def run(self, task, cid, prompt):
                if os.getenv("STARDEW_LIFE_MODE") == "plan":
                    self.plan_turns += 1
                    asyncio.run(server.call_tool("manage_milestones", {
                        "action": "propose", "title": "照料缺水菜地", "summary": "仅浇眼前两格缺水作物，不购买或出货",
                        "preparation": ["water"],
                    }))
                    return {"success": True, "response": "我可以先浇眼前两格缺水菜地。你觉得呢？"}
                self.work_turns += 1
                if self.work_turns == 1:
                    goal = bridge._work_store.list_goals("S")[0]
                    asyncio.run(server.call_tool("submit_plan", {"goal_id": goal["id"], "tasks": [
                        {"title": "浇眼前两格菜地", "steps": [{"operation": "water_auto", "params": {"max_tiles": 2}}]}]}))
                return {"success": True, "response": "按眼前实际情况处理。"}

        provider = Provider()
        bridge._backend = provider
        ws = AsyncMock()
        await bridge._run_life_chat_turn(ws, {"request_id": "plan", "save_id": "S", "mode": "plan", "text": "商量一下菜地"})
        replies = [json.loads(call.args[0]) for call in ws.send_text.call_args_list]
        offered = next(reply["payload"] for reply in reversed(replies) if reply["messageType"] == "life.chat.reply")
        assert offered["proposalReady"] is True
        assert offered["activity"]["phase"] == "proposed"
        assert not bridge._work_store.list_goals("S") and not dispatched
        assert provider.plan_turns == 1 and provider.work_turns == 0
        await bridge._handle_life_chat_submit(ws, {"payload": {
            "requestId": "accept", "saveId": "S", "mode": "plan", "text": "就这样安排",
            "acceptedNodeId": offered["proposalNodeId"]}}, "S")
        assert provider.plan_turns == 1  # accepting the real node needs no extra planning generation
        assert provider.work_turns == 1
        assert bridge._player_activity("S")["phase"] == "working"
        assert not bridge._autonomy.state("S").enabled
        await bridge._plan_worker.evaluate()
        await bridge.wait_for_chains()
        assert dispatched == ["water_auto"]
        assert provider.work_turns == 2  # normal chain summary after real native terminal
        activity = bridge._life_work_projection("S")["activity"]
        assert activity["phase"] == "completed"
        assert "浇眼前两格菜地" in activity["summary"]
        replies = [json.loads(call.args[0]) for call in ws.send_text.call_args_list]
        terminal = next(reply["payload"] for reply in replies if reply["messageType"] == "chat.reply" and reply["payload"]["status"] == "job-completed")
        assert terminal["saveId"] == "S" and terminal["commandId"].startswith("preparation-")
        assert "water_auto" not in terminal["replyText"]
        reopened = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        assert reopened._life_work_projection("S")["activity"]["phase"] == "completed"

    asyncio.run(run())


@pytest.mark.parametrize("state", ["paused", "future", "deferred", "changed", "chat"])
def test_explicit_acceptance_respects_state_and_cannot_replay(tmp_path, state):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._latest_snapshot_payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 11}}
        proposal = bridge._milestone_store.propose("S", title="清理指定位置", summary="仅清理玩家指定的左侧两格，不砍树",
            target_date="1:spring:12" if state == "future" else "1:spring:11", preparation=["clear"])
        bridge._life_proposals["S"] = (proposal["id"], bridge._profile_revision("S"), proposal["updatedAt"])
        bridge.handle_chat_submit = AsyncMock()
        if state == "paused":
            bridge._work_store.set_paused("S", True)
        elif state == "deferred":
            bridge._milestone_store.defer("S", proposal["id"], work_store=bridge._work_store)
        elif state == "changed":
            bridge._life_proposals["S"] = (proposal["id"], 999, proposal["updatedAt"])
        item = {"request_id": "approve", "save_id": "S", "mode": "chat" if state == "chat" else "plan",
                "accepted_node_id": proposal["id"], "text": "认可"}
        ws = AsyncMock()
        await bridge._run_life_chat_turn(ws, item)
        bridge.handle_chat_submit.assert_not_awaited()
        reply = json.loads(ws.send_text.call_args.args[0])["payload"]
        if state in {"paused", "future"}:
            assert reply["status"] == "completed"
            assert reply["activity"]["phase"] == ("paused" if state == "paused" else "waiting")
            assert "左侧两格" in bridge._work_store.list_todos("S")[0]["intent"]
        else:
            assert reply["status"] == "failed"
            assert not bridge._work_store.list_goals("S")
        await bridge._run_life_chat_turn(ws, item)
        bridge.handle_chat_submit.assert_not_awaited()

    asyncio.run(run())


def test_plain_chat_and_unstored_plan_never_offer_execute(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._execute_life_turn = lambda *args: {"success": True, "response": "我们可以再想想。"}
        bridge.handle_chat_submit = AsyncMock()
        ws = AsyncMock()
        for mode in ("chat", "plan"):
            await bridge._run_life_chat_turn(ws, {"request_id": mode, "save_id": "S", "mode": mode, "text": "能做些什么"})
            reply = json.loads(ws.send_text.call_args.args[0])["payload"]
            assert not reply.get("proposalReady")
        bridge.handle_chat_submit.assert_not_awaited()
        assert not bridge._work_store.list_tasks("S")

    asyncio.run(run())


def test_wire_schema_and_four_preferences():
    schema = json.loads((Path(__file__).resolve().parents[2] / "protocol/schemas/protocol-v0.1.schema.json").read_text())
    reply = Envelope.create_life_chat_reply("runtime", "plan", "S", "completed", 1, 0,
        proposal_ready=True, proposal_node_id="node-1",
        activity={"phase": "proposed", "summary": "准备材料", "nextStep": "认可或修改"})
    jsonschema.validate(reply.to_mapping(), schema)
    with pytest.raises(ProtocolError, match="acceptedNodeId"):
        LifeChatSubmitPayload.from_mapping({"requestId": "r", "saveId": "S", "mode": "chat", "text": "行", "acceptedNodeId": "n"})
    for style in ("earn", "workhorse", "community", "decor"):
        prompt = LifeChatService.build_system_prompt({"playStyle": style}, {}, {}, mode="plan")
        assert "propose" in prompt and "软偏好" in prompt and "选择方向本身不是工作授权" in prompt
    decor = LifeChatService.build_system_prompt({"playStyle": "decor"}, {}, {}, mode="plan")
    assert "摆放和回收家具、地板、围栏与物件" in decor
    assert "制造已解锁配方" in decor and "搬迁建筑" in decor
    assert "服务柜台办理并等待真实工期" in decor
    assert "提交仍由玩家完成" in LifeChatService.build_system_prompt({"playStyle": "community"}, {}, {}, mode="plan")


def test_release_bridge_uses_selected_agy_model_and_provider_effort(tmp_path, monkeypatch):
    config = tmp_path / "chat-backend.json"
    config.write_text('{"backend":"agy","model":"claude-sonnet-4-6"}', encoding="utf-8")
    monkeypatch.setenv("STARDEW_CHAT_BACKEND_CONFIG", str(config))
    bridge = ChatBridge(run_dir=tmp_path)
    assert (bridge.backend_name, bridge.model, bridge.effort) == ("agy", "claude-sonnet-4-6", "default")
    config.write_text('{"backend":"agy","model":"claude-sonnet-4-6","effort":"high"}', encoding="utf-8")
    assert ChatBridge(run_dir=tmp_path).effort == "high"
    override = ChatBridge(run_dir=tmp_path, model="explicit-model", effort="low")
    assert (override.model, override.effort) == ("explicit-model", "low")
    assert ChatBridge(run_dir=tmp_path, backend="kimi").model == "kimi-code/k3"
