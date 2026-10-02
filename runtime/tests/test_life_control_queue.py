"""Multiple tool controls in one sentence must be durable and ordered."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
    monkeypatch.setenv("STARDEW_LIFE_TURN_ID", "two-controls")
    monkeypatch.setenv("STARDEW_LIFE_SAVE_ID", "farm")
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._life_turn_lease("two-controls", "farm")
    (tmp_path / "data" / "life-snapshot.json").write_text(json.dumps({
        "saveId": "farm", "capturedAt": time.time(), "payload": {"world": {"dayOfMonth": 2}},
    }), encoding="utf-8")
    return bridge, create_mcp_server(run_dir=tmp_path, surface="life")


@pytest.mark.parametrize("actions,paused", [(["bedtime", "resume"], False),
                                          (["resume", "pause"], True),
                                          (["pause", "bedtime"], True)])
def test_two_real_tool_commits_are_consumed_in_order(tmp_path, monkeypatch, actions, paused):
    async def scenario():
        bridge, server = setup(tmp_path, monkeypatch)
        bridge._autonomy.set_paused("farm", True)
        for action in actions:
            await server.call_tool("manage_companion", {"action": action, **({"bedtime": 2300} if action == "bedtime" else {})})
        assert len(list((tmp_path / "data" / "life-controls").glob("*.json"))) == 2
        if paused:
            with pytest.raises(ToolError, match="WORK_STOPPED"):
                await server.call_tool("manage_milestones", {"action": "propose", "title": "抢在暂停前派工", "preparation": ["water"]})
        await bridge._apply_life_intent(AsyncMock(), "two-controls", "farm")
        assert bridge._autonomy.state("farm").paused is paused
        if "bedtime" in actions:
            assert bridge._profile_store.get("farm")["profile"]["bedtime"] == 2300
        assert not list((tmp_path / "data" / "life-controls").glob("*.json"))
        if paused:
            with pytest.raises(ToolError, match="WORK_STOPPED"):
                await server.call_tool("manage_milestones", {"action": "propose", "title": "迟到派工", "preparation": ["water"]})
            await server.call_tool("manage_companion", {"action": "resume"})
            await bridge._apply_life_intent(AsyncMock(), "two-controls", "farm")
            assert not bridge._autonomy.state("farm").paused
    asyncio.run(scenario())


def test_new_player_control_during_ack_does_not_resurrect_old_lease(tmp_path, monkeypatch):
    async def scenario():
        bridge, server = setup(tmp_path, monkeypatch)
        await server.call_tool("manage_companion", {"action": "resume"})
        await server.call_tool("manage_companion", {"action": "bedtime", "bedtime": 2300})
        async def new_player_action(*args, **kwargs):
            bridge._life_turn_lease(None, "farm")
        bridge._handle_autonomy_control = AsyncMock(side_effect=new_player_action)
        await bridge._apply_life_intent(AsyncMock(), "two-controls", "farm")
        lease = json.loads((tmp_path / "data" / "life-turn.json").read_text())
        assert lease["requestId"] is None
        assert (bridge._profile_store.get("farm").get("profile") or {}).get("bedtime", 2400) == 2400
    asyncio.run(scenario())
