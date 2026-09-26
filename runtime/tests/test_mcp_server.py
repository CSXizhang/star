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
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import NoActiveTaskError
from stardew_ai_runtime.work_state import WorkStore

# Import MockModTransportServer from test_transport_live
sys.path.insert(0, str(Path(__file__).parent))
from test_transport_live import MockModTransportServer  # noqa: E402


def _grant_decision(tmp_path: Path, scheduler, token: str = "decision-1") -> WorkStore:
    """Begin a fresh provider decision so guarded native tools may select one short job."""
    scheduler.run_dir = str(tmp_path)
    store = WorkStore(tmp_path / "data" / "work-state.json")
    store.begin_decision("mock-save-123", token)
    return store


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
        assert len(tools) == 52
        assert "autonomy_status" in tool_names
        assert "set_autonomy" in tool_names
        assert "query_wiki" in tool_names
        assert "manage_milestones" in tool_names

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


def test_mcp_server_call_water_zone(mock_scheduler, tmp_path):
    """Guarded native tools only select one short job per provider decision."""
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "water_zone", {"center_x": 64, "center_y": 15, "radius": 0}
            )
        assert data["status"] == "job-selected"
        assert data["effectStatus"] == "not_executed_yet"
        assert data["nextBusiness"] == "new_model_decision_required"
        mock_scheduler.execute_water_zone.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "water_zone"
        assert step.params == {"center_x": 64, "center_y": 15, "radius": 0,
                               "include_empty_tiles": False, "detail": False}

        # A second job in the same decision is rejected.
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            with pytest.raises(ToolError, match="当前决策周期已选择过任务"):
                await server.call_tool(
                    "water_zone", {"center_x": 64, "center_y": 15, "radius": 0}
                )
        mock_scheduler.execute_water_zone.assert_not_awaited()

    asyncio.run(run())


