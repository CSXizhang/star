"""Exact chest quantities survive the scheduler, native wire and discovery contract."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from stardew_ai_runtime.client import TransportClient
from stardew_ai_runtime.scheduler import CompanionScheduler, PolicyViolationError


def test_exact_deposit_quantity_reaches_native_wire():
    async def run():
        native = TransportClient("127.0.0.1", 1)
        native.save_id = "farm"
        native.game_session_id = "session"
        native.send_envelope = AsyncMock()
        scheduler = CompanionScheduler(client=native)

        async def dispatch_only(**kwargs):
            return await kwargs["dispatch"](native, task_id="test",
                                            idempotency_key="test-key", expires_seconds=30)

        scheduler.execute_skill = dispatch_only
        await scheduler.deposit_to_chest(66, 20, items=[{"itemId": "(O)24", "count": 2}])
        envelope = native.send_envelope.call_args.args[0].to_mapping()
        assert envelope["payload"]["parameters"]["items"] == [{"itemId": "(O)24", "count": 2}]
        assert "itemIds" not in envelope["payload"]["parameters"]
        schema = json.loads((Path(__file__).resolve().parents[2] / "protocol/schemas/protocol-v0.1.schema.json").read_text(encoding="utf-8"))
        assert not list(Draft202012Validator(schema).iter_errors(envelope))

    asyncio.run(run())


@pytest.mark.parametrize("items", [[], [{"itemId": "24", "count": 0}], [{"itemId": "24", "count": True}]])
def test_exact_deposit_invalid_quantity_never_dispatches(items):
    async def run():
        scheduler = CompanionScheduler(client=TransportClient())
        scheduler.execute_skill = AsyncMock()
        with pytest.raises(PolicyViolationError):
            await scheduler.deposit_to_chest(66, 20, items=items)
        scheduler.execute_skill.assert_not_called()
        with pytest.raises(PolicyViolationError, match="either"):
            await scheduler.deposit_to_chest(66, 20, item_ids=["24"], items=[{"itemId": "24", "count": 2}])
        scheduler.execute_skill.assert_not_called()

    asyncio.run(run())
