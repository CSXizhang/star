"""Regression for the two-item deposit rejected during live harvest logistics."""

import asyncio
from unittest.mock import AsyncMock

from support.mcp_scheduler import _grant_decision
from support.mcp_scheduler import mock_scheduler as _scheduler_fixture

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import CompanionScheduler

mock_scheduler = _scheduler_fixture


def test_live_two_item_deposit_survives_actual_mcp_model_parsing(tmp_path, monkeypatch, mock_scheduler):
    store = _grant_decision(tmp_path, mock_scheduler)
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision-1")
    monkeypatch.setenv("STARDEW_MCP_WIRE_STYLE", "mcode")
    items = [{"count": 3, "itemId": "(O)388"}, {"count": 5, "itemId": "(O)24"}]

    async def native_boundary(**kwargs):
        # Use the real scheduler validator; a permissive AsyncMock concealed
        # the live failure caused by nested ItemQuantity instances.
        normalized = CompanionScheduler._validate_withdraw_items(items=kwargs["items"])
        assert normalized == items
        return {"status": "completed", "terminalState": "succeeded", "completedCount": 8}

    mock_scheduler.deposit_to_chest = AsyncMock(side_effect=native_boundary)

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        _, result = await server.call_tool("submit_work", {"action": {"deposit_to_chest": {
            "chest_x": 65, "chest_y": 37, "items": items, "location_id": "Farm"}}})
        assert result["terminalState"] == "succeeded"
        mock_scheduler.deposit_to_chest.assert_awaited_once()
        assert mock_scheduler.deposit_to_chest.call_args.kwargs["items"] == items
        assert not store.state("mock-save-123").tasks

    asyncio.run(run())
