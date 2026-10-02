"""Prompt routing is packaged, read-only and does not repeat core instructions."""
import asyncio
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.agent_instructions import runtime_instructions
from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.life_chat import LifeChatService
from stardew_ai_runtime.mcp_server import create_mcp_server


def test_guidance_is_readable_without_a_game_write_token(monkeypatch):
    monkeypatch.delenv("STARDEW_DECISION_TOKEN", raising=False)
    monkeypatch.delenv("STARDEW_MCP_SURFACE", raising=False)
    async def run():
        game = create_mcp_server(scheduler=MagicMock(), surface="light")
        catalog = await game.call_tool("discover_capabilities", {"group": "knowledge"})
        assert "read_guidance" in str(catalog)
        result = await game.call_tool("call_capability", {
            "tool": "read_guidance", "params": {"topic": "crop-selection"}})
        assert result[1]["result"]["topic"] == "crop-selection"
        assert "净收益" in result[1]["result"]["text"]
        with pytest.raises(ToolError, match="Unknown guidance topic"):
            await game.call_tool("call_capability", {
                "tool": "read_guidance", "params": {"topic": "../../config/auth.json"}})
        life = create_mcp_server(scheduler=MagicMock(), surface="life")
        assert "read_guidance" in {t.name for t in await life.list_tools()}
        await life.call_tool("read_guidance", {"topic": "daily-rhythm"})
    asyncio.run(run())


def test_core_injection_is_shared_and_once(monkeypatch):
    core = runtime_instructions()
    bridge = ChatBridge.__new__(ChatBridge)
    monkeypatch.setattr(bridge, "_decision_context", lambda *a, **k: {})
    assert bridge._format_agent_prompt("继续养鸡", "farm").count(core) == 1
    life = LifeChatService.build_system_prompt(None, None, None, mode="plan")
    assert life.count(core) == 1
    assert "优先已有种子和不花钱" not in life


@pytest.mark.parametrize("mode", ["chat", "plan"])
def test_decision_policy_stays_in_core_and_continuation_only_repeats_flag(tmp_path, mode):
    core = runtime_instructions()
    service = LifeChatService("fake", {}, tmp_path / "sessions.json", {}, tmp_path / "fingerprints.json")
    options = dict(mode=mode, milestones=[], live_context={})
    first = service.build_turn_prompt("farm", None, None, None, None, **options)
    service.mark_prompt_delivered("farm", "cid", mode)
    continuation = service.build_turn_prompt("farm", "cid", None, None, None, **options)
    assert first.count(core) == 1
    # The policy has one source even when life-specific instructions are appended.
    for policy in ("任务物品的指定用途", "已承诺支出", "承担不可恢复的损失",
                   "当前游戏模式的恢复能力", "未经成功回包不宣称"):
        assert first.count(policy) == 1
        assert policy not in continuation
    assert first.count("playerConfirmedDecision") == 1
    assert continuation.count("playerConfirmedDecision") == 1
    assert "false时只继续商量，为true时才提交该事项的决定" in continuation
    assert "真实执行前不宣称" not in first + continuation
    assert "已明确的选择直接记录并安排" not in first + continuation