def test_mcp_server_call_water_auto(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool("water_auto", {"max_tiles": 15})
        assert data["status"] == "job-selected"
        mock_scheduler.water_auto.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "water_auto"
        assert step.params == {"max_tiles": 15, "include_empty_tiles": False, "detail": False}

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


def test_mcp_server_call_harvest_auto(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool("harvest_auto", {"max_tiles": 10})
        assert data["status"] == "job-selected"
        mock_scheduler.harvest_auto.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "harvest_auto"
        assert step.params == {"max_tiles": 10, "detail": False}

    asyncio.run(run())


def test_mcp_server_call_harvest_auto_default(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool("harvest_auto", {})
        assert data["status"] == "job-selected"
        mock_scheduler.harvest_auto.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "harvest_auto"
        assert step.params == {"max_tiles": 16, "detail": False}

    asyncio.run(run())


def test_mcp_server_call_deposit_to_chest(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "deposit_to_chest",
                {"chest_x": 70, "chest_y": 12, "item_ids": ["(O)24"]},
            )
        assert data["status"] == "job-selected"
        mock_scheduler.deposit_to_chest.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "deposit_to_chest"
        assert step.params == {"chest_x": 70, "chest_y": 12, "item_ids": ["(O)24"], "detail": False}

    asyncio.run(run())


def test_mcp_server_call_deposit_to_chest_without_item_ids(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "deposit_to_chest", {"chest_x": 70, "chest_y": 12}
            )
        assert data["status"] == "job-selected"
        mock_scheduler.deposit_to_chest.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "deposit_to_chest"
        assert step.params == {"chest_x": 70, "chest_y": 12, "item_ids": None, "detail": False}

    asyncio.run(run())


def test_mcp_server_call_withdraw_from_chest(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "withdraw_from_chest",
                {"chest_x": 70, "chest_y": 12, "item_id": "(O)CarrotSeeds", "count": 3},
            )
        assert data["status"] == "job-selected"
        mock_scheduler.withdraw_from_chest.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "withdraw_from_chest"
        assert step.params == {
            "chest_x": 70, "chest_y": 12, "item_id": "(O)CarrotSeeds", "count": 3,
            "items": None, "location_id": "Farm", "detail": False,
        }

    asyncio.run(run())


def test_mcp_server_call_organize_chest(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "organize_chest", {"chest_x": 70, "chest_y": 12}
            )
        assert data["status"] == "job-selected"
        mock_scheduler.organize_chest.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "organize_chest"
        assert step.params == {"chest_x": 70, "chest_y": 12, "detail": False}

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


def test_mcp_server_call_hoe_tiles(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "hoe_tiles", {"tiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}]}
            )
        assert data["status"] == "job-selected"
        mock_scheduler.execute_hoe_tiles.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "hoe_tiles"
        assert step.params == {"tiles": [{"x": 64, "y": 14}, {"x": 65, "y": 14}], "detail": False}

    asyncio.run(run())


def test_mcp_server_call_plant_seeds(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "plant_seeds",
                {"seed_item_id": "(O)472", "tiles": [{"x": 64, "y": 14}]},
            )
        assert data["status"] == "job-selected"
        mock_scheduler.execute_plant_seeds.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "plant_seeds"
        assert step.params == {"seed_item_id": "(O)472", "tiles": [{"x": 64, "y": 14}], "detail": False}

    asyncio.run(run())


def test_mcp_server_call_ship_items(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "ship_items",
                {"items": [{"itemId": "(O)24", "count": 2}]},
            )
        assert data["status"] == "job-selected"
        mock_scheduler.execute_ship_items.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "ship_items"
        assert step.params == {"items": [{"itemId": "(O)24", "count": 2}], "detail": False}

    asyncio.run(run())


def test_mcp_server_call_purchase_items(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "purchase_items",
                {"items": [{"itemId": "(O)472", "count": 2}], "budget_limit": 100},
            )
        assert data["status"] == "job-selected"
        mock_scheduler.execute_purchase_items.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "purchase_items"
        assert step.params == {
            "items": [{"itemId": "(O)472", "count": 2}], "budget_limit": 100,
            "shop_id": "SeedShop", "detail": False, "command_id": None,
        }

    asyncio.run(run())


def test_mcp_server_call_navigate_to(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "navigate_to",
                {"location_id": "Town", "tile_x": 43, "tile_y": 58},
            )
        assert data["status"] == "job-selected"
        mock_scheduler.execute_navigate_to.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "navigate_to"
        assert step.params == {"location_id": "Town", "tile_x": 43, "tile_y": 58,
                               "x": None, "y": None, "tile": None, "landmark": None,
                               "detail": False}

    asyncio.run(run())


def test_mcp_server_call_controls(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)

        # Pause
        store = _grant_decision(tmp_path, mock_scheduler)
        pause_content, pause_data = await server.call_tool("pause_task", {})
        assert len(pause_content) == 1
        assert pause_data["status"] == "paused"
        mock_scheduler.pause_task.assert_awaited_once()
        # Pausing a native job preserves the current model decision.
        assert store.state("mock-save-123").decision["token"] == "decision-1"

        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, resume_data = await server.call_tool("resume_task", {})
        assert resume_data["status"] == "resumed"
        mock_scheduler.resume_task.assert_awaited_once()

        # Cancel
        _, cancel_data = await server.call_tool(
            "cancel_task", {"reason": "MCP test"}
        )
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


def test_cancel_without_active_job_preserves_new_decision_token(tmp_path: Path, mock_scheduler):
    """A new player turn may ask to stop old work before selecting its own job."""
    async def run():
        mock_scheduler.cancel_task.side_effect = NoActiveTaskError("idle")
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler, token="new-turn")

        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "new-turn"}):
            with pytest.raises(ToolError, match="Cannot cancel"):
                await server.call_tool("cancel_task", {})
            assert store.state("mock-save-123").decision["token"] == "new-turn"

            _, selected = await server.call_tool("submit_plan", {
                "goal_text": "浇两格干土",
                "tasks": [{
                    "id": "new-task", "title": "浇水",
                    "steps": [{"id": "new-step", "operation": "water_auto", "params": {"max_tiles": 2}}],
                }],
            })

        assert selected["tasks"][0]["id"] == "new-task"
        assert store.state("mock-save-123").decision["taskId"] == "new-task"
        mock_scheduler.water_auto.assert_not_awaited()

    asyncio.run(run())


def test_cancel_selected_pending_job_revokes_dispatch_authority(tmp_path: Path, mock_scheduler):
    """Cancelling a selected but not yet dispatched job must stop its worker claim."""
    async def run():
        mock_scheduler.cancel_task.side_effect = NoActiveTaskError("idle")
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler, token="selected-turn")
        store.submit_plan(
            "mock-save-123", goal_text="旧作业", decision_token="selected-turn",
            tasks=[{
                "id": "selected-task", "title": "旧浇水",
                "steps": [{"id": "s1", "operation": "water_auto", "params": {"max_tiles": 2}}],
            }],
        )
        assert store.has_ready_step("mock-save-123")

        with pytest.raises(ToolError, match="Cannot cancel"):
            await server.call_tool("cancel_task", {})

        assert store.state("mock-save-123").decision == {}
        assert not store.has_ready_step("mock-save-123")
        mock_scheduler.water_auto.assert_not_awaited()

    asyncio.run(run())


