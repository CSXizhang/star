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
def test_core_is_shared_and_only_repeated_for_a_new_session(tmp_path, mode):
    from stardew_ai_runtime.prompt_delivery import PromptDelivery
    core = runtime_instructions()
    service = LifeChatService("fake", {}, tmp_path / "sessions.json", {}, tmp_path / "fingerprints.json")
    prompt = service.build_turn_prompt("farm", None, None, None, None,
        mode=mode, milestones=[], live_context={})
    delivery = PromptDelivery(tmp_path / "delivery.json")
    sent, pending = delivery.prepare("fake:farm", None, prompt, core)
    assert sent.count(core) == 1
    delivery.acknowledge("fake:farm", pending, "cid")
    sent, _ = delivery.prepare("fake:farm", "cid", prompt, core)
    assert core not in sent and "实时上下文：" in sent
    assert delivery.prepare("fake:farm", "new", prompt, core)[0].count(core) == 1
