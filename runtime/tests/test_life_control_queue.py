"""Multiple tool controls in one sentence must be durable and ordered."""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.work_state import WorkStore


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("stardew_ai_runtime.mcp_server.time.time_ns", lambda: 1791200000000000000)
    monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
    monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "two-controls")
    monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "farm")
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._life_turn_lease("two-controls", "farm")
    (tmp_path / "data" / "life-snapshot.json").write_text(json.dumps({
        "saveId": "farm", "capturedAt": time.time(), "payload": {"world": {"dayOfMonth": 2}},
    }), encoding="utf-8")
    scheduler = MagicMock(run_dir=tmp_path)
    scheduler.get_status = AsyncMock(return_value={"saveId": "farm", "world": {"dayOfMonth": 2}})
    return bridge, create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="life")


@pytest.mark.parametrize("actions,paused", [(["bedtime", "resume"], False),
                                          (["resume", "pause"], True),
                                          (["pause", "bedtime"], True)])
def test_two_real_tool_commits_are_consumed_in_order(tmp_path, monkeypatch, actions, paused):
    async def scenario():
        bridge, server = setup(tmp_path, monkeypatch)
        bridge._autonomy.set_paused("farm", True)
        for action in actions:
            await server.call_tool("manage_arrangements", {"change": {"manage_companion": {"action": action, **({"bedtime": 2300} if action == "bedtime" else {})}}})
        assert len(list((tmp_path / "data" / "life-controls").glob("*.json"))) == 2
        if paused:
            with pytest.raises(ToolError, match="WORK_STOPPED"):
                await server.call_tool("manage_arrangements", {"change": {"manage_milestones": {"action": "propose", "title": "抢在暂停前派工", "preparation": ["water"]}}})
        await bridge._apply_life_intent(AsyncMock(), "two-controls", "farm")
        assert bridge._autonomy.state("farm").paused is paused
        if "bedtime" in actions:
            assert bridge._profile_store.get("farm")["profile"]["bedtime"] == 2300
        assert not list((tmp_path / "data" / "life-controls").glob("*.json"))
        if paused:
            with pytest.raises(ToolError, match="WORK_STOPPED"):
                await server.call_tool("manage_arrangements", {"change": {"manage_milestones": {"action": "propose", "title": "迟到派工", "preparation": ["water"]}}})
            await server.call_tool("manage_arrangements", {"change": {"manage_companion": {"action": "resume"}}})
            await bridge._apply_life_intent(AsyncMock(), "two-controls", "farm")
            assert not bridge._autonomy.state("farm").paused
    asyncio.run(scenario())


def test_new_player_control_during_ack_does_not_resurrect_old_lease(tmp_path, monkeypatch):
    async def scenario():
        bridge, server = setup(tmp_path, monkeypatch)
        await server.call_tool("manage_arrangements", {"change": {"manage_companion": {"action": "resume"}}})
        await server.call_tool("manage_arrangements", {"change": {"manage_companion": {"action": "bedtime", "bedtime": 2300}}})
        async def new_player_action(*args, **kwargs):
            bridge._life_turn_lease(None, "farm")
        bridge._handle_autonomy_control = AsyncMock(side_effect=new_player_action)
        await bridge._apply_life_intent(AsyncMock(), "two-controls", "farm")
        lease = json.loads((tmp_path / "data" / "life-turn.json").read_text())
        assert lease["requestId"] is None
        assert (bridge._profile_store.get("farm").get("profile") or {}).get("bedtime", 2400) == 2400
    asyncio.run(scenario())


@pytest.mark.parametrize("stop_applied", [False, True])
def test_paused_arrangement_can_be_revised_without_resuming_old_work(tmp_path, monkeypatch, stop_applied):
    async def scenario():
        bridge, server = setup(tmp_path, monkeypatch)
        monkeypatch.setenv("STARDEW_LIFE_MODE", "chat")
        _, proposed = await server.call_tool("manage_arrangements", {"change": {"manage_milestones": {
            "action": "propose", "title": "照料原田", "target_date": "1:spring:2", "preparation": ["harvest", "water"],
        }}})
        node_id = proposed["node"]["id"]
        _, adopted = await server.call_tool("manage_arrangements", {"change": {"manage_milestones": {"action": "adopt", "node_id": node_id}}})
        await server.call_tool("manage_arrangements", {"change": {"manage_companion": {"action": "pause"}}})
        if stop_applied:
            await bridge._apply_life_intent(AsyncMock(), "two-controls", "farm")
        _, revised = await server.call_tool("manage_arrangements", {"change": {"manage_milestones": {
            "action": "revise", "node_id": node_id, "preparation": ["water"], "summary": "今天只浇缺水幼苗",
        }}})
        assert revised["node"]["summary"] == "今天只浇缺水幼苗"
        state = WorkStore(tmp_path / "data/work-state.json").state("farm")
        assert state.goals[0].id == adopted["goalId"]
        assert state.goals[0].constraints["objectiveScope"]["preparation"] == ["water"]
        await bridge._apply_life_intent(AsyncMock(), "two-controls", "farm")
        assert bridge._autonomy.state("farm").paused
        assert WorkStore(tmp_path / "data/work-state.json").state("farm").paused
        with pytest.raises(ToolError, match="WORK_STOPPED"):
            await server.call_tool("manage_arrangements", {"change": {"manage_milestones": {"action": "propose", "title": "新工作"}}})
        bridge._life_turn_lease(None, "farm")
        with pytest.raises(ToolError, match="STALE_CONVERSATION"):
            await server.call_tool("manage_arrangements", {"change": {"manage_milestones": {"action": "revise", "node_id": node_id, "summary": "过期修改"}}})
    asyncio.run(scenario())