def test_mcp_server_error_handling(mock_scheduler, tmp_path):
    async def run():
        from stardew_ai_runtime.scheduler import PolicyViolationError, SchedulerError

        # Guarded native tools surface guard rejections (no live decision) as ToolError.
        server = create_mcp_server(scheduler=mock_scheduler)
        with pytest.raises(ToolError, match="决策令牌缺失或不匹配"):
            await server.call_tool(
                "water_zone", {"center_x": -1, "center_y": 15, "radius": 0}
            )

        # Read-only tools keep executing directly and map scheduler errors.
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

        # Policy violations from the real scheduler during plan dispatch map to ToolError.
        mock_scheduler.run_dir = None
        internal = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="internal")
        store = WorkStore(tmp_path / "data" / "work-state.json")
        store.begin_decision("mock-save-123", "decision-eh")
        store.submit_plan(
            "mock-save-123",
            goal_text="water",
            decision_token="decision-eh",
            tasks=[{
                "id": "t1",
                "title": "water",
                "steps": [{"id": "s1", "operation": "water_zone",
                           "params": {"center_x": -1, "center_y": 15, "radius": 0}}],
            }],
        )
        store.claim_next_step("mock-save-123", "worker-eh")
        command_id = "plan:mock-save-123:t1:s1:attempt-1"
        store.assign_command_id("mock-save-123", "t1", "s1", command_id)

        async def _rejecting_water_zone(**kwargs):
            raise PolicyViolationError("Invalid coordinates")

        mock_scheduler.execute_water_zone = _rejecting_water_zone
        with pytest.raises(ToolError, match="Invalid coordinates"):
            await internal.call_tool(
                "dispatch_plan_operation",
                {
                    "operation": "water_zone",
                    "params": {"center_x": -1, "center_y": 15, "radius": 0},
                    "command_id": command_id,
                },
            )

    asyncio.run(run())


def test_mcp_server_new_tools_error_mapping(mock_scheduler, tmp_path):
    """Two-layer validation: short-job budget rules reject at selection time;
    execution-time parameter validation defers to plan dispatch."""
    async def run():
        from stardew_ai_runtime.scheduler import SchedulerError

        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        env = {"STARDEW_DECISION_TOKEN": "decision-1"}

        # Selection-time budget violation surfaces the guard's ToolError.
        with pytest.raises(ToolError, match="Short job exceeds 64 native targets"):
            await server.call_tool("harvest_auto", {"max_tiles": 100})

        # Execution-time policy violations pass selection and become a selected job.
        for tool_name, args in [
            ("deposit_to_chest", {"chest_x": -1, "chest_y": 12}),
            ("withdraw_from_chest", {"chest_x": 70, "chest_y": 12}),
            ("organize_chest", {"chest_x": 70, "chest_y": 12}),
            ("hoe_tiles", {"tiles": []}),
            ("plant_seeds", {"seed_item_id": "", "tiles": [{"x": 64, "y": 15}]}),
            ("ship_items", {"items": []}),
        ]:
            store.begin_decision("mock-save-123", "decision-1")
            with patch.dict(os.environ, env):
                _, data = await server.call_tool(tool_name, args)
            assert data["status"] == "job-selected", f"{tool_name} should be selectable"
        mock_scheduler.deposit_to_chest.assert_not_awaited()
        mock_scheduler.harvest_auto.assert_not_awaited()

        # Read-only query error mapping stays direct.
        mock_scheduler.query_inventory.side_effect = SchedulerError(
            "World snapshot does not include an 'inventory' section"
        )
        with pytest.raises(ToolError, match="Failed to query inventory"):
            await server.call_tool("query_inventory", {})

    asyncio.run(run())


