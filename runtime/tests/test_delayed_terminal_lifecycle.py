import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.scheduler import CompanionScheduler


@pytest.mark.parametrize("terminal,matching,cleared", [
    ("succeeded", True, True), ("cancelled", True, True),
    ("running", True, False), ("succeeded", False, False),
])
def test_reconcile_closes_only_the_confirmed_matching_native_task(terminal, matching, cleared):
    async def check():
        client = MagicMock(save_id="farm")
        client.get_cached_result.return_value = SimpleNamespace(payload={"commandId": "nav", "terminalState": terminal})
        scheduler = CompanionScheduler(client=client)
        scheduler.ensure_connected = AsyncMock(return_value=client)
        scheduler._settle_free_mode_purchase = MagicMock()
        scheduler._active_task = SimpleNamespace(command_id="nav" if matching else "other",
                                                status="running", is_terminal=False)
        await scheduler.reconcile_command("nav")
        assert (scheduler.active_task is None) is cleared
        if not cleared:
            assert scheduler.has_active_task
    asyncio.run(check())
