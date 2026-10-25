from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.work_state import WorkStore


def _grant_decision(tmp_path: Path, scheduler, token: str = "decision-1") -> WorkStore:
    """Begin a fresh provider decision so guarded native tools may select one short job."""
    scheduler.run_dir = str(tmp_path)
    store = WorkStore(tmp_path / "data" / "work-state.json")
    store.begin_decision("mock-save-123", token)
    return store


@pytest.fixture
def mock_scheduler():
    """Provides a mocked CompanionScheduler returning canonical envelopes and data."""
    scheduler = MagicMock()
    scheduler.latest_world_revision = 1
    scheduler.wait_for_fresh_snapshot = AsyncMock(return_value=(None, False))

    scheduler.get_work_overview = AsyncMock(return_value={
        "farmWork": {
            "matureCropCount": 1,
            "tilledUnwateredCount": 2,
            "isTruncated": False,
            "missing": False,
        },
        "companion": {
            "freeSlots": 11,
            "capacity": 12,
            "nonToolItems": [
                {"itemId": "(O)24", "name": "Parsnip", "stack": 3, "quality": 0}
            ],
            "activity": "idle",
            "stamina": 270.0,
            "waterCanLevel": 40,
            "missing": False,
        },
        "chests": [
            {
                "tile": {"x": 70, "y": 12},
                "capacity": 36,
                "freeSlots": 35,
                "itemCount": 1,
                "items": [
                    {"itemId": "(O)24", "name": "Parsnip", "stack": 5, "quality": 0}
                ],
                "matchingItems": [
                    {"itemId": "(O)24", "name": "Parsnip", "stack": 5, "quality": 0}
                ],
                "hasDuplicateStacks": False,
                "hasMergeableStacks": False,
                "canDeposit": True,
                "fresh": True,
            }
        ],
        "chestsTruncated": False,
        "isTruncated": False,
        "worldRevision": 1,
        "saveId": "mock-save-123",
        "gameSessionId": "mock-session-123",
    })

    scheduler.get_status = AsyncMock(return_value={
        "companion": {
            "locationId": "Farm",
            "tileX": 64,
            "tileY": 15,
            "facingDirection": 2,
            "stamina": 270.0,
            "maxStamina": 270.0,
            "waterCanLevel": 40,
            "maxWaterCanLevel": 40,
            "hasWateringCan": True,
            "activity": "idle",
            "currentTask": None,
        },
        "world": {
            "currentLocation": "Farm",
            "timeOfDay": 600,
            "season": "spring",
            "dayOfMonth": 1,
            "isRaining": False,
        },
        "farmWork": {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}],
            "tilledUnwateredCount": 1,
            "isTruncated": False,
            "matureCropCount": 0,
        },
        "saveId": "mock-save-123",
        "gameSessionId": "mock-session-123",
        "worldRevision": 1,
    })

    scheduler.query_farm_work = AsyncMock(return_value={
        "farmWork": {
            "tilledUnwateredTiles": [{"x": 64, "y": 15}, {"x": 65, "y": 15}],
            "tilledUnwateredCount": 2,
            "isTruncated": False,
            "matureCropCount": 1,
        },
        "companion": {"locationId": "Farm", "tileX": 64, "tileY": 15, "stamina": 270.0},
        "world": {"currentLocation": "Farm", "timeOfDay": 600, "season": "spring"},
        "saveId": "mock-save-123",
        "gameSessionId": "mock-session-123",
        "worldRevision": 1,
    })

    scheduler.execute_water_zone = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 1,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [{"tile": {"x": 64, "y": 15}, "state": "watered"}],
        "details": None,
        "error": None,
    })

    scheduler.water_auto = AsyncMock(return_value={
        "status": "executed",
        "terminalState": "succeeded",
        "completedCount": 2,
        "skippedCount": 0,
        "failedCount": 0,
        "targetTiles": [{"x": 64, "y": 15}, {"x": 65, "y": 15}],
        "targetCount": 2,
        "remainingUnwateredCount": 0,
        "isTruncated": False,
        "effects": [{"tile": {"x": 64, "y": 15}, "state": "watered"}],
        "details": None,
        "error": None,
    })

    scheduler.pause_task = AsyncMock(return_value={
        "status": "paused",
        "taskId": "task-test-1",
        "message": "Task paused.",
    })
    scheduler.resume_task = AsyncMock(return_value={
        "status": "resumed",
        "taskId": "task-test-1",
        "message": "Task resumed.",
    })
    scheduler.cancel_task = AsyncMock(return_value={
        "status": "cancelling",
        "taskId": "task-test-1",
        "message": "Task cancelling.",
    })

    scheduler.query_inventory = AsyncMock(return_value={
        "inventory": {
            "capacity": 12,
            "freeSlots": 11,
            "slots": [
                {"index": 0, "itemId": "(O)24", "name": "Parsnip", "stack": 3, "quality": 0}
            ],
        },
        "saveId": "mock-save-123",
        "gameSessionId": "mock-session-123",
        "worldRevision": 1,
    })

    scheduler.query_chests = AsyncMock(return_value={
        "chests": [
            {
                "tile": {"x": 70, "y": 12},
                "capacity": 36,
                "freeSlots": 35,
                "contents": [
                    {"slot": 0, "itemId": "(O)24", "name": "Parsnip", "stack": 5, "quality": 0}
                ],
            }
        ],
        "isTruncated": False,
        "saveId": "mock-save-123",
        "gameSessionId": "mock-session-123",
        "worldRevision": 1,
    })

    scheduler.harvest_auto = AsyncMock(return_value={
        "status": "executed",
        "terminalState": "succeeded",
        "completedCount": 2,
        "skippedCount": 0,
        "failedCount": 0,
        "targetTiles": [{"x": 64, "y": 16}, {"x": 65, "y": 16}],
        "targetCount": 2,
        "remainingMatureCount": 0,
        "isTruncated": False,
        "effects": [{"tile": {"x": 64, "y": 16}, "state": "harvested"}],
        "details": None,
        "error": None,
    })

    scheduler.deposit_to_chest = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 2,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [
            {
                "state": "deposited",
                "itemId": "(O)24",
                "itemName": "Parsnip",
                "stack": 3,
                "quality": 0,
                "chestTile": {"x": 70, "y": 12},
            }
        ],
        "details": None,
        "error": None,
    })

    scheduler.withdraw_from_chest = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 1,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [
            {
                "state": "withdrawn",
                "itemId": "(O)CarrotSeeds",
                "itemName": "Carrot Seeds",
                "stack": 3,
                "quality": 0,
                "chestTile": {"x": 70, "y": 12},
            }
        ],
        "details": {
            "action": "withdraw",
            "withdrawnItems": [{"itemId": "(O)CarrotSeeds", "count": 3}],
        },
        "error": None,
    })

    scheduler.organize_chest = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 1,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [
            {
                "state": "merged",
                "itemId": "(O)24",
                "itemName": "Parsnip",
                "quality": 0,
                "mergedStack": 8,
                "chestTile": {"x": 70, "y": 12},
            }
        ],
        "details": None,
        "error": None,
    })

    scheduler.query_planting_options = AsyncMock(return_value={
        "season": "spring",
        "dayOfMonth": 1,
        "companionHasHoe": True,
        "seeds": [
            {
                "itemId": "(O)472",
                "name": "Parsnip Seeds",
                "stack": 15,
                "canPlantCurrentSeason": True,
                "seasons": ["spring"],
            }
        ],
        "candidateTiles": {
            "tilledEmptyCount": 6,
            "tilledEmptyTiles": [{"x": 64, "y": 14}],
            "tilledEmptyTruncated": False,
            "tillableCount": 20,
            "tillableTiles": [{"x": 65, "y": 14}],
            "tillableTruncated": False,
        },
        "searchBounds": {
            "center": {"x": 64, "y": 15},
            "radius": 15,
        },
        "worldRevision": 1,
        "saveId": "mock-save-123",
        "gameSessionId": "mock-session-123",
    })

    scheduler.execute_hoe_tiles = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 2,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [
            {"tile": {"x": 64, "y": 14}, "state": "hoed"},
            {"tile": {"x": 65, "y": 14}, "state": "hoed"},
        ],
        "details": {
            "hoedTiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}],
        },
        "error": None,
    })

    scheduler.execute_plant_seeds = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 1,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [
            {"tile": {"x": 64, "y": 14}, "state": "planted", "itemId": "(O)472", "stack": 14},
        ],
        "details": {
            "plantedTiles": [{"x": 64, "y": 14}],
            "remainingSeedStack": 14,
            "seedItemId": "(O)472",
            "outOfSeeds": False,
        },
        "error": None,
    })

    scheduler.query_shop = AsyncMock(return_value={
        "shopId": "SeedShop",
        "status": "ok",
        "isOpen": True,
        "ownerPresent": True,
        "closedMessage": None,
        "owners": ["Pierre"],
        "currency": 0,
        "availableMoney": 1000,
        "moneyStatus": "ok",
        "itemsCount": 1,
        "items": [
            {
                "itemId": "(O)472",
                "name": "Parsnip Seeds",
                "price": 20,
                "stock": 2147483647,
                "isInfiniteStock": True,
            }
        ],
        "worldRevision": 1,
        "saveId": "mock-save-123",
        "gameSessionId": "mock-session-123",
    })

    scheduler.execute_ship_items = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 2,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [
            {
                "state": "shipped",
                "itemId": "(O)24",
                "count": 2,
                "estimatedUnitValue": 35,
                "estimatedTotalValue": 70,
                "remainingBackpackCount": 2,
            }
        ],
        "details": {
            "shippedItems": [
                {
                    "itemId": "(O)24",
                    "count": 2,
                    "estimatedUnitValue": 35,
                    "estimatedTotalValue": 70,
                    "remainingBackpackCount": 2,
                }
            ],
            "estimatedTotalValue": 70,
            "estimatedValue": 70,
            "shippingBinTotalCount": 2,
            "remainingBackpackCounts": {"(O)24": 2},
            "note": "Estimated value only; funds will be settled overnight by the game engine.",
        },
        "error": None,
    })

    scheduler.execute_navigate_to = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 1,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [
            {
                "location": "Farm",
                "pathLength": 15,
                "exitWarp": {
                    "sourceTile": {"x": 79, "y": 15},
                    "targetLocation": "BusStop",
                    "targetTile": {"x": 0, "y": 23},
                    "kind": "Warp",
                },
            },
            {
                "location": "BusStop",
                "pathLength": 20,
                "exitWarp": {
                    "sourceTile": {"x": 35, "y": 23},
                    "targetLocation": "Town",
                    "targetTile": {"x": 0, "y": 55},
                    "kind": "Warp",
                },
            },
            {
                "location": "Town",
                "pathLength": 30,
            },
        ],
        "details": {
            "visitedLocations": ["Farm", "BusStop", "Town"],
            "finalLocation": "Town",
            "finalTile": {"x": 43, "y": 58},
            "targetAdjusted": False,
            "requestedTile": {"x": 43, "y": 58},
            "hopCount": 2,
        },
        "error": None,
    })

    scheduler.execute_purchase_items = AsyncMock(return_value={
        "terminalState": "succeeded",
        "completedCount": 2,
        "skippedCount": 0,
        "failedCount": 0,
        "effects": [
            {
                "state": "purchased",
                "itemId": "(O)472",
                "count": 2,
                "unitPrice": 20,
                "subtotal": 40,
            }
        ],
        "details": {
            "shopId": "SeedShop",
            "totalCost": 40,
            "budgetLimit": 100,
            "remainingBudget": 60,
            "availableMoneyAfter": 460,
            "purchasedItems": [
                {
                    "itemId": "(O)472",
                    "count": 2,
                    "unitPrice": 20,
                    "subtotal": 40,
                }
            ],
            "skippedItems": [],
            "skipReason": None,
            "rollbackPerformed": False,
            "rollbackDetails": None,
        },
        "error": None,
    })

    scheduler.plant_crop_workflow = AsyncMock(return_value={
        "status": "succeeded",
        "crop": "Parsnip",
        "seedId": "(O)472",
        "seedsPlanted": 3,
        "tilesHoed": 3,
        "tilesWatered": 3,
        "targetTiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}, {"x": 66, "y": 14}],
        "withdrawnFromChest": None,
        "remainingSeedsInBackpack": 0,
        "pendingDecision": None,
        "effects": [{"tile": {"x": 64, "y": 14}, "state": "planted"}],
    })

    return scheduler
