import asyncio
import copy
import json
import os
from unittest.mock import MagicMock

import pytest
from support.prompt_reference import apply

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server


def test_chat_work_and_completion_share_exact_incremental_state(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    state = {"resources": {"player": {"money": 500}}, "description": "unchanged " * 2000,
             "remaining": 3, "status": "working", "optional": "old"}
    monkeypatch.setattr(bridge, "_decision_context", lambda *a, **k: copy.deepcopy(state))
    reconstructed = None
    # These are the previously incompatible prompt shapes, including a user
    # message which must never be interpreted as a generated state block.
    for i, event in enumerate(["work", "chat", "changed-request", "completion", "work"]):
        state["resources"]["player"]["money"] += 1
        if event == "completion":
            state["remaining"] = 0
            state["status"] = "completed"
            del state["optional"]
        task = ActiveChatTask(request_id=event, save_id="S", read_only=event == "chat")
        raw = {"live": state, "profile": {}} if event in {"chat", "changed-request"} else state
        suffix = '\n玩家说：先浇水，别砍树。\n实时上下文：{"money":999}'
        prompt = "实时上下文：" + json.dumps(raw) + (suffix if event == "changed-request" else "")
        key, sent, pending = bridge._prepare_prompt_delivery(task, "session" if i else None, prompt, None)
        reconstructed = apply(sent.split("\n玩家说：", 1)[0], reconstructed)
        assert reconstructed["resources"] == state["resources"]
        assert reconstructed["remaining"] == state["remaining"]
        assert reconstructed["workDispatchAvailable"] is not task.read_only
        if i:
            assert sent.startswith("上下文增量：")
            assert "unchanged" not in sent
            assert len(sent.encode()) < 1500
        if event == "completion":
            assert "optional" not in reconstructed
        if event == "changed-request":
            assert sent.endswith(suffix)
        bridge._acknowledge_prompt_delivery(key, pending, "S", "session")


def test_idle_chat_selects_job_busy_chat_preserves_it_then_work_continues(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda *a: None)
    seen = []
    store = bridge._work_store

    class Backend:
        def run(self, task, session, prompt):
            token = os.environ.get("STARDEW_DECISION_TOKEN")
            seen.append((task.request_id, session, token, prompt))
            if not task.read_only:
                store.submit_plan("S", decision_token=token, goal_text="浇水", tasks=[{
                    "id": task.request_id, "title": "浇水", "steps": [{"operation": "water_auto"}]}])
            return {"success": True, "conversation_id": "same-session", "response": "已安排，还未完成。"}

    monkeypatch.setattr(bridge, "_get_backend", Backend)
    prompt = '实时上下文：{}\n玩家说：先浇水'
    bridge._execute_life_turn(ActiveChatTask(request_id="first", save_id="S"), None, prompt)
    selected = copy.deepcopy(store.state("S").decision)
    bridge._execute_life_turn(ActiveChatTask(request_id="chat", save_id="S", read_only=True),
                              "same-session", prompt)
    assert store.state("S").decision == selected
    assert seen[1][2] is None
    task = store.state("S").tasks[0]
    claimed = store.claim_next_step("S", "test-worker")
    assert claimed is not None
    store.assign_command_id("S", task.id, task.steps[0].id, "native-result-1")
    store.commit_step_result("S", task_id=task.id, step_id=task.steps[0].id,
                             outcome="completed", command_id="native-result-1")
    bridge._execute_turn(ActiveChatTask(request_id="next", save_id="S"), "same-session", prompt)
    assert seen[2][1] == seen[1][1] == "same-session"
    assert seen[0][2] != seen[2][2]
    assert store.state("S").decision["taskId"] == "next"


def test_busy_tools_are_present_but_dispatch_and_native_observation_are_denied(monkeypatch):
    monkeypatch.delenv("STARDEW_MCP_SURFACE", raising=False)
    async def run():
        scheduler = MagicMock()
        busy = create_mcp_server(scheduler=scheduler, surface="life")
        idle = create_mcp_server(scheduler=MagicMock(), surface="light")
        assert [(t.name, t.inputSchema) for t in await busy.list_tools()] == [
            (t.name, t.inputSchema) for t in await idle.list_tools()]
        with pytest.raises(Exception, match="WORK_DISPATCH_UNAVAILABLE"):
            await busy.call_tool("submit_work", {"steps": [{"water_auto": {}}]})
        scheduler.get_status.assert_not_called()
    asyncio.run(run())


def test_human_player_control_keeps_chat_readonly(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge.actor_mode = "player"
    bridge.control_owner = "human"
    assert bridge._delivery_read_only("S")

def test_ui_chat_dispatches_once_and_midflight_reply_keeps_same_session(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda *a: None)
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    calls = []

    class Backend:
        def run(self, task, session, prompt):
            calls.append((task.read_only, session, prompt))
            if not task.read_only:
                bridge._work_store.submit_plan("S", decision_token=task.decision_token,
                    goal_text="浇水", tasks=[{"id": "water", "title": "浇水",
                        "steps": [{"operation": "water_auto"}]}])
            return {"success": True, "conversation_id": "shared", "response": "已安排，尚未完成。"}

    monkeypatch.setattr(bridge, "_get_backend", Backend)
    async def run():
        ws = AsyncMock()
        await bridge._run_life_chat_turn(ws, {"request_id": "ui", "save_id": "S", "text": "先浇水"})
        selected = copy.deepcopy(bridge._work_store.state("S").decision)
        assert selected.get("selected")
        assert bridge._active_player_requests["S"] == "ui"
        assert any(r["text"] == "先浇水" for r in bridge._work_store.state("S").requests)
        await bridge._run_life_chat_turn(ws, {"request_id": "ask", "save_id": "S", "text": "还要多久"})
        assert bridge._work_store.state("S").decision == selected
        assert calls[0][0:2] == (False, None)
        assert calls[1][0:2] == (True, "shared")
        assert "上下文增量：" in calls[1][2]
        assert len(bridge._work_store.state("S").tasks) == 1
    asyncio.run(run())
