"""mcode consumes structured results; preserve other MCP clients and errors."""

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp import types
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.mcp_server import create_mcp_server


def make_server(tmp_path, monkeypatch, style):
    monkeypatch.setenv("STARDEW_MCP_WIRE_STYLE", style)
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="light")

    @server.tool()
    def observation() -> dict[str, Any]:
        return {"water": {"current": 1, "capacity": 40}, "quantity": 19}

    @server.tool()
    def failed() -> dict[str, Any]:
        raise ToolError("native result unknown; do not replay")

    return server


@pytest.mark.parametrize("style", ["mcode", "", "codex", "kimi"])
def test_structured_fact_has_one_model_representation_for_mcode_only(tmp_path, monkeypatch, style):
    server = make_server(tmp_path, monkeypatch, style)

    async def run():
        content, data = await server.call_tool("observation", {})
        assert data == {"water": {"current": 1, "capacity": 40}, "quantity": 19}
        if style == "mcode":
            assert content == []
        else:
            assert len(content) == 1 and json.loads(content[0].text) == data
        # Exercise the SDK wire boundary, including its output-schema validation.
        result = await server._mcp_server.request_handlers[types.CallToolRequest](
            types.CallToolRequest(params=types.CallToolRequestParams(name="observation", arguments={})))
        assert result.root.structuredContent == data
        assert result.root.isError is False
        assert bool(result.root.content) is (style != "mcode")
        assert next(t for t in await server.list_tools() if t.name == "observation").outputSchema

    asyncio.run(run())


def test_mcode_keeps_nonduplicate_text_images_and_error_semantics(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch, "mcode")
    data = {"quantity": 19}
    image = types.ImageContent(type="image", data="AA==", mimeType="image/png")
    warning = types.TextContent(type="text", text="Some quantities are unconfirmed")
    other_json = types.TextContent(type="text", text='{"quantity": null}')
    annotated = types.TextContent(type="text", text=json.dumps(data), annotations=types.Annotations(priority=1))

    from unittest.mock import AsyncMock
    server._tool_manager.call_tool = AsyncMock(return_value=(
        [types.TextContent(type="text", text=json.dumps(data)), warning, other_json, image, annotated], data))

    async def mixed():
        content, structured = await server.call_tool("observation", {})
        assert content == [warning, other_json, image, annotated]
        assert structured == data

    asyncio.run(mixed())
    server = make_server(tmp_path, monkeypatch, "mcode")

    async def errors():
        with pytest.raises(ToolError, match="native result unknown"):
            await server.call_tool("failed", {})
        result = await server._mcp_server.request_handlers[types.CallToolRequest](
            types.CallToolRequest(params=types.CallToolRequestParams(name="failed", arguments={})))
        assert result.root.isError is True
        assert "native result unknown" in result.root.content[0].text
        assert result.root.structuredContent is None

    asyncio.run(errors())
