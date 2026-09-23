"""Integration and unit tests for Stardew AI FastMCP server.

Tests:
1. Tool registration and schema validation for all 13 tools:
   - get_work_overview
   - get_status
   - query_farm_work
   - query_inventory
   - query_chests
   - water_zone
   - water_auto
   - harvest_auto
   - deposit_to_chest
   - organize_chest
   - pause_task
   - resume_task
   - cancel_task
2. FastMCP in-memory tool execution with mock scheduler.
3. Streamlined default responses vs detail=True responses.
4. Post-action state closing loop (fresh snapshot vs snapshot timeout).
5. Error handling and policy violation mapping into ToolError.
6. Stdio loopback integration test with ClientSession.
   NOTE: Stdio loopback tests use MockModTransportServer and DO NOT constitute real game evidence.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import NoActiveTaskError

# Import MockModTransportServer from test_transport_live
sys.path.insert(0, str(Path(__file__).parent))
from test_transport_live import MockModTransportServer  # noqa: E402


@pytest.fixture(autouse=True)
def _full_tool_surface(monkeypatch):
    """These tests exercise the complete base surface; disclosure is tested separately."""
    monkeypatch.setenv("STARDEW_MCP_FULL", "1")


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


def test_mcp_server_tool_registration(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        tools = await server.list_tools()
        tool_names = [t.name for t in tools]

        assert "get_work_overview" in tool_names
        assert "get_status" in tool_names
        assert "query_farm_work" in tool_names
        assert "query_inventory" in tool_names
        assert "query_chests" in tool_names
        assert "query_planting_options" in tool_names
        assert "water_zone" in tool_names
        assert "water_auto" in tool_names
        assert "harvest_auto" in tool_names
        assert "deposit_to_chest" in tool_names
        assert "organize_chest" in tool_names
        assert "hoe_tiles" in tool_names
        assert "plant_seeds" in tool_names
        assert "pause_task" in tool_names
        assert "resume_task" in tool_names
        assert "cancel_task" in tool_names
        assert "query_shop" in tool_names
        assert "ship_items" in tool_names
        assert "purchase_items" in tool_names
        assert "navigate_to" in tool_names
        assert "withdraw_from_chest" in tool_names
        assert "plant_crop_workflow" in tool_names
        assert "work_plan_overview" in tool_names
        assert "manage_goal" in tool_names
        assert "manage_plan" in tool_names
        assert "manage_todo" in tool_names
        assert "run_next_step" in tool_names
        assert "reconcile_plan_command" in tool_names
        assert "dispatch_plan_operation" in tool_names
        assert "submit_plan" in tool_names
        assert "remember_intent" in tool_names
        assert "harvest_and_store" in tool_names
        assert "discover_capabilities" in tool_names
        assert "call_capability" in tool_names
        # Grouped observation + explicit native farming/husbandry actions.
        assert "observe_farming_helpers" in tool_names
        assert "observe_machines" in tool_names
        assert "observe_livestock" in tool_names
        assert "refill_watering_can" in tool_names
        assert "apply_fertilizer" in tool_names
        assert "clear_debris" in tool_names
        assert "pickup_items" in tool_names
        assert "insert_machine" in tool_names
        assert "collect_machine" in tool_names
        assert "pet_animal" in tool_names
        assert "collect_animal_produce" in tool_names
        assert "feed_animals" in tool_names
        assert "toggle_animal_door" in tool_names
        assert "chop_tree" in tool_names
        assert len(tools) == 51
        assert "autonomy_status" in tool_names
        assert "set_autonomy" in tool_names
        assert "query_wiki" in tool_names

        # Check plant_crop_workflow schema
        workflow_tool = next(t for t in tools if t.name == "plant_crop_workflow")
        assert "count" in workflow_tool.inputSchema["properties"]
        assert "auto_till" in workflow_tool.inputSchema["properties"]
        assert "auto_water" in workflow_tool.inputSchema["properties"]
        assert "withdraw_from_chest" in workflow_tool.inputSchema["properties"]

        # Check withdraw_from_chest schema
        withdraw_tool = next(t for t in tools if t.name == "withdraw_from_chest")
        assert "chest_x" in withdraw_tool.inputSchema["required"]
        assert "chest_y" in withdraw_tool.inputSchema["required"]
        assert "item_id" in withdraw_tool.inputSchema["properties"]
        assert "count" in withdraw_tool.inputSchema["properties"]
        assert "items" in withdraw_tool.inputSchema["properties"]
        assert "location_id" in withdraw_tool.inputSchema["properties"]
        assert withdraw_tool.inputSchema["properties"]["location_id"]["default"] == "Farm"
        assert "detail" in withdraw_tool.inputSchema["properties"]
        assert withdraw_tool.inputSchema["properties"]["detail"]["default"] is False

        # Check purchase_items schema
        purchase_tool = next(t for t in tools if t.name == "purchase_items")
        assert "items" in purchase_tool.inputSchema["properties"]
        assert "budget_limit" in purchase_tool.inputSchema["properties"]
        assert "shop_id" in purchase_tool.inputSchema["properties"]
        assert purchase_tool.inputSchema["properties"]["shop_id"]["default"] == "SeedShop"
        assert "detail" in purchase_tool.inputSchema["properties"]
        assert purchase_tool.inputSchema["properties"]["detail"]["default"] is False
        assert "items" in purchase_tool.inputSchema["required"]
        assert "budget_limit" in purchase_tool.inputSchema["required"]

        # Check navigate_to schema
        nav_tool = next(t for t in tools if t.name == "navigate_to")
        assert "location_id" in nav_tool.inputSchema["properties"]
        assert "detail" in nav_tool.inputSchema["properties"]
        assert nav_tool.inputSchema["properties"]["detail"]["default"] is False

        # Check get_work_overview schema
        overview_tool = next(t for t in tools if t.name == "get_work_overview")
        assert "detail" in overview_tool.inputSchema["properties"]
        assert overview_tool.inputSchema["properties"]["detail"]["default"] is False

        # Check query_planting_options schema
        planting_tool = next(t for t in tools if t.name == "query_planting_options")
        assert "detail" in planting_tool.inputSchema["properties"]
        assert planting_tool.inputSchema["properties"]["detail"]["default"] is False

        # Check query_shop schema
        shop_tool = next(t for t in tools if t.name == "query_shop")
        assert "shop_id" in shop_tool.inputSchema["properties"]
        assert shop_tool.inputSchema["properties"]["shop_id"]["default"] == "SeedShop"
        assert "detail" in shop_tool.inputSchema["properties"]
        assert shop_tool.inputSchema["properties"]["detail"]["default"] is False

        # Check water_auto schema
        water_auto_tool = next(t for t in tools if t.name == "water_auto")
        assert "max_tiles" in water_auto_tool.inputSchema["properties"]
        assert water_auto_tool.inputSchema["properties"]["max_tiles"]["default"] == 25
        assert "detail" in water_auto_tool.inputSchema["properties"]

        # Check water_zone schema
        water_zone_tool = next(t for t in tools if t.name == "water_zone")
        assert "center_x" in water_zone_tool.inputSchema["required"]
        assert "center_y" in water_zone_tool.inputSchema["required"]
        assert "radius" in water_zone_tool.inputSchema["properties"]
        assert "detail" in water_zone_tool.inputSchema["properties"]

        # Check harvest_auto schema
        harvest_auto_tool = next(t for t in tools if t.name == "harvest_auto")
        assert "max_tiles" in harvest_auto_tool.inputSchema["properties"]
        assert harvest_auto_tool.inputSchema["properties"]["max_tiles"]["default"] == 16
        assert "detail" in harvest_auto_tool.inputSchema["properties"]

        # Check deposit_to_chest schema
        deposit_tool = next(t for t in tools if t.name == "deposit_to_chest")
        assert "chest_x" in deposit_tool.inputSchema["required"]
        assert "chest_y" in deposit_tool.inputSchema["required"]
        assert "item_ids" in deposit_tool.inputSchema["properties"]
        assert "item_ids" not in deposit_tool.inputSchema["required"]
        assert deposit_tool.inputSchema["properties"]["item_ids"]["default"] is None
        assert "detail" in deposit_tool.inputSchema["properties"]

        # Check organize_chest schema
        organize_tool = next(t for t in tools if t.name == "organize_chest")
        assert "chest_x" in organize_tool.inputSchema["required"]
        assert "chest_y" in organize_tool.inputSchema["required"]
        assert "detail" in organize_tool.inputSchema["properties"]

        # Check hoe_tiles schema
        hoe_tool = next(t for t in tools if t.name == "hoe_tiles")
        assert "tiles" in hoe_tool.inputSchema["required"]
        assert "detail" in hoe_tool.inputSchema["properties"]

        # Check plant_seeds schema
        plant_tool = next(t for t in tools if t.name == "plant_seeds")
        assert "seed_item_id" in plant_tool.inputSchema["required"]
        assert "tiles" in plant_tool.inputSchema["required"]
        assert "detail" in plant_tool.inputSchema["properties"]

        # Check ship_items schema
        ship_tool = next(t for t in tools if t.name == "ship_items")
        assert "items" in ship_tool.inputSchema["required"]
        assert "detail" in ship_tool.inputSchema["properties"]
        assert ship_tool.inputSchema["properties"]["detail"]["default"] is False

    asyncio.run(run())


def test_mcp_server_call_get_work_overview(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool("get_work_overview", {})
        assert len(content) == 1
        assert data["farmWork"]["matureCropCount"] == 1
        assert data["farmWork"]["tilledUnwateredCount"] == 2
        assert data["companion"]["freeSlots"] == 11
        assert data["companion"]["nonToolItems"][0]["name"] == "Parsnip"
        assert len(data["chests"]) == 1
        assert data["chests"][0]["tile"] == {"x": 70, "y": 12}
        mock_scheduler.get_work_overview.assert_awaited_once_with(detail=False)

    asyncio.run(run())


def test_mcp_server_call_get_status(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        # Default concise call
        content, data = await server.call_tool("get_status", {})
        assert len(content) == 1
        assert data["companion"]["tileX"] == 64
        assert data["companion"]["tileY"] == 15
        assert data["world"]["timeOfDay"] == 600
        assert data["saveId"] == "mock-save-123"
        assert "locationId" not in data["companion"]

        # Detailed call
        _, data_det = await server.call_tool("get_status", {"detail": True})
        assert data_det["companion"]["locationId"] == "Farm"
        assert data_det["world"]["season"] == "spring"

    asyncio.run(run())


def test_mcp_server_call_query_farm_work(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        # Default concise call
        content, data = await server.call_tool("query_farm_work", {})
        assert len(content) == 1
        assert data["farmWork"]["tilledUnwateredCount"] == 2
        assert data["farmWork"]["matureCropCount"] == 1
        assert "tilledUnwateredTiles" not in data["farmWork"]

        # Detailed call
        _, data_det = await server.call_tool("query_farm_work", {"detail": True})
        assert len(data_det["farmWork"]["tilledUnwateredTiles"]) == 2

    asyncio.run(run())


def test_mcp_server_call_water_zone(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        # Default concise call
        content, data = await server.call_tool(
            "water_zone", {"center_x": 64, "center_y": 15, "radius": 0}
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["completedCount"] == 1
        assert "effects" not in data
        mock_scheduler.execute_water_zone.assert_awaited_once_with(
            center_x=64, center_y=15, radius=0
        )

        # Detailed call
        _, data_det = await server.call_tool(
            "water_zone", {"center_x": 64, "center_y": 15, "radius": 0, "detail": True}
        )
        assert len(data_det["effects"]) == 1

    asyncio.run(run())


def test_mcp_server_call_water_auto(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        # Default concise call
        content, data = await server.call_tool("water_auto", {"max_tiles": 15})
        assert len(content) == 1
        assert data["status"] == "executed"
        assert data["targetCount"] == 2
        assert data["fresh"] is False
        assert data["remainingUnwateredCount"] is None  # Snapshot not refreshed in mock, un-faked
        assert "targetTiles" not in data
        assert "effects" not in data
        mock_scheduler.water_auto.assert_awaited_once_with(max_tiles=15)

        # Detailed call
        _, data_det = await server.call_tool("water_auto", {"max_tiles": 15, "detail": True})
        assert len(data_det["targetTiles"]) == 2
        assert len(data_det["effects"]) == 1

    asyncio.run(run())


def test_mcp_server_call_query_inventory(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        # Default concise call
        content, data = await server.call_tool("query_inventory", {})
        assert len(content) == 1
        assert data["inventory"]["capacity"] == 12
        assert data["inventory"]["freeSlots"] == 11
        assert data["inventory"]["nonToolItems"][0]["itemId"] == "(O)24"
        assert "slots" not in data["inventory"]

        # Detailed call
        _, data_det = await server.call_tool("query_inventory", {"detail": True})
        assert data_det["inventory"]["slots"][0]["itemId"] == "(O)24"

    asyncio.run(run())


def test_mcp_server_call_query_chests(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        # Default concise call
        content, data = await server.call_tool("query_chests", {})
        assert len(content) == 1
        assert len(data["chests"]) == 1
        assert data["chests"][0]["tile"] == {"x": 70, "y": 12}
        assert data["chests"][0]["freeSlots"] == 35
        assert data["chests"][0]["items"][0]["itemId"] == "(O)24"
        assert data["chests"][0]["itemsTruncated"] is False
        assert "contents" not in data["chests"][0]

        # Detailed call
        _, data_det = await server.call_tool("query_chests", {"detail": True})
        assert data_det["chests"][0]["contents"][0]["itemId"] == "(O)24"

    asyncio.run(run())


def test_mcp_server_call_harvest_auto(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool("harvest_auto", {"max_tiles": 10})
        assert len(content) == 1
        assert data["status"] == "executed"
        assert data["targetCount"] == 2
        assert data["fresh"] is False
        assert data["remainingMatureCount"] is None  # Snapshot not received, unfaked
        assert "targetTiles" not in data
        mock_scheduler.harvest_auto.assert_awaited_once_with(max_tiles=10)

        # Detailed call
        _, data_det = await server.call_tool("harvest_auto", {"max_tiles": 10, "detail": True})
        assert len(data_det["targetTiles"]) == 2

    asyncio.run(run())


def test_mcp_server_call_harvest_auto_default(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool("harvest_auto", {})
        assert len(content) == 1
        assert data["status"] == "executed"
        mock_scheduler.harvest_auto.assert_awaited_once_with(max_tiles=16)

    asyncio.run(run())


def test_mcp_server_call_deposit_to_chest(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "deposit_to_chest",
            {"chest_x": 70, "chest_y": 12, "item_ids": ["(O)24"]},
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["completedCount"] == 2
        assert data["chest"]["tile"] == {"x": 70, "y": 12}
        assert "effects" not in data
        mock_scheduler.deposit_to_chest.assert_awaited_once_with(
            chest_x=70, chest_y=12, item_ids=["(O)24"]
        )

        # Detailed call
        _, data_det = await server.call_tool(
            "deposit_to_chest",
            {"chest_x": 70, "chest_y": 12, "item_ids": ["(O)24"], "detail": True},
        )
        assert len(data_det["effects"]) == 1

    asyncio.run(run())


def test_mcp_server_call_deposit_to_chest_without_item_ids(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "deposit_to_chest", {"chest_x": 70, "chest_y": 12}
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        mock_scheduler.deposit_to_chest.assert_awaited_once_with(
            chest_x=70, chest_y=12, item_ids=None
        )

    asyncio.run(run())


def test_mcp_server_call_withdraw_from_chest(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "withdraw_from_chest",
            {"chest_x": 70, "chest_y": 12, "item_id": "(O)CarrotSeeds", "count": 3},
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["completedCount"] == 1
        assert data["chest"]["tile"] == {"x": 70, "y": 12}
        assert "effects" not in data
        mock_scheduler.withdraw_from_chest.assert_awaited_once_with(
            chest_x=70,
            chest_y=12,
            items=None,
            item_id="(O)CarrotSeeds",
            count=3,
            location_id="Farm",
        )

        # Detailed call
        _, data_det = await server.call_tool(
            "withdraw_from_chest",
            {
                "chest_x": 70,
                "chest_y": 12,
                "items": [{"itemId": "(O)CarrotSeeds", "count": 3}],
                "detail": True,
            },
        )
        assert len(data_det["effects"]) == 1

    asyncio.run(run())


def test_mcp_server_call_organize_chest(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "organize_chest", {"chest_x": 70, "chest_y": 12}
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["completedCount"] == 1
        assert data["chest"]["tile"] == {"x": 70, "y": 12}
        assert "effects" not in data
        mock_scheduler.organize_chest.assert_awaited_once_with(chest_x=70, chest_y=12)

        # Detailed call
        _, data_det = await server.call_tool(
            "organize_chest", {"chest_x": 70, "chest_y": 12, "detail": True}
        )
        assert len(data_det["effects"]) == 1

    asyncio.run(run())


def test_mcp_server_call_query_planting_options(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool("query_planting_options", {})
        assert len(content) == 1
        assert data["season"] == "spring"
        assert data["companionHasHoe"] is True
        assert data["candidateTiles"]["tilledEmptyCount"] == 6
        mock_scheduler.query_planting_options.assert_awaited_once_with(detail=False)

        await server.call_tool("query_planting_options", {"detail": True})
        mock_scheduler.query_planting_options.assert_awaited_with(detail=True)

    asyncio.run(run())


def test_mcp_server_call_query_shop(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool("query_shop", {})
        assert len(content) == 1
        assert data["shopId"] == "SeedShop"
        assert data["isOpen"] is True
        assert data["ownerPresent"] is True
        assert data["availableMoney"] == 1000
        assert len(data["items"]) == 1
        mock_scheduler.query_shop.assert_awaited_once_with(shop_id="SeedShop", detail=False)

        await server.call_tool("query_shop", {"shop_id": "SeedShop", "detail": True})
        mock_scheduler.query_shop.assert_awaited_with(shop_id="SeedShop", detail=True)

    asyncio.run(run())


def test_mcp_server_call_hoe_tiles(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "hoe_tiles", {"tiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}]}
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["completedCount"] == 2
        assert data["affectedTiles"] == [{"x": 64, "y": 14}, {"x": 65, "y": 14}]
        assert "effects" not in data
        mock_scheduler.execute_hoe_tiles.assert_awaited_once_with(
            tiles=[{"x": 64, "y": 14}, {"x": 65, "y": 14}]
        )

        _, data_det = await server.call_tool(
            "hoe_tiles", {"tiles": [{"x": 64, "y": 14}], "detail": True}
        )
        assert len(data_det["effects"]) == 2

    asyncio.run(run())


def test_mcp_server_call_plant_seeds(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "plant_seeds",
            {"seed_item_id": "(O)472", "tiles": [{"x": 64, "y": 14}]},
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["completedCount"] == 1
        assert data["plantedTiles"] == [{"x": 64, "y": 14}]
        assert "effects" not in data
        mock_scheduler.execute_plant_seeds.assert_awaited_once_with(
            seed_item_id="(O)472", tiles=[{"x": 64, "y": 14}]
        )

        _, data_det = await server.call_tool(
            "plant_seeds",
            {"seed_item_id": "(O)472", "tiles": [{"x": 64, "y": 14}], "detail": True},
        )
        assert len(data_det["effects"]) == 1

    asyncio.run(run())


def test_mcp_server_call_ship_items(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "ship_items",
            {"items": [{"itemId": "(O)24", "count": 2}]},
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["completedCount"] == 2
        assert data["estimatedValue"] == 70
        assert data["estimatedTotalValue"] == 70
        assert data["shippingBinTotalCount"] == 2
        assert data["remainingBackpackCounts"] == {"(O)24": 2}
        assert len(data["shippedItems"]) == 1
        assert "effects" not in data
        mock_scheduler.execute_ship_items.assert_awaited_once_with(
            items=[{"itemId": "(O)24", "count": 2}]
        )

        _, data_det = await server.call_tool(
            "ship_items",
            {"items": [{"itemId": "(O)24", "count": 2}], "detail": True},
        )
        assert len(data_det["effects"]) == 1
        assert data_det["effects"][0]["state"] == "shipped"

    asyncio.run(run())


def test_mcp_server_call_purchase_items(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "purchase_items",
            {"items": [{"itemId": "(O)472", "count": 2}], "budget_limit": 100},
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["completedCount"] == 2
        assert data["totalCost"] == 40
        assert data["remainingBudget"] == 60
        assert data["availableMoneyAfter"] == 460
        assert len(data["purchasedItems"]) == 1
        assert data["purchasedItems"][0]["itemId"] == "(O)472"
        assert data["purchasedItems"][0]["unitPrice"] == 20
        assert "effects" not in data
        first_call = mock_scheduler.execute_purchase_items.await_args_list[0].kwargs
        assert first_call["items"] == [{"itemId": "(O)472", "count": 2}]
        assert first_call["budget_limit"] == 100 and first_call["shop_id"] == "SeedShop"
        assert isinstance(first_call["command_id"], str) and first_call["command_id"]

        _, data_det = await server.call_tool(
            "purchase_items",
            {"items": [{"itemId": "(O)472", "count": 2}], "budget_limit": 100, "detail": True},
        )
        assert len(data_det["effects"]) == 1
        assert data_det["effects"][0]["state"] == "purchased"

    asyncio.run(run())


def test_mcp_server_call_navigate_to(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "navigate_to",
            {"location_id": "Town", "tile_x": 43, "tile_y": 58},
        )
        assert len(content) == 1
        assert data["terminalState"] == "succeeded"
        assert data["status"] == "success"
        assert data["finalLocation"] == "Town"
        assert data["finalTile"] == {"x": 43, "y": 58}
        assert data["targetAdjusted"] is False
        assert data["requestedTile"] == {"x": 43, "y": 58}
        assert data["visitedLocations"] == ["Farm", "BusStop", "Town"]
        assert "effects" not in data

        mock_scheduler.execute_navigate_to.assert_awaited_once_with(
            location_id="Town",
            tile={"x": 43, "y": 58},
        )

        _, data_det = await server.call_tool(
            "navigate_to",
            {"location_id": "Town", "tile": {"x": 43, "y": 58}, "detail": True},
        )
        assert len(data_det["effects"]) == 3
        assert "resources" in data_det

        # Validation errors
        with pytest.raises(ToolError, match="non-negative integers"):
            await server.call_tool(
                "navigate_to", {"location_id": "Town", "tile_x": -1, "tile_y": 5}
            )

        with pytest.raises(ToolError, match="non-negative integers"):
            await server.call_tool(
                "navigate_to", {"location_id": "Town", "tile": {"x": -1, "y": 5}}
            )

        with pytest.raises(ToolError, match="non-empty string"):
            await server.call_tool(
                "navigate_to", {"location_id": "", "tile_x": 10, "tile_y": 10}
            )

    asyncio.run(run())


def test_mcp_server_call_controls(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)

        # Pause
        pause_content, pause_data = await server.call_tool("pause_task", {})
        assert len(pause_content) == 1
        assert pause_data["status"] == "paused"
        mock_scheduler.pause_task.assert_awaited_once()

        # Resume
        resume_content, resume_data = await server.call_tool("resume_task", {})
        assert len(resume_content) == 1
        assert resume_data["status"] == "resumed"
        mock_scheduler.resume_task.assert_awaited_once()

        # Cancel
        cancel_content, cancel_data = await server.call_tool(
            "cancel_task", {"reason": "MCP test"}
        )
        assert len(cancel_content) == 1
        assert cancel_data["status"] == "cancelling"
        mock_scheduler.cancel_task.assert_awaited_once_with(reason="MCP test")

    asyncio.run(run())


def test_mcp_controls_disable_autonomy_before_no_active_task(tmp_path: Path, mock_scheduler):
    async def run():
        mock_scheduler.run_dir = None
        mock_scheduler.pause_task.side_effect = NoActiveTaskError("idle")
        mock_scheduler.cancel_task.side_effect = NoActiveTaskError("idle")
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        state_path = tmp_path / "data" / "autonomy-state.json"
        from stardew_ai_runtime.autonomy import AutonomyController
        ctl = AutonomyController(state_path)
        ctl.set_enabled("mock-save-123", True)

        with pytest.raises(ToolError, match="Cannot pause"):
            await server.call_tool("pause_task", {})
        assert ctl.state("mock-save-123").enabled is False
        ctl.set_enabled("mock-save-123", True)
        with pytest.raises(ToolError, match="Cannot cancel"):
            await server.call_tool("cancel_task", {})
        assert ctl.state("mock-save-123").enabled is False

    asyncio.run(run())


def test_mcp_server_error_handling(mock_scheduler):
    async def run():
        from stardew_ai_runtime.scheduler import PolicyViolationError
        mock_scheduler.execute_water_zone.side_effect = PolicyViolationError(
            "Invalid coordinates"
        )
        server = create_mcp_server(scheduler=mock_scheduler)

        with pytest.raises(
            ToolError, match="Water zone execution rejected: Invalid coordinates"
        ):
            await server.call_tool(
                "water_zone", {"center_x": -1, "center_y": 15, "radius": 0}
            )

    asyncio.run(run())


def test_mcp_server_new_tools_error_mapping(mock_scheduler):
    async def run():
        from stardew_ai_runtime.scheduler import PolicyViolationError, SchedulerError

        server = create_mcp_server(scheduler=mock_scheduler)

        mock_scheduler.harvest_auto.side_effect = PolicyViolationError(
            "Invalid max_tiles: 100. Value must be between 1 and 64 (inclusive)."
        )
        with pytest.raises(ToolError, match="Auto harvest rejected: Invalid max_tiles"):
            await server.call_tool("harvest_auto", {"max_tiles": 100})

        mock_scheduler.deposit_to_chest.side_effect = PolicyViolationError(
            "chest_x must be an integer, got bool"
        )
        with pytest.raises(ToolError, match="Deposit to chest rejected: chest_x"):
            await server.call_tool("deposit_to_chest", {"chest_x": -1, "chest_y": 12})

        mock_scheduler.withdraw_from_chest.side_effect = PolicyViolationError(
            "Either items list or item_id must be provided for withdraw_from_chest"
        )
        with pytest.raises(ToolError, match="Withdraw from chest rejected: Either items"):
            await server.call_tool("withdraw_from_chest", {"chest_x": 70, "chest_y": 12})

        mock_scheduler.organize_chest.side_effect = PolicyViolationError(
            "Concurrent tasks are not permitted"
        )
        with pytest.raises(ToolError, match="Organize chest rejected: Concurrent tasks"):
            await server.call_tool("organize_chest", {"chest_x": 70, "chest_y": 12})

        mock_scheduler.query_inventory.side_effect = SchedulerError(
            "World snapshot does not include an 'inventory' section"
        )
        with pytest.raises(ToolError, match="Failed to query inventory"):
            await server.call_tool("query_inventory", {})

        mock_scheduler.query_chests.side_effect = SchedulerError(
            "World snapshot does not include a 'chests' section"
        )
        with pytest.raises(ToolError, match="Failed to query chests"):
            await server.call_tool("query_chests", {})

        mock_scheduler.get_work_overview.side_effect = SchedulerError(
            "Failed to connect to Companion Mod"
        )
        with pytest.raises(ToolError, match="Failed to get work overview"):
            await server.call_tool("get_work_overview", {})

        mock_scheduler.query_planting_options.side_effect = SchedulerError(
            "World snapshot does not include a 'planting' section"
        )
        with pytest.raises(ToolError, match="Failed to query planting options"):
            await server.call_tool("query_planting_options", {})

        mock_scheduler.query_shop.side_effect = SchedulerError(
            "World snapshot does not include a 'shop' section"
        )
        with pytest.raises(ToolError, match="Failed to query shop"):
            await server.call_tool("query_shop", {})

        mock_scheduler.execute_hoe_tiles.side_effect = PolicyViolationError(
            "tiles must be a non-empty list"
        )
        with pytest.raises(ToolError, match="Hoe tiles rejected: tiles must be a non-empty list"):
            await server.call_tool("hoe_tiles", {"tiles": []})

        mock_scheduler.execute_plant_seeds.side_effect = PolicyViolationError(
            "seed_item_id cannot be empty"
        )
        with pytest.raises(ToolError, match="Plant seeds rejected: seed_item_id cannot be empty"):
            await server.call_tool(
                "plant_seeds", {"seed_item_id": "", "tiles": [{"x": 64, "y": 15}]}
            )

        mock_scheduler.execute_ship_items.side_effect = PolicyViolationError(
            "items must be a non-empty list of 1..36 items"
        )
        with pytest.raises(ToolError, match="Ship items rejected: items must be a non-empty list"):
            await server.call_tool("ship_items", {"items": []})

    asyncio.run(run())


def test_mcp_server_state_closing_loop_fresh_and_details():
    """Verifies that post-action state closure accurately consumes fresh snapshots

    and preserves crucial decision details like inventoryFull and chestFull.
    """
    async def run():
        sched = MagicMock()
        sched.latest_world_revision = 5

        # Mock fresh snapshot arriving with revision 6
        fresh_payload = {
            "worldRevision": 6,
            "payload": {
                "farmWork": {
                    "matureCropCount": 1,  # 1 mature crop truly remaining
                    "tilledUnwateredCount": 0,  # 0 unwatered truly remaining
                },
                "inventory": {
                    "capacity": 12,
                    "freeSlots": 0,  # backpack full!
                    "slots": [
                        {"index": 0, "itemId": "(O)24", "name": "Parsnip", "stack": 1, "quality": 0}
                    ],
                },
                "chests": {
                    "items": [
                        {
                            "tile": {"x": 70, "y": 12},
                            "capacity": 36,
                            "freeSlots": 0,  # chest full!
                            "contents": [
                                {
                                    "slot": 0,
                                    "itemId": "(O)24",
                                    "name": "Parsnip",
                                    "stack": 999,
                                    "quality": 0,
                                }
                            ],
                        }
                    ]
                },
                "planting": {
                    "seeds": [
                        {
                            "itemId": "(O)472",
                            "name": "Parsnip Seeds",
                            "stack": 12,
                            "canPlantCurrentSeason": True,
                        }
                    ],
                    "candidateTiles": {
                        "tilledEmptyCount": 5,
                        "tillableCount": 15,
                    },
                },
            },
        }

        sched.wait_for_fresh_snapshot = AsyncMock(return_value=(fresh_payload, True))

        # 1. Harvest stops early due to inventory full
        sched.harvest_auto = AsyncMock(return_value={
            "status": "executed",
            "terminalState": "partially-succeeded",
            "completedCount": 1,
            "skippedCount": 2,
            "failedCount": 0,
            "targetCount": 3,
            "details": {"inventoryFull": True},
            "error": None,
        })

        server = create_mcp_server(scheduler=sched)
        _, h_res = await server.call_tool("harvest_auto", {"max_tiles": 3})

        assert h_res["terminalState"] == "partially-succeeded"
        assert h_res["fresh"] is True
        assert h_res["remainingMatureCount"] == 1  # From fresh snapshot, NOT 3-1=2!
        assert h_res["inventory"]["freeSlots"] == 0
        assert h_res["details"] == {"inventoryFull": True}

        # 2. Deposit stops early due to chest full
        sched.deposit_to_chest = AsyncMock(return_value={
            "terminalState": "partially-succeeded",
            "completedCount": 1,
            "skippedCount": 1,
            "failedCount": 0,
            "details": {"chestFull": True},
            "error": None,
        })
        _, d_res = await server.call_tool("deposit_to_chest", {"chest_x": 70, "chest_y": 12})
        assert d_res["fresh"] is True
        assert d_res["chest"]["freeSlots"] == 0
        assert d_res["details"] == {"chestFull": True}

        # 3. Water auto succeeds with 0 remaining
        sched.water_auto = AsyncMock(return_value={
            "status": "executed",
            "terminalState": "succeeded",
            "completedCount": 4,
            "skippedCount": 0,
            "failedCount": 0,
            "targetCount": 4,
            "details": None,
            "error": None,
        })
        _, w_res = await server.call_tool("water_auto", {"max_tiles": 4})
        assert w_res["fresh"] is True
        assert w_res["remainingUnwateredCount"] == 0

        # 4. Hoe tiles with fresh snapshot closure
        sched.execute_hoe_tiles = AsyncMock(return_value={
            "terminalState": "succeeded",
            "completedCount": 2,
            "skippedCount": 0,
            "failedCount": 0,
            "effects": [
                {"tile": {"x": 64, "y": 14}, "state": "hoed"},
                {"tile": {"x": 65, "y": 14}, "state": "hoed"},
            ],
            "details": {"hoedTiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}]},
            "error": None,
        })
        _, hoe_fresh = await server.call_tool(
            "hoe_tiles", {"tiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}]}
        )
        assert hoe_fresh["terminalState"] == "succeeded"
        assert hoe_fresh["fresh"] is True
        assert hoe_fresh["candidateTiles"]["tillableCount"] == 15
        assert hoe_fresh["candidateTiles"]["tilledEmptyCount"] == 5

        # 5. Plant seeds with fresh snapshot closure
        sched.execute_plant_seeds = AsyncMock(return_value={
            "terminalState": "succeeded",
            "completedCount": 1,
            "skippedCount": 0,
            "failedCount": 0,
            "effects": [
                {"tile": {"x": 64, "y": 14}, "state": "planted", "itemId": "(O)472", "stack": 12}
            ],
            "details": {
                "plantedTiles": [{"x": 64, "y": 14}],
                "remainingSeedStack": 12,
                "seedItemId": "(O)472",
            },
            "error": None,
        })
        _, plant_fresh = await server.call_tool(
            "plant_seeds", {"seed_item_id": "(O)472", "tiles": [{"x": 64, "y": 14}]}
        )
        assert plant_fresh["terminalState"] == "succeeded"
        assert plant_fresh["fresh"] is True
        assert plant_fresh["remainingSeedStack"] == 12
        assert plant_fresh["candidateTiles"]["tilledEmptyCount"] == 5

        # 6. Action without fresh snapshot (fresh=False) -> unknown instead of arithmetic spoofing
        sched.wait_for_fresh_snapshot = AsyncMock(return_value=(None, False))
        _, plant_stale = await server.call_tool(
            "plant_seeds", {"seed_item_id": "(O)472", "tiles": [{"x": 64, "y": 14}]}
        )
        assert plant_stale["fresh"] is False
        assert plant_stale["remainingSeedStack"] == "unknown"
        assert plant_stale["candidateTiles"]["tilledEmptyCount"] == "unknown"
        assert plant_stale["candidateTiles"]["fresh"] is False

        _, hoe_stale = await server.call_tool(
            "hoe_tiles", {"tiles": [{"x": 64, "y": 14}]}
        )
        assert hoe_stale["fresh"] is False
        assert hoe_stale["candidateTiles"]["tillableCount"] == "unknown"
        assert hoe_stale["candidateTiles"]["fresh"] is False

    asyncio.run(run())


def test_mcp_server_stdio_integration_with_mock_transport(tmp_path: Path):
    """End-to-end integration test spawning MCP server process via stdio client against mock server.

    NOTE: This integration test runs against MockModTransportServer on loopback
    and is strictly offline test evidence. It is NOT real game evidence.
    """
    async def run():
        mock_server = MockModTransportServer(session_token="secret-token-test")
        await mock_server.start()

        disc_file = tmp_path / "transport-discovery.json"
        disc_file.write_text(
            json.dumps({
                "host": "127.0.0.1",
                "port": mock_server.port,
                "sessionToken": "secret-token-test",
                "saveId": "mock-save-hash-123",
                "gameSessionId": "mock-session-456",
            }),
            encoding="utf-8",
        )

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "stardew_ai_runtime.mcp_server", "--run-dir", str(tmp_path)],
        )

        try:
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    # 1. List tools
                    tools_result = await session.list_tools()
                    tool_names = [t.name for t in tools_result.tools]
                    assert "get_work_overview" in tool_names
                    assert "get_status" in tool_names
                    assert "query_farm_work" in tool_names
                    assert "water_auto" in tool_names
                    assert "water_zone" in tool_names

                    # 2. Call get_work_overview
                    overview_res = await session.call_tool("get_work_overview", {})
                    assert not overview_res.isError
                    overview_data = json.loads(overview_res.content[0].text)
                    assert overview_data["farmWork"]["tilledUnwateredCount"] == 2

                    # 3. Call query_farm_work with detail=True
                    work_res = await session.call_tool("query_farm_work", {"detail": True})
                    assert not work_res.isError
                    work_data = json.loads(work_res.content[0].text)
                    assert "farmWork" in work_data
                    assert len(work_data["farmWork"]["tilledUnwateredTiles"]) == 2
                    assert work_data["farmWork"]["tilledUnwateredCount"] == 2

                    # 4. Call water_auto
                    auto_res = await session.call_tool("water_auto", {"max_tiles": 5})
                    assert not auto_res.isError
                    auto_data = json.loads(auto_res.content[0].text)
                    assert auto_data["status"] == "executed"
                    assert auto_data["terminalState"] == "succeeded"
                    assert auto_data["completedCount"] == 2
                    assert auto_data["targetCount"] == 2
                    assert auto_data["fresh"] is False  # MockMod does not push fresh snapshot
                    assert auto_data["remainingUnwateredCount"] is None  # Unfaked count

                    # 5. Call pause_task when idle -> must return isError: True
                    pause_res = await session.call_tool("pause_task", {})
                    assert pause_res.isError
                    assert "No active task" in pause_res.content[0].text

                    # 6. Security audit: verify session token NEVER leaks into MCP tool responses
                    raw_text = work_res.content[0].text + auto_res.content[0].text
                    assert "secret-token-test" not in raw_text

        finally:
            await mock_server.stop()

    asyncio.run(run())


def test_mcp_server_call_plant_crop_workflow(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "plant_crop_workflow",
            {"crop_name_or_id": "Parsnip", "count": 3},
        )
        assert len(content) == 1
        assert data["status"] == "succeeded"
        assert data["seedsPlanted"] == 3
        assert data["tilesHoed"] == 3
        assert data["tilesWatered"] == 3
        assert "effects" not in data  # detail=False strips effects
        mock_scheduler.plant_crop_workflow.assert_awaited_once_with(
            crop_name_or_id="Parsnip",
            count=3,
            target_tiles=None,
            auto_till=True,
            water=True,
            withdraw_from_chest=True,
            chest_tile=None,
            location_id="Farm",
        )

        # Test with detail=True
        _, data_det = await server.call_tool(
            "plant_crop_workflow",
            {"seed_item_id": "(O)472", "count": 2, "detail": True},
        )
        assert "effects" in data_det

        # Test validation error
        with pytest.raises(ToolError, match="Must provide seed_item_id or crop_name_or_id"):
            await server.call_tool("plant_crop_workflow", {"count": 1})

    asyncio.run(run())


def test_mcp_server_call_navigate_to_with_landmark(mock_scheduler):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        content, data = await server.call_tool(
            "navigate_to",
            {"location_id": "Farm", "landmark": "shipping_bin"},
        )
        assert len(content) == 1
        mock_scheduler.execute_navigate_to.assert_awaited_with(
            location_id="Farm",
            tile=None,
            landmark="shipping_bin",
        )

    asyncio.run(run())


def test_mcp_server_call_query_chests_filter_and_paginate(mock_scheduler):
    async def run():
        # Setup scheduler with 2 chests
        mock_scheduler.query_chests = AsyncMock(return_value={
            "chests": [
                {
                    "tile": {"x": 70, "y": 12},
                    "capacity": 36,
                    "freeSlots": 34,
                    "contents": [
                        {"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 15, "category": -74},
                        {"itemId": "(O)388", "name": "Wood", "stack": 50, "category": -16},
                    ],
                },
                {
                    "tile": {"x": 72, "y": 12},
                    "capacity": 36,
                    "freeSlots": 35,
                    "contents": [
                        {"itemId": "(O)390", "name": "Stone", "stack": 99, "category": -2},
                    ],
                },
            ],
            "isTruncated": False,
            "worldRevision": 1,
        })
        server = create_mcp_server(scheduler=mock_scheduler)

        # 1. Filter by is_seed=True -> only chest 1 matches
        content, data = await server.call_tool("query_chests", {"is_seed": True})
        assert len(content) == 1
        assert data["totalChests"] == 2
        assert data["matchingChests"] == 1
        assert len(data["chests"]) == 1
        assert data["chests"][0]["tile"] == {"x": 70, "y": 12}
        assert len(data["chests"][0]["items"]) == 1
        assert data["chests"][0]["items"][0]["name"] == "Parsnip Seeds"

        # 2. Filter by name="stone" -> only chest 2 matches
        _, data_stone = await server.call_tool("query_chests", {"name": "stone"})
        assert data_stone["matchingChests"] == 1
        assert data_stone["chests"][0]["tile"] == {"x": 72, "y": 12}

        # 3. Filter with no match -> empty list
        _, data_none = await server.call_tool("query_chests", {"name": "nonexistent"})
        assert data_none["matchingChests"] == 0
        assert len(data_none["chests"]) == 0

        # 4. Pagination
        _, data_page = await server.call_tool("query_chests", {"page": 1, "page_size": 1})
        assert len(data_page["chests"]) == 1
        assert data_page["totalPages"] == 2
        assert data_page["isTruncated"] is True

    asyncio.run(run())


def test_mcp_server_call_query_shop_filter(mock_scheduler):
    async def run():
        mock_scheduler.query_shop = AsyncMock(return_value={
            "shopId": "SeedShop",
            "status": "open",
            "isOpen": True,
            "ownerPresent": True,
            "availableMoney": 500,
            "itemsCount": 2,
            "items": [
                {"itemId": "(O)472", "name": "Parsnip Seeds", "price": 20, "stock": -1, "isInfiniteStock": True, "category": -74},
                {"itemId": "(O)24", "name": "Parsnip", "price": 50, "stock": 5, "isInfiniteStock": False, "category": -75},
            ],
            "worldRevision": 1,
        })
        server = create_mcp_server(scheduler=mock_scheduler)

        # Filter by is_seed=True -> only seeds
        content, data = await server.call_tool("query_shop", {"is_seed": True})
        assert len(content) == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "Parsnip Seeds"

        # Filter by item_id
        _, data_id = await server.call_tool("query_shop", {"item_id": "(O)24"})
        assert len(data_id["items"]) == 1
        assert data_id["items"][0]["name"] == "Parsnip"

    asyncio.run(run())


def test_mcp_server_navigate_to_xy_and_executing_status(mock_scheduler):
    async def run():
        mock_scheduler.execute_navigate_to = AsyncMock(return_value={
            "status": "executing",
            "terminalState": "running",
            "taskId": "task-nav-123",
            "inProgress": True,
            "message": "Navigation task is executing on companion in background.",
            "details": {
                "visitedLocations": ["Farm"],
                "finalLocation": "Farm",
                "finalTile": {"x": 61, "y": 17},
            },
            "error": None,
        })
        server = create_mcp_server(scheduler=mock_scheduler)

        # Call with flat x and y parameters
        _, data = await server.call_tool("navigate_to", {"location_id": "Farm", "x": 61, "y": 17})
        assert data["status"] == "executing"
        assert data["terminalState"] == "running"
        assert data["inProgress"] is True
        assert data["taskId"] == "task-nav-123"
        mock_scheduler.execute_navigate_to.assert_awaited_with(
            location_id="Farm", tile={"x": 61, "y": 17}
        )

    asyncio.run(run())


def test_mcp_server_water_auto_blocked_targets_actionable_summary(mock_scheduler):
    async def run():
        mock_scheduler.water_auto = AsyncMock(return_value={
            "status": "executed",
            "taskId": "task-water-1",
            "terminalState": "partially-succeeded",
            "completedCount": 1,
            "skippedCount": 0,
            "failedCount": 1,
            "targetCount": 2,
            "effects": [
                {"state": "watered", "tile": {"x": 60, "y": 21}}
            ],
            "details": {
                "failedTiles": [
                    {"tile": {"x": 61, "y": 21}, "reason": "Dynamic obstacle blocked path and maximum replans exceeded."}
                ],
                "skippedTiles": []
            },
            "error": None,
        })
        server = create_mcp_server(scheduler=mock_scheduler)

        _, data = await server.call_tool("water_auto", {"max_tiles": 25})
        assert data["status"] == "executed"
        assert data["wateredTiles"] == [{"x": 60, "y": 21}]
        assert len(data["blockedTargets"]) == 1
        assert data["blockedTargets"][0]["x"] == 61
        assert data["blockedTargets"][0]["y"] == 21
        assert data["blockedTargets"][0]["retryable"] is False
        assert "Non-retryable blocked targets" in data["actionableSummary"]

    asyncio.run(run())


def test_mcp_server_executing_status_propagation(mock_scheduler):
    async def run():
        exec_payload = {
            "status": "executing",
            "terminalState": "running",
            "taskId": "task-gen-999",
            "inProgress": True,
            "completedCount": 0,
            "details": {"taskId": "task-gen-999"},
        }
        mock_scheduler.execute_water_zone = AsyncMock(return_value=exec_payload)
        mock_scheduler.harvest_auto = AsyncMock(return_value=exec_payload)
        mock_scheduler.deposit_to_chest = AsyncMock(return_value=exec_payload)
        mock_scheduler.withdraw_from_chest = AsyncMock(return_value=exec_payload)
        mock_scheduler.execute_hoe_tiles = AsyncMock(return_value=exec_payload)
        mock_scheduler.execute_plant_seeds = AsyncMock(return_value=exec_payload)
        server = create_mcp_server(scheduler=mock_scheduler)

        for tool_name, args in [
            ("water_zone", {"center_x": 60, "center_y": 20, "radius": 1}),
            ("harvest_auto", {"max_tiles": 10}),
            ("deposit_to_chest", {"chest_x": 59, "chest_y": 17}),
            ("withdraw_from_chest", {"chest_x": 59, "chest_y": 17, "item_id": "seed"}),
            ("hoe_tiles", {"tiles": [{"x": 60, "y": 20}]}),
            ("plant_seeds", {"seed_item_id": "seed", "tiles": [{"x": 60, "y": 20}]}),
        ]:
            _, data = await server.call_tool(tool_name, args)
            assert data["status"] == "executing", f"{tool_name} should return status=executing"
            assert data["terminalState"] == "running", f"{tool_name} should return terminalState=running"
            assert data["inProgress"] is True

    asyncio.run(run())


def test_mcp_work_plan_runs_step_and_goes_idle(mock_scheduler, tmp_path):
    """Harness worker claims a ready step, executes the real scheduler op, and commits."""
    mock_scheduler.run_dir = None

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)

        _, idle = await server.call_tool("run_next_step", {})
        assert idle["status"] == "idle"
        assert idle["hasExecutableWork"] is False

        _, goal = await server.call_tool("manage_goal", {"action": "create", "text": "照料农场"})
        assert goal["goal"]["source"] == "agent"
        goal_id = goal["goal"]["id"]

        _, plan = await server.call_tool(
            "manage_plan",
            {
                "goal_id": goal_id,
                "tasks": [
                    {
                        "id": "t1",
                        "title": "浇水",
                        "completionCondition": "作物湿润",
                        "steps": [{"id": "s1", "operation": "water_auto", "params": {"max_tiles": 5}}],
                    }
                ],
            },
        )
        assert plan["tasks"][0]["id"] == "t1"

        _, overview = await server.call_tool("work_plan_overview", {})
        assert overview["hasExecutableWork"] is True
        assert overview["nextStep"]["operation"] == "water_auto"

        _, executed = await server.call_tool("run_next_step", {})
        assert executed["status"] == "executed"
        assert executed["outcome"] == "completed"
        assert executed["taskStatus"] == "completed"
        mock_scheduler.water_auto.assert_awaited_once_with(max_tiles=5)

        _, idle_after = await server.call_tool("run_next_step", {})
        assert idle_after["status"] == "idle"

    asyncio.run(run())


def test_mcp_plan_rejects_bad_operation_and_model_user_goal(mock_scheduler, tmp_path):
    mock_scheduler.run_dir = None

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)

        with pytest.raises(ToolError):
            await server.call_tool(
                "manage_goal", {"action": "create", "text": "假装用户指令", "source": "user"}
            )

        _, goal = await server.call_tool("manage_goal", {"action": "create", "text": "g"})
        goal_id = goal["goal"]["id"]
        with pytest.raises(ToolError):
            await server.call_tool(
                "manage_plan",
                {
                    "goal_id": goal_id,
                    "tasks": [{"id": "t1", "title": "x", "steps": [{"operation": "run_shell", "params": {}}]}],
                },
            )

    asyncio.run(run())


def test_mcp_todo_due_selection_uses_latest_snapshot(mock_scheduler, tmp_path):
    mock_scheduler.run_dir = None
    mock_scheduler.latest_snapshot = {
        "payload": {
            "world": {"year": 1, "season": "spring", "dayOfMonth": 10},
            "inventory": {"slots": [{"itemId": "(O)472", "stack": 5}]},
        }
    }

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        await server.call_tool(
            "manage_todo",
            {
                "action": "create",
                "intent": "种子够了就播种",
                "trigger": {"type": "inventory", "itemId": "(O)472", "minCount": 3},
            },
        )
        _, due = await server.call_tool("manage_todo", {"action": "due"})
        assert [item["intent"] for item in due["due"]] == ["种子够了就播种"]

    asyncio.run(run())


def test_progressive_disclosure_default_list_is_small_and_callable(mock_scheduler, tmp_path):
    """Light surface lists common actions + write entries; base schemas stay callable.

    The generic default is the full legacy list (no existing tool disappears on
    upgrade). ``light`` is requested explicitly by the game provider profile; the
    ChatBridge internal plan worker selects ``internal`` (light + reconcile).
    """
    mock_scheduler.run_dir = None

    async def run():
        # Generic default entries stay fully compatible.
        default_server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        default_tools = await default_server.list_tools()
        default_names = {t.name for t in default_tools}
        assert {"water_auto", "navigate_to", "query_chests"} <= default_names

        full_server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, full=True)
        light_server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, full=False)
        schema_light_server = create_mcp_server(
            run_dir=tmp_path, scheduler=mock_scheduler, surface="light"
        )
        internal_server = create_mcp_server(
            run_dir=tmp_path, scheduler=mock_scheduler, surface="internal"
        )

        full_tools = await full_server.list_tools()
        light_tools = await light_server.list_tools()
        light_names = {t.name for t in light_tools}

        assert "call_capability" in light_names
        assert "discover_capabilities" in light_names
        assert "plant_crop_workflow" in light_names
        # Common actions are exposed directly on the game surface.
        assert "water_auto" in light_names
        assert "harvest_auto" in light_names
        assert "navigate_to" in light_names
        # Write entry points converge on submit_plan / remember_intent.
        assert "submit_plan" in light_names
        assert "remember_intent" in light_names
        # Legacy harness tools and the fixed harvest-and-store binding stay full-only.
        assert "manage_goal" not in light_names
        assert "manage_plan" not in light_names
        assert "manage_todo" not in light_names
        assert "run_next_step" not in light_names
        assert "work_plan_overview" not in light_names
        assert "harvest_and_store" not in light_names
        assert "query_chests" not in light_names
        assert len(light_tools) < len(full_tools)
        assert {t.name for t in schema_light_server._tool_manager.list_tools()} == light_names

        internal_names = {t.name for t in internal_server._tool_manager.list_tools()}
        assert internal_names == light_names | {"reconcile_plan_command", "dispatch_plan_operation"}
        # The worker's write dispatch and reconcile tools are harness-only.
        assert "dispatch_plan_operation" not in light_names
        assert "reconcile_plan_command" not in light_names

        default_size = len(json.dumps([t.model_dump() for t in default_tools], ensure_ascii=False))
        full_size = len(json.dumps([t.model_dump() for t in full_tools], ensure_ascii=False))
        light_size = len(json.dumps([t.model_dump() for t in light_tools], ensure_ascii=False))
        assert light_size < full_size, "lightweight list must serialize smaller"
        assert default_size == full_size, "generic default must keep the full legacy list"
        # Recorded evidence for the report.
        print(f"tools/list size: full={full_size} light={light_size}")

        _, discovered = await light_server.call_tool("discover_capabilities", {"group": "movement"})
        names = [entry["name"] for entry in discovered["groups"]["movement"]]
        assert "navigate_to" in names
        assert "get_status" in names

        calls_before = mock_scheduler.execute_navigate_to.await_count
        _, invoked = await light_server.call_tool(
            "call_capability",
            {"tool": "navigate_to", "params": {"location_id": "Farm", "x": 61, "y": 17}},
        )
        assert invoked["tool"] == "navigate_to"
        assert mock_scheduler.execute_navigate_to.await_count == calls_before + 1

        with pytest.raises(ToolError):
            await light_server.call_tool("call_capability", {"tool": "not_a_tool", "params": {}})
        with pytest.raises(ToolError):
            await light_server.call_tool(
                "call_capability", {"tool": "navigate_to", "params": {"bogus": 1}}
            )
        with pytest.raises(ToolError):
            await light_server.call_tool("discover_capabilities", {"group": "nope"})

    asyncio.run(run())


def test_submit_plan_and_remember_intent_are_the_model_entry_points(mock_scheduler, tmp_path):
    """submit_plan revises a plan; remember_intent records agent goals/todos only."""
    mock_scheduler.run_dir = None

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, full=False)

        _, goal = await server.call_tool(
            "remember_intent", {"intent": "把农场种满胡萝卜", "kind": "goal"}
        )
        assert goal["kind"] == "goal"
        assert goal["goal"]["source"] == "agent"

        _, plan = await server.call_tool(
            "submit_plan",
            {
                "goal_id": goal["goal"]["id"],
                "tasks": [
                    {
                        "id": "t1",
                        "title": "浇水",
                        "steps": [{"id": "s1", "operation": "water_auto", "params": {"max_tiles": 3}}],
                    }
                ],
            },
        )
        assert plan["tasks"][0]["id"] == "t1"

        # Re-submitting with replace supersedes the still-pending task.
        _, revised = await server.call_tool(
            "submit_plan",
            {
                "goal_id": goal["goal"]["id"],
                "replace": True,
                "tasks": [
                    {
                        "id": "t2",
                        "title": "收获",
                        "steps": [{"id": "s2", "operation": "harvest_auto", "params": {}}],
                    }
                ],
            },
        )
        assert "t1" in revised["supersededTaskIds"]

        # A todo intent needs a valid trigger.
        _, todo = await server.call_tool(
            "remember_intent",
            {
                "intent": "种子够了就播种",
                "kind": "todo",
                "trigger": {"type": "inventory", "itemId": "(O)472", "minCount": 3},
            },
        )
        assert todo["kind"] == "todo"

        with pytest.raises(ToolError):
            await server.call_tool(
                "remember_intent",
                {"intent": "坏的待办", "kind": "todo", "trigger": {"type": "nope"}},
            )

    asyncio.run(run())


def test_harvest_and_store_uses_authorized_chest_only(mock_scheduler, tmp_path):
    mock_scheduler.run_dir = None

    async def run():
        # harvest_and_store stays a full-surface compatibility tool (removed from light).
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, full=True)
        _, data = await server.call_tool(
            "harvest_and_store", {"chest_x": 70, "chest_y": 12, "max_tiles": 8}
        )
        assert data["outcome"] == "completed"
        assert data["goalSatisfied"] is True
        assert data["stage"] == "done"
        mock_scheduler.harvest_auto.assert_awaited_once_with(max_tiles=8)
        mock_scheduler.deposit_to_chest.assert_awaited_once_with(
            chest_x=70, chest_y=12, item_ids=None
        )

    asyncio.run(run())


def test_internal_dispatch_plan_operation_forwards_stable_command_id(mock_scheduler, tmp_path):
    """The worker dispatch must reach the real method with the persisted id.

    ``call_capability`` is the model-facing schema and has no ``command_id``; using
    it for plan dispatch was the round6 STEP_DISPATCH_FAILED root cause.
    """
    mock_scheduler.run_dir = None
    captured: dict[str, Any] = {}

    async def _execute_water_zone(
        center_x: int,
        center_y: int,
        radius: int = 0,
        include_empty_tiles: bool = False,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        captured["command_id"] = command_id
        return {
            "status": "executed",
            "terminalState": "succeeded",
            "completedCount": 1,
            "effects": [{"tile": {"x": 65, "y": 15}, "state": "watered"}],
        }

    mock_scheduler.execute_water_zone = _execute_water_zone

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="internal")
        command_id = "plan:Save1:t1:s1:attempt-1"
        _, result = await server.call_tool(
            "dispatch_plan_operation",
            {
                "operation": "water_zone",
                "params": {"center_x": 65, "center_y": 15, "radius": 0},
                "command_id": command_id,
            },
        )
        assert result["terminalState"] == "succeeded"
        assert captured["command_id"] == command_id

        # The model surface still rejects the harness-only parameter (the old bug).
        with pytest.raises(ToolError):
            await server.call_tool(
                "call_capability",
                {
                    "tool": "water_zone",
                    "params": {"center_x": 65, "center_y": 15, "radius": 0, "command_id": command_id},
                },
            )

        # Unknown operations are refused by the real validator, not executed.
        with pytest.raises(ToolError):
            await server.call_tool(
                "dispatch_plan_operation",
                {"operation": "run_shell", "params": {"cmd": "rm -rf"}},
            )

    asyncio.run(run())


def test_get_status_detail_keeps_real_native_sections(mock_scheduler):
    """detail=True must not drop the real inventory/weather/wallet sections."""
    mock_scheduler.run_dir = None

    async def run():
        mock_scheduler.get_status = AsyncMock(
            return_value={
                "companion": {
                    "tileX": 64,
                    "tileY": 15,
                    "activity": "idle",
                    "stamina": 270.0,
                    "waterCanLevel": 40,
                    "currentTask": None,
                    "availableMoney": 500,
                    "moneyStatus": "ok",
                },
                "world": {
                    "timeOfDay": 700,
                    "isRaining": False,
                    "weatherIcon": "0",
                    "season": "spring",
                },
                "saveId": "mock-save-123",
                "worldRevision": 1,
            }
        )
        mock_scheduler.latest_snapshot = {
            "payload": {
                "inventory": {
                    "capacity": 12,
                    "freeSlots": 11,
                    "slots": [{"itemId": "(O)472", "name": "Parsnip", "stack": 3, "isTool": False}],
                },
            }
        }
        server = create_mcp_server(scheduler=mock_scheduler)
        _, concise = await server.call_tool("get_status", {})
        # The compact view still carries the real weather/wallet facts.
        assert concise["world"]["weatherIcon"] == "0"
        assert concise["companion"]["availableMoney"] == 500
        assert "inventory" not in concise
        _, detail = await server.call_tool("get_status", {"detail": True})
        assert detail["inventory"]["freeSlots"] == 11
        assert detail["inventory"]["slots"][0]["name"] == "Parsnip"
        assert detail["world"]["weatherIcon"] == "0"
        assert detail["companion"]["availableMoney"] == 500

    asyncio.run(run())
