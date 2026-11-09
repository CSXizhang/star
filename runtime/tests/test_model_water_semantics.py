"""Model observation entry points agree on water quantity and observed tool grade."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.decision_context import (
    build_compact_summary,
    build_decision_context,
    water_can_resources,
)
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
                _, result = await server.call_tool(tool, {"detail": detail}) if tool == "get_status" else await server.call_tool("query_game", {"query": {tool: {"detail": detail}}})
                companion = result["companion"]
                assert companion["waterCan"] == expected
                assert "waterCanLevel" not in companion and "maxWaterCanLevel" not in companion
        if quantity is not None:
            life = build_decision_context(payload)["resources"]["companion"]["inventory"]["toolResources"]["waterCan"]
            assert life == expected
        assert expected["currentWater"] == quantity
        assert expected["upgradeLevel"] is None
    asyncio.run(run())


def test_observed_tool_grades_reach_context_and_status_without_using_other_revisions(tmp_path):
    async def run():
        native = {"waterCanLevel": 40, "maxWaterCanLevel": 40}
        inventory = {"slots": [
            {"itemId": "(T)Hoe", "name": "Hoe", "isTool": True, "upgradeLevel": 0},
            {"itemId": "(T)Axe", "name": "Axe", "isTool": True, "upgradeLevel": 4},
            {"itemId": "(T)WateringCan", "name": "Watering Can", "isTool": True, "upgradeLevel": 2},
            {"itemId": "(T)Pickaxe", "name": "Pickaxe", "isTool": True},
            {"itemId": "(O)24", "name": "Parsnip", "quality": 4, "stack": 1},
        ]}
        payload = {"companion": native, "inventory": inventory}
        context = build_decision_context(payload)
        for observed in (context["inventory"], build_compact_summary(context)["inventory"]):
            assert {tool["itemId"]: tool["upgradeLevel"] for tool in observed["tools"]} == {
                "(T)Hoe": 0, "(T)Axe": 4, "(T)WateringCan": 2, "(T)Pickaxe": None}
            assert observed["toolResources"]["waterCan"]["upgradeLevel"] == 2
        assert "upgradeLevel" not in context["inventory"]["items"][0]
        sched = MagicMock()
        sched.latest_snapshot = {"payload": payload, "saveId": "farm", "worldRevision": 7}
        status = {"companion": native, "saveId": "farm", "worldRevision": 7}
        sched.get_status = AsyncMock(return_value=status)
        sched.get_work_overview = AsyncMock(return_value=status)
        server = create_mcp_server(run_dir=tmp_path, scheduler=sched, surface="light")
        for query in ("get_status", "get_work_overview"):
            _, result = (await server.call_tool("get_status", {}) if query == "get_status"
                         else await server.call_tool("query_game", {"query": {query: {}}}))
            assert result["companion"]["waterCan"]["upgradeLevel"] == 2
        sched.get_status.return_value = {**status, "worldRevision": 8}
        _, result = await server.call_tool("get_status", {})
        assert result["companion"]["waterCan"]["upgradeLevel"] is None
        assert result["companion"]["waterCan"]["currentWater"] == 40
    asyncio.run(run())
