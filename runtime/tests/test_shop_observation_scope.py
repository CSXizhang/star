import asyncio
from unittest.mock import AsyncMock, MagicMock

from stardew_ai_runtime.protocol import validate_native_action_parameters
from stardew_ai_runtime.scheduler import CompanionScheduler


def test_requested_animal_shop_observes_native_catalog_instead_of_seed_snapshot():
    async def check():
        scheduler = CompanionScheduler.__new__(CompanionScheduler)
        client = MagicMock(world_revision=12, save_id="farm", game_session_id="session")
        scheduler.ensure_connected = AsyncMock(return_value=client)
        scheduler._refresh_snapshot = AsyncMock()
        scheduler._latest_snapshot_data = {"payload": {"shop": {"shopId": "SeedShop", "items": []}}}
        native = {"shopId": "AnimalShop", "status": "ok", "isOpen": True,
                  "ownerPresent": True, "availableMoney": 36710, "moneyStatus": "ok",
                  "locationId": "AnimalShop", "interactionTile": {"x": 10, "y": 15},
                  "items": [{"itemId": "(O)178", "name": "Hay", "price": 50,
                             "stock": 2147483647, "isInfiniteStock": True, "isSeed": False}]}
        scheduler._execute_native_action = AsyncMock(return_value={"details": {"shop": native}})
        result = await scheduler.query_shop("AnimalShop", item_id="(O)178")
        assert result["shopId"] == "AnimalShop" and result["isOpen"]
        assert result["items"][0]["price"] == 50
        assert result["locationId"] == "AnimalShop"
        scheduler._execute_native_action.assert_awaited_once_with("inspect-shop", {"locationId": "AnimalShop", "shopId": "AnimalShop"}, tiles=[])
        validate_native_action_parameters("inspect-shop", {"locationId": "AnimalShop", "shopId": "AnimalShop"})
    asyncio.run(check())
