import asyncio
import base64
import hashlib
import json
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.game_tools import GAME_TOOLS
from stardew_ai_runtime.mcp_server import create_mcp_server


def test_selected_reference_pixels_and_source_reach_existing_consult_only(tmp_path):
    data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
    root = tmp_path / "config/reference-images"
    root.mkdir(parents=True)
    (root / "inspiration.png").write_bytes(data)
    entry = dict(id="inspiration", title="Example", author="Creator", source="https://example.test/farm",
                 file="inspiration.png", sha256=hashlib.sha256(data).hexdigest())
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"images": [entry]}), encoding="utf-8")
    async def run():
        game = create_mcp_server(run_dir=tmp_path, scheduler=MagicMock(), surface="light")
        assert {tool.name for tool in await game.list_tools()} == GAME_TOOLS
        catalog = await game.call_tool("consult", {"request": {"read_reference_image": {}}})
        assert len(catalog.content) == 1 and catalog.structuredContent["images"][0]["source"] == entry["source"]
        shown = await game.call_tool("consult", {"request": {"read_reference_image": {"image_id": "inspiration"}}})
        assert base64.b64decode(shown.content[0].data) == data
        assert shown.content[0].type == "image"
        assert entry["source"] in shown.content[1].text
        with pytest.raises(ToolError, match="Unknown reference"):
            await game.call_tool("consult", {"request": {"read_reference_image": {"image_id": "../private"}}})
        (root / "inspiration.png").write_bytes(data + b"changed")
        with pytest.raises(ToolError, match="hash"):
            await game.call_tool("consult", {"request": {"read_reference_image": {"image_id": "inspiration"}}})
        entry["file"] = "../../private.png"
        manifest.write_text(json.dumps({"images": [entry]}), encoding="utf-8")
        with pytest.raises(ToolError, match="inside its manifest"):
            await game.call_tool("consult", {"request": {"read_reference_image": {"image_id": "inspiration"}}})
        manifest.unlink()
        absent = await game.call_tool("consult", {"request": {"read_reference_image": {}}})
        assert absent.structuredContent["images"] == []
    asyncio.run(run())
