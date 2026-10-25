from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.work_state import WorkStore


@pytest.fixture
def game(tmp_path, monkeypatch):
    for key in ("STARDEW_MCP_SURFACE", "STARDEW_MCP_FULL", "STARDEW_EXTERNAL_CODEX",
                "STARDEW_LIFE_MODE", "STARDEW_TURN_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision")
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.latest_snapshot = {}
    scheduler.get_status = AsyncMock(return_value={"saveId": "save"})

    async def withdraw(*, command_id=None, **params):
        CompanionScheduler._validate_chest_coords(params["chest_x"], params["chest_y"])
        items = CompanionScheduler._validate_withdraw_items(
            items=params.get("items"), item_id=params.get("item_id"), count=params.get("count", 1)
        )
        return {"status": "executed", "terminalState": "succeeded", "completedCount": 3,
                "effects": [{"state": "completed", "items": items}]}

    scheduler.withdraw_from_chest = create_autospec(withdraw, side_effect=withdraw)
    store = WorkStore(tmp_path / "data" / "work-state.json")
    store.begin_decision("save", "decision")
    return scheduler, store, create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="full")
