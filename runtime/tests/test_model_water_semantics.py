"""All model observation entry points agree on water quantity and unknown tool grade."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.decision_context import build_decision_context, water_can_resources
from stardew_ai_runtime.mcp_server import create_mcp_server


@pytest.mark.parametrize("quantity", [0, 1, 40, None])
def test_execution_status_and_overview_match_life_water_observation(tmp_path, quantity):
    async def run():
        native = {"waterCanLevel": quantity, "maxWaterCanLevel": 40}
        payload = {"companion": native, "world": {"year": 1, "season": "spring", "dayOfMonth": 2}}
        sched = MagicMock()
        sched.latest_snapshot = {"payload": payload, "saveId": "farm", "worldRevision": 7}
        sched.get_status = AsyncMock(return_value={"companion": native, "saveId": "farm", "worldRevision": 7})
        sched.get_work_overview = AsyncMock(return_value={"companion": {"waterCanLevel": quantity}, "saveId": "farm", "worldRevision": 7})
        server = create_mcp_server(run_dir=tmp_path, scheduler=sched, surface="light")
        expected = water_can_resources(native)
        for tool in ("get_status", "get_work_overview"):
            for detail in (False, True):
                _, result = await server.call_tool(tool, {"detail": detail})
                companion = result["companion"]
                assert companion["waterCan"] == expected
                assert "waterCanLevel" not in companion and "maxWaterCanLevel" not in companion
        if quantity is not None:
            life = build_decision_context(payload)["resources"]["companion"]["inventory"]["toolResources"]["waterCan"]
            assert life == expected
        assert expected["currentWater"] == quantity
        assert expected["upgradeLevel"] is None
    asyncio.run(run())
