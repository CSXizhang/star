import asyncio
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.client import TransportClient
from stardew_ai_runtime.scheduler import CompanionScheduler


@pytest.mark.parametrize("include_empty", [False, True])
def test_zone_selection_reaches_the_native_wire_without_being_dropped(include_empty):
    async def check():
        client = TransportClient()
        client.save_id = "farm"
        client.game_session_id = "test-session"
        client.send_envelope = AsyncMock()
        scheduler = CompanionScheduler(client=client)

        async def dispatch_skill(**kwargs):
            return await kwargs["dispatch"](client, task_id="selection",
                idempotency_key="selection-1", expires_seconds=30)

        scheduler.execute_skill = AsyncMock(side_effect=dispatch_skill)
        await scheduler.execute_water_zone(10, 12, radius=1, include_empty_tiles=include_empty)
        envelope = client.send_envelope.await_args.args[0]
        assert envelope.payload["parameters"]["includeEmptyTiles"] is include_empty
        assert len(envelope.payload["parameters"]["tiles"]) == 9

    asyncio.run(check())