def test_mcp_server_state_closing_loop_fresh_and_details(mock_scheduler, tmp_path):
    """Post-action fresh-snapshot closure is not part of the model surface anymore:
    native tool calls only select a short job, and the harness executes it later."""
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool("harvest_auto", {"max_tiles": 3})
        assert data["status"] == "job-selected"
        assert "fresh" not in data
        assert "inventory" not in data
        assert "details" not in data
        mock_scheduler.harvest_auto.assert_not_awaited()
        mock_scheduler.wait_for_fresh_snapshot.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "harvest_auto"
        assert step.params == {"max_tiles": 3, "detail": False}

    asyncio.run(run())


def test_mcp_server_stdio_integration_with_mock_transport(tmp_path: Path):
    """End-to-end integration test spawning MCP server process via stdio client against mock server.

    NOTE: Stdio loopback tests use MockModTransportServer on loopback
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

        # Pre-begin a provider decision in the subprocess run dir and export the
        # matching token so the guarded native tool can select one short job.
        store = WorkStore(tmp_path / "data" / "work-state.json")
        store.begin_decision("mock-save-hash-123", "stdio-decision-1")

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "stardew_ai_runtime.mcp_server", "--run-dir", str(tmp_path)],
            env={**os.environ, "STARDEW_DECISION_TOKEN": "stdio-decision-1"},
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

                    # 4. Call water_auto: selects one short job, does not execute
                    auto_res = await session.call_tool("water_auto", {"max_tiles": 5})
                    assert not auto_res.isError
                    auto_data = json.loads(auto_res.content[0].text)
                    assert auto_data["status"] == "job-selected"
                    assert auto_data["effectStatus"] == "not_executed_yet"
                    assert auto_data["nextBusiness"] == "new_model_decision_required"

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
    """Composite planting workflows are rejected on the model surface: one short
    job may contain a single business kind only."""
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        with pytest.raises(ToolError, match="MULTIPLE_BUSINESSES"):
            await server.call_tool(
                "plant_crop_workflow",
                {"crop_name_or_id": "Parsnip", "count": 3},
            )
        mock_scheduler.plant_crop_workflow.assert_not_called()

    asyncio.run(run())


def test_mcp_server_call_navigate_to_with_landmark(mock_scheduler, tmp_path):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "navigate_to",
                {"location_id": "Farm", "landmark": "shipping_bin"},
            )
        assert data["status"] == "job-selected"
        mock_scheduler.execute_navigate_to.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "navigate_to"
        assert step.params == {"location_id": "Farm", "landmark": "shipping_bin",
                               "tile_x": None, "tile_y": None, "x": None, "y": None,
                               "tile": None, "detail": False}

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


def test_mcp_server_navigate_to_xy_and_executing_status(mock_scheduler, tmp_path):
    """Executing/running propagation is a harness-side concern now: the model
    surface only selects the navigation job."""
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool(
                "navigate_to", {"location_id": "Farm", "x": 61, "y": 17}
            )
        assert data["status"] == "job-selected"
        mock_scheduler.execute_navigate_to.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "navigate_to"
        assert step.params == {"location_id": "Farm", "x": 61, "y": 17,
                               "tile_x": None, "tile_y": None, "tile": None,
                               "landmark": None, "detail": False}

    asyncio.run(run())


def test_mcp_server_water_auto_blocked_targets_actionable_summary(mock_scheduler, tmp_path):
    """Blocked-target summaries belong to the execution result; selection records
    the request only."""
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, data = await server.call_tool("water_auto", {"max_tiles": 25})
        assert data["status"] == "job-selected"
        assert "blockedTargets" not in data
        assert "actionableSummary" not in data
        mock_scheduler.water_auto.assert_not_awaited()
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "water_auto"
        assert step.params == {"max_tiles": 25, "include_empty_tiles": False, "detail": False}

    asyncio.run(run())


def test_mcp_server_executing_status_propagation(mock_scheduler, tmp_path):
    """Every guarded native tool selects a job without executing; none of them
    touches the scheduler."""
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        store = _grant_decision(tmp_path, mock_scheduler)
        for tool_name, args in [
            ("water_zone", {"center_x": 60, "center_y": 20, "radius": 1}),
            ("harvest_auto", {"max_tiles": 10}),
            ("deposit_to_chest", {"chest_x": 59, "chest_y": 17}),
            ("withdraw_from_chest", {"chest_x": 59, "chest_y": 17, "item_id": "seed"}),
            ("hoe_tiles", {"tiles": [{"x": 60, "y": 20}]}),
            ("plant_seeds", {"seed_item_id": "seed", "tiles": [{"x": 60, "y": 20}]}),
        ]:
            store.begin_decision("mock-save-123", "decision-1")
            with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
                _, data = await server.call_tool(tool_name, args)
            assert data["status"] == "job-selected", f"{tool_name} should only select"
            assert data["effectStatus"] == "not_executed_yet"
            assert data["nextBusiness"] == "new_model_decision_required"
        mock_scheduler.execute_water_zone.assert_not_awaited()
        mock_scheduler.harvest_auto.assert_not_awaited()
        mock_scheduler.deposit_to_chest.assert_not_awaited()
        mock_scheduler.withdraw_from_chest.assert_not_awaited()
        mock_scheduler.execute_hoe_tiles.assert_not_awaited()
        mock_scheduler.execute_plant_seeds.assert_not_awaited()

    asyncio.run(run())


def test_mcp_work_plan_runs_step_and_goes_idle(mock_scheduler, tmp_path):
    """Harness worker claims a ready step, executes the real scheduler op, and commits."""
    mock_scheduler.run_dir = None

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)

        _, idle = await server.call_tool("run_next_step", {})
        assert idle["status"] == "idle"
        assert idle["hasExecutableWork"] is False

        _, goal = await server.call_tool("remember_intent", {"intent": "照料农场", "kind": "goal"})
        assert goal["goal"]["source"] == "agent"
        goal_id = goal["goal"]["id"]

        store = WorkStore(tmp_path / "data" / "work-state.json")
        store.begin_decision("mock-save-123", "decision-wp")
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-wp"}):
            _, plan = await server.call_tool(
                "submit_plan",
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

        # call_capability runs the same guarded implementation: with a live decision
        # it selects the navigation job instead of executing it.
        calls_before = mock_scheduler.execute_navigate_to.await_count
        store = _grant_decision(tmp_path, mock_scheduler)
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": "decision-1"}):
            _, invoked = await light_server.call_tool(
                "call_capability",
                {"tool": "navigate_to", "params": {"location_id": "Farm", "x": 61, "y": 17}},
            )
        assert invoked["tool"] == "navigate_to"
        assert invoked["result"]["status"] == "job-selected"
        assert mock_scheduler.execute_navigate_to.await_count == calls_before
        # call_capability passes params through without schema default injection.
        step = store.state("mock-save-123").tasks[0].steps[0]
        assert step.operation == "navigate_to"
        assert step.params == {"location_id": "Farm", "x": 61, "y": 17}

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
    """submit_plan selects one short job per provider decision; remember_intent records
    agent goals/todos only. Revising a plan requires a fresh decision."""
    mock_scheduler.run_dir = None

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, full=False)

        _, goal = await server.call_tool(
            "remember_intent", {"intent": "把农场种满胡萝卜", "kind": "goal"}
        )
        assert goal["kind"] == "goal"
        assert goal["goal"]["source"] == "agent"

        store = WorkStore(tmp_path / "data" / "work-state.json")
        store.begin_decision("mock-save-123", "decision-sp")
        env = {"STARDEW_DECISION_TOKEN": "decision-sp"}
        with patch.dict(os.environ, env):
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

        # One short job per decision: revising in the same decision is rejected.
        with patch.dict(os.environ, env):
            with pytest.raises(ToolError, match="当前决策周期已选择过任务"):
                await server.call_tool(
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

        # A fresh decision may revise: the still-pending task is superseded.
        store.begin_decision("mock-save-123", "decision-sp")
        with patch.dict(os.environ, env):
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


def test_harvest_and_store_uses_authorized_chest_only(mock_scheduler):
    """harvest_and_store stays a full-surface compatibility tool that is not
    selectable as a short job (harvest and deposit are two business steps)."""
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler, full=True)
        with pytest.raises(ToolError, match="SHORT_JOB_UNSUPPORTED"):
            await server.call_tool(
                "harvest_and_store", {"chest_x": 70, "chest_y": 12, "max_tiles": 8}
            )
        mock_scheduler.harvest_auto.assert_not_called()
        mock_scheduler.deposit_to_chest.assert_not_called()

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
    server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="internal")
    store = WorkStore(tmp_path / "data" / "work-state.json")
    store.begin_decision("mock-save-123", "decision-id")
    store.submit_plan(
        "mock-save-123",
        goal_text="water",
        decision_token="decision-id",
        tasks=[{
            "id": "t1",
            "title": "water",
            "steps": [{"id": "s1", "operation": "water_zone",
                       "params": {"center_x": 65, "center_y": 15, "radius": 0}}],
        }],
    )
    store.claim_next_step("mock-save-123", "worker1")
    command_id = "plan:mock-save-123:t1:s1:attempt-1"
    store.assign_command_id("mock-save-123", "t1", "s1", command_id)

    async def run():
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


# ---------------------------------------------------------------- manage_milestones (contract §3.2)
def test_manage_milestones_aliases_missing_and_conflicting_ids(mock_scheduler, tmp_path, monkeypatch):
    async def run():
        mock_scheduler.run_dir = None
        mock_scheduler.latest_snapshot = {"payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 11}}}
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        tools = {tool.name: tool for tool in await server.list_tools()}
        schema = tools["manage_milestones"].inputSchema
        assert {"node_id", "id", "nodeId"} <= set(schema["properties"])
        assert '"node_id"' in tools["manage_milestones"].description
        monkeypatch.setenv("STARDEW_LIFE_MODE", "plan")
        with pytest.raises(ToolError, match="NODE_ID_REQUIRED.*node_id"):
            await server.call_tool("manage_milestones", {"action": "adopt"})
        with pytest.raises(ToolError, match="CONFLICTING_NODE_ID"):
            await server.call_tool("manage_milestones", {"action": "adopt", "node_id": "a", "id": "b"})
        todo_ids = None
        for key in ("id", "nodeId", "node_id"):
            _, saved = await server.call_tool("manage_milestones", {
                "action": "adopt" if todo_ids is None else "revise",
                key: "spring-egg-festival-strawberry:y1", "planned_count": 5,
            })
            assert saved["saved"] is True
            assert saved["execution"] == "not_started_by_this_tool"
            assert saved["node"]["plannedCount"] == 5
            assert any(p["support"] == "manual" for p in saved["node"]["prepItems"])
            if todo_ids is None:
                todo_ids = saved["todoIds"]
        with pytest.raises(ToolError, match="unknown"):
            await server.call_tool("manage_milestones", {"action": "adopt", "id": "not-a-node"})
        monkeypatch.setenv("STARDEW_LIFE_MODE", "chat")
        with pytest.raises(ToolError, match="PLAN_MODE_REQUIRED"):
            await server.call_tool("manage_milestones", {"action": "adopt", "id": "spring-egg-festival-strawberry:y1"})
    asyncio.run(run())


def test_manage_milestones_write_actions_require_plan_mode(mock_scheduler, monkeypatch):
    async def run():
        server = create_mcp_server(scheduler=mock_scheduler)
        monkeypatch.delenv("STARDEW_LIFE_MODE", raising=False)
        # list is always allowed, even in casual chat.
        _, result = await server.call_tool("manage_milestones", {"action": "list"})
        assert result["saveId"] == "mock-save-123"
        assert result["nodes"] == []
        # adopt without plan mode is rejected with guidance to the plan mode.
        with pytest.raises(ToolError, match="PLAN_MODE_REQUIRED"):
            await server.call_tool(
                "manage_milestones",
                {"action": "adopt", "node_id": "spring-egg-festival-strawberry:y1"},
            )
        with pytest.raises(ToolError, match="PLAN_MODE_REQUIRED"):
            await server.call_tool(
                "manage_milestones",
                {"action": "propose", "title": "自定义", "target_date": "1:spring:13"},
            )
        with pytest.raises(ToolError, match="unsupported milestones action"):
            await server.call_tool("manage_milestones", {"action": "explode"})

    asyncio.run(run())


def test_manage_milestones_adopt_wires_goal_todo_without_touching_budget(
    mock_scheduler, tmp_path, monkeypatch
):
    from stardew_ai_runtime.autonomy import AutonomyController

    mock_scheduler.run_dir = None
    mock_scheduler.latest_snapshot = {
        "payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 11}}
    }
    autonomy = AutonomyController(tmp_path / "data" / "autonomy-state.json")
    autonomy.set_preferences(
        "mock-save-123", goal="优先赚钱", budget_limit=500, box_preference="shipping"
    )

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        monkeypatch.setenv("STARDEW_LIFE_MODE", "plan")
        _, result = await server.call_tool("manage_milestones", {
            "action": "adopt",
            "node_id": "spring-egg-festival-strawberry:y1",
            "reserved_funds": 1000,
            "planned_count": 10,
            "terms_note": "预留1000g只用于买种子",
        })
        node = result["node"]
        assert node["status"] == "adopted"
        assert node["reservedFunds"] == 1000
        assert node["plannedCount"] == 10
        assert result["goalId"]
        assert set(result["todoIds"]) == {"pre-till", "plant-after"}

        store = WorkStore(tmp_path / "data" / "work-state.json")
        goal = next(g for g in store.list_goals("mock-save-123") if g["id"] == result["goalId"])
        assert goal["source"] == "user"
        assert goal["constraints"]["reservedFunds"] == 1000
        assert "不冻结资金" in goal["constraints"]["reservedFundsNote"]
        todos = {t["id"]: t for t in store.list_todos("mock-save-123")}
        pre_till = todos[result["todoIds"]["pre-till"]]
        assert pre_till["trigger"] == {"type": "calendar", "year": 1, "season": "spring", "day": 12}
        assert pre_till["expiry"] == {"year": 1, "season": "spring", "day": 13}
        plant_after = todos[result["todoIds"]["plant-after"]]
        assert plant_after["trigger"] == {"type": "calendar", "year": 1, "season": "spring", "day": 13}

        # The one-time reservation never becomes a per-day purchase budget.
        state = autonomy.state("mock-save-123")
        assert state.budget_limit == 500
        assert state.daily_spend == 0
        # No decision/plan machinery was involved.
        assert store.state("mock-save-123").decision == {}

    asyncio.run(run())


def test_manage_milestones_defer_cancels_todos_and_pauses_goal(
    mock_scheduler, tmp_path, monkeypatch
):
    mock_scheduler.run_dir = None
    mock_scheduler.latest_snapshot = {
        "payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 11}}
    }

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        monkeypatch.setenv("STARDEW_LIFE_MODE", "plan")
        _, adopted = await server.call_tool("manage_milestones", {
            "action": "adopt",
            "node_id": "spring-egg-festival-strawberry:y1",
            "reserved_funds": 1000,
        })
        _, deferred = await server.call_tool("manage_milestones", {
            "action": "defer",
            "node_id": "spring-egg-festival-strawberry:y1",
            "reason": "先攒钱",
        })
        assert deferred["node"]["status"] == "deferred"
        store = WorkStore(tmp_path / "data" / "work-state.json")
        todos = {t["id"]: t for t in store.list_todos("mock-save-123")}
        assert all(todos[tid]["status"] == "cancelled" for tid in adopted["todoIds"].values())
        goal = next(g for g in store.list_goals("mock-save-123") if g["id"] == adopted["goalId"])
        assert goal["status"] == "paused"

    asyncio.run(run())


def test_manage_milestones_on_life_surface_is_not_job_wrapped(
    mock_scheduler, tmp_path, monkeypatch
):
    mock_scheduler.run_dir = None
    mock_scheduler.latest_snapshot = {
        "payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 11}}
    }

    async def run():
        monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler)
        tools = {t.name for t in await server.list_tools()}
        assert "manage_milestones" in tools
        monkeypatch.setenv("STARDEW_LIFE_MODE", "plan")
        _, result = await server.call_tool("manage_milestones", {
            "action": "adopt",
            "node_id": "spring-egg-festival-strawberry:y1",
        })
        # Exempt from protect_job: a real node result, not a "job-selected" wrapper.
        assert result["node"]["status"] == "adopted"
        assert result.get("status") != "job-selected"
        _, listed = await server.call_tool("manage_milestones", {"action": "list"})
        assert listed["nodes"][0]["status"] == "adopted"

    asyncio.run(run())
