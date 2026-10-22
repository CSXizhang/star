import asyncio
from unittest.mock import AsyncMock, MagicMock

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import CompanionScheduler


def test_farm_animals_are_observed_even_when_snapshot_is_from_shop():
    async def check():
        scheduler = CompanionScheduler.__new__(CompanionScheduler)
        scheduler._latest_snapshot_data = {"saveId": "farm", "payload": {"companion": {"location": "AnimalShop"},
                                          "livestock": {"roamingAnimals": [], "roamingScopeLocationId": "AnimalShop"}}}
        animals = [{"animalId": str(i), "location": "Farm", "fullness": 255} for i in range(10)]
        native = {"locationId": "Farm", "roamingScopeLocationId": "Farm", "roamingAnimals": animals,
                  "buildings": [], "unobservedResidentIds": [], "capturedRevision": 12}
        scheduler._execute_native_action = AsyncMock(return_value={"details": {"livestock": native}})
        proxy = MagicMock()
        proxy.query_livestock = scheduler.query_livestock
        server = create_mcp_server(scheduler=proxy, surface="light")
        _, result = await server.call_tool("call_capability", {"tool": "observe_livestock", "params": {"location_id": "Farm"}})
        observed = result["result"]
        assert observed["animalCount"] == 10 and observed["roamingScopeLocationId"] == "Farm"
        assert observed["worldRevision"] == 12
        scheduler._execute_native_action.assert_awaited_once_with("inspect-livestock", {"locationId": "Farm"}, tiles=[])
    asyncio.run(check())
