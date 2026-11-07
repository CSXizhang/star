"""Atomic native tool registration on the existing MCP server and scheduler."""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import BeforeValidator, Field

from stardew_ai_runtime.mcp_contract import (
    ChestCoordinate,
    ItemCount,
    ItemQuantity,
    SpatialRegion,
    TileXY,
    _item_dicts,
    _native_action_response,
    _tile_dicts,
    _unwrap_item_array,
)
from stardew_ai_runtime.mcp_contract import (
    _safe_wait_for_fresh_snapshot as _safe_wait_for_fresh_snapshot,
)
from stardew_ai_runtime.scheduler import (
    CompanionScheduler,
    PolicyViolationError,
    SchedulerError,
    extract_chest_summary,
    extract_non_tool_items,
)
from stardew_ai_runtime.work_state import (
    PLANT_WORKFLOW_PLAN_ERROR,
)

logger = logging.getLogger("stardew_ai_runtime.mcp_server")


def register_native_tools(mcp: FastMCP, sched: CompanionScheduler, record_observation_recovery, work_for_run, run_dir=None) -> None:
    @mcp.tool()
    async def query_planting_options(detail: bool = False, location_id: str = "Farm", region: SpatialRegion | None = None) -> dict[str, Any]:
        """Query companion seeds, season, tillable dirt and tilled empty tiles for planting.

        Compare connected regular field regions with capacity, crop/dead state, clearing
        tools, reserved paths and actual path distances to water/storage. Choose and
        save one region in goal.project.farmRegion; pass its bounds here after travel,
        refill and clearing. detail=True includes ordered tile batches. No live crop
        removal or tree clearing is assumed. A region is advisory, not action approval.
        """
        try:
            return await sched.query_planting_options(detail=detail, location_id=location_id,
                                                    region=region.model_dump() if isinstance(region, SpatialRegion) else region)
        except SchedulerError as ex:
            raise ToolError(f"Failed to query planting options: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error querying planting options: {ex}") from None

    @mcp.tool()
    async def query_shop(
        shop_id: str = "SeedShop",
        item_id: str | None = None,
        name: str | None = None,
        is_seed: bool | None = None,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Query ordinary item shop stock (animal catalog: observe_building_services), prices, operating status, locationId,
        interactionTile, and available money.

        Defaults to Pierre's General Store ('SeedShop').
        Supports filtering items by item_id, name, or is_seed to avoid dumping full catalogs.
        Returns locationId, counter interactionTile, concise item list with price & stock;
        pass detail=True for full trade & purchase details.
        """
        try:
            kwargs: dict[str, Any] = {"shop_id": shop_id, "detail": detail}
            if item_id is not None:
                kwargs["item_id"] = item_id
            if name is not None:
                kwargs["name"] = name
            if is_seed is not None:
                kwargs["is_seed"] = is_seed
            res = await sched.query_shop(**kwargs)
            res = dict(res)

            items = res.get("items")
            if isinstance(items, list):
                filtered_items = []
                for it in items:
                    iid = str(it.get("itemId", ""))
                    iname = str(it.get("name", ""))
                    if item_id is not None:
                        tid = item_id.strip().lower()
                        if iid.lower() != tid and not iid.lower().endswith(tid):
                            continue
                    if name is not None:
                        if name.strip().lower() not in iname.lower():
                            continue
                    if is_seed is not None:
                        native_flag = it.get("isSeed")
                        is_it_seed = native_flag if isinstance(native_flag, bool) else (
                            it.get("category") == -74
                            or "seed" in iname.lower()
                            or "starter" in iname.lower()
                            or "spores" in iname.lower()
                            or "seed" in iid.lower()
                        )
                        if is_it_seed != is_seed:
                            continue
                    filtered_items.append(it)

                has_filter = (item_id is not None) or (name is not None) or (is_seed is not None)
                if not detail and not has_filter and len(filtered_items) > 15:
                    res["items"] = filtered_items[:15]
                    res["itemsTruncated"] = True
                else:
                    res["items"] = filtered_items
                    res["itemsTruncated"] = False
                res["itemsCount"] = len(res["items"])
            return res
        except SchedulerError as ex:
            raise ToolError(f"Failed to query shop: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error querying shop: {ex}") from None

    @mcp.tool()
    async def harvest_auto(
        max_tiles: int = 16, detail: bool = False,
        target_tiles: Annotated[list[TileXY] | None, BeforeValidator(_unwrap_item_array), Field(description="Optional exact candidate coordinates [{x:62,y:27},{x:66,y:23}]. Intersect with freshly observed mature crops; omitted/null uses observed work, [] selects no work. Coordinates outside this list are never selected.")] = None,
        location_id: str | None = None,
    ) -> dict[str, Any]:
        """Harvest up to max_tiles observed mature crop targets using their native harvest method.

        target_tiles bounds candidates to freshly observed mature crops; [] selects
        no work. Omitted/null keeps ordinary auto selection. location_id, if given,
        must match the observed work map. Returns remaining count and backpack space.
        Observed matureCrops.requiresScythe=false identifies hand-pickable crops;
        true requires a scythe, absent/null is unknown. For a hand-only request,
        select explicit observed false targets; no wiki lookup is needed for this fact.
        The real player hand-picks ordinary crops and swings an owned scythe for
        scythe crops. Native arcs can also harvest neighboring crops; all observed
        harvested tiles are reported once. Scythe drops enter the bag natively or
        remain on the ground: per-tile stack is not a yield estimate. inventoryDelta
        reports measured backpack changes. A full bag can still accept stacks;
        an actual native capacity refusal stops hand harvesting without discarding.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            auto_kwargs = {"max_tiles": max_tiles}
            if target_tiles is not None:
                auto_kwargs["target_tiles"] = _tile_dicts(target_tiles)
            if location_id is not None:
                auto_kwargs["location_id"] = location_id
            res = await sched.harvest_auto(**auto_kwargs)
            if res.get("status") == "no-work":
                return {
                    "status": "no-work",
                    "terminalState": "none",
                    "targetCount": 0,
                    "remainingMatureCount": 0,
                    "fresh": True,
                    "message": res.get("message", "No mature crops ready for harvest."),
                }

            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "targetCount": res.get("targetCount", 0),
                    "completedCount": res.get("completedCount", 0),
                    "remainingMatureCount": res.get("remainingMatureCount"),
                    "fresh": False,
                    "message": res.get("message") or f"Harvest task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)
            remaining_mature = None
            inv_summary: dict[str, Any] = {"fresh": False}
            if fresh and fresh_snap:
                payload = fresh_snap.get("payload", {})
                fw = payload.get("farmWork") or {}
                remaining_mature = fw.get("matureCropCount")
                inv = payload.get("inventory") or {}
                inv_summary = {
                    "capacity": inv.get("capacity", 0),
                    "freeSlots": inv.get("freeSlots", 0),
                    "nonToolItems": extract_non_tool_items(inv.get("slots", [])),
                    "fresh": True,
                }

            response = {
                "status": "executed",
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "targetCount": res.get("targetCount", 0),
                "remainingMatureCount": remaining_mature,
                "inventory": inv_summary,
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if detail:
                response["targetTiles"] = res.get("targetTiles", [])
                response["effects"] = res.get("effects", [])
                response["isTruncated"] = res.get("isTruncated", False)
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Auto harvest rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Auto harvest failed: {ex}") from None

    @mcp.tool()
    async def deposit_to_chest(
        chest_x: ChestCoordinate, chest_y: ChestCoordinate, item_ids: list[str] | None = None, detail: bool = False,
        location_id: str = "Farm",
        items: Annotated[list[ItemQuantity] | None, BeforeValidator(_unwrap_item_array)] = None,
    ) -> dict[str, Any]:
        """Deposit into chest at (chest_x, chest_y): use either items [{itemId,count}] for exact quantities or item_ids for whole stacks, never both.

        Native player mode requires items or item_ids and can store explicitly
        chosen tools. The model decides what to retain, store or ship; no default
        player cleanup is selected. Companion mode retains its non-tool default.

        Use items [{itemId, count}] for exact total quantities across backpack
        stacks in inventory order, preserving native quality and splitting stacks.
        Native quality codes are 0=normal, 1=silver, 2=gold, 4=iridium.
        Alternatively item_ids filters whole stacks of the chosen identities.
        Specify either items or item_ids. A shortage or full chest is partial,
        with requestedQuantity, actualQuantity and remainingQuantity in details.
        Returns updated chest and backpack status.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            res = await sched.deposit_to_chest(
                chest_x=chest_x,
                chest_y=chest_y,
                item_ids=item_ids,
                location_id=location_id,
                **({"items": _item_dicts(items)} if items is not None else {}),
            )

            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "completedCount": res.get("completedCount", 0),
                    "fresh": False,
                    "message": res.get("message") or f"Deposit task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)
            chest_summary: dict[str, Any] = {"tile": {"x": chest_x, "y": chest_y}, "fresh": False}
            companion_summary: dict[str, Any] = {"fresh": False}
            if fresh and fresh_snap:
                payload = fresh_snap.get("payload", {})
                chests = payload.get("chests", {}).get("items", [])
                target_chest = next(
                    (
                        c
                        for c in chests
                        if c.get("tile", {}).get("x") == chest_x
                        and c.get("tile", {}).get("y") == chest_y
                    ),
                    None,
                )
                inv = payload.get("inventory") or {}
                non_tool_items = extract_non_tool_items(inv.get("slots", []))
                chest_summary = extract_chest_summary(
                    target_chest, chest_x, chest_y, non_tool_items, detail=detail
                )
                companion_summary = {
                    "capacity": inv.get("capacity", 0),
                    "freeSlots": inv.get("freeSlots", 0),
                    "nonToolItems": non_tool_items,
                    "fresh": True,
                }

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "chest": chest_summary,
                "companion": companion_summary,
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = res.get("effects", [])
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Deposit to chest rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Deposit to chest failed: {ex}") from None

    @mcp.tool()
    async def withdraw_from_chest(
        chest_x: ChestCoordinate,
        chest_y: ChestCoordinate,
        item_id: str | None = None,
        count: ItemCount = 1,
        items: Annotated[list[ItemQuantity] | None, BeforeValidator(_unwrap_item_array)] = None,
        location_id: str = "Farm",
        detail: bool = False,
    ) -> dict[str, Any]:
        """Withdraw items from chest at (chest_x, chest_y) into companion backpack.

        Specify either item_id and count, or a list of items [{itemId, count}].
        Count is the total number of items, spanning matching quality stacks in
        chest order; partial stacks can be split. Each transferred stack retains
        its native quality. A shortage is partial, with remainingQuantity.
        Native quality codes are 0=normal, 1=silver, 2=gold, 4=iridium;
        stack order can change the quality distribution between backpack and chest.
        Returns updated chest and backpack status.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            res = await sched.withdraw_from_chest(
                chest_x=chest_x,
                chest_y=chest_y,
                items=_item_dicts(items),
                item_id=item_id,
                count=count,
                location_id=location_id,
            )

            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "completedCount": res.get("completedCount", 0),
                    "fresh": False,
                    "message": res.get("message") or f"Withdraw task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)
            chest_summary: dict[str, Any] = {"tile": {"x": chest_x, "y": chest_y}, "fresh": False}
            companion_summary: dict[str, Any] = {"fresh": False}
            if fresh and fresh_snap:
                payload = fresh_snap.get("payload", {})
                chests = payload.get("chests", {}).get("items", [])
                target_chest = next(
                    (
                        c
                        for c in chests
                        if c.get("tile", {}).get("x") == chest_x
                        and c.get("tile", {}).get("y") == chest_y
                    ),
                    None,
                )
                inv = payload.get("inventory") or {}
                non_tool_items = extract_non_tool_items(inv.get("slots", []))
                chest_summary = extract_chest_summary(
                    target_chest, chest_x, chest_y, non_tool_items, detail=detail
                )
                companion_summary = {
                    "capacity": inv.get("capacity", 0),
                    "freeSlots": inv.get("freeSlots", 0),
                    "slots": inv.get("slots", []),
                    "nonToolItems": non_tool_items,
                    "fresh": True,
                }

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "chest": chest_summary,
                "companion": companion_summary,
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = res.get("effects", [])
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Withdraw from chest rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Withdraw from chest failed: {ex}") from None

    @mcp.tool()
    async def organize_chest(chest_x: ChestCoordinate, chest_y: ChestCoordinate, detail: bool = False) -> dict[str, Any]:
        """Merge same-item stacks inside chest at (chest_x, chest_y). Returns chest status."""
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            res = await sched.organize_chest(chest_x=chest_x, chest_y=chest_y)

            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "fresh": False,
                    "message": res.get("message") or f"Organize task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)
            chest_summary: dict[str, Any] = {"tile": {"x": chest_x, "y": chest_y}, "fresh": False}
            if fresh and fresh_snap:
                payload = fresh_snap.get("payload", {})
                chests = payload.get("chests", {}).get("items", [])
                target_chest = next(
                    (
                        c
                        for c in chests
                        if c.get("tile", {}).get("x") == chest_x
                        and c.get("tile", {}).get("y") == chest_y
                    ),
                    None,
                )
                chest_summary = extract_chest_summary(
                    target_chest, chest_x, chest_y, None, detail=detail
                )

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "chest": chest_summary,
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = res.get("effects", [])
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Organize chest rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Organize chest failed: {ex}") from None

    @mcp.tool()
    async def hoe_tiles(
        tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], detail: bool = False,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Use hoe on the specified non-empty list of dirt tiles to till them.

        Protects existing crops and objects.
        Returns terminalState, counts, affectedTiles, candidateTiles (fresh=True/False), and status.
        """
        pre_rev_getter = getattr(sched, "latest_world_revision", 0)
        pre_rev = pre_rev_getter() if callable(pre_rev_getter) else int(pre_rev_getter)
        try:
            res = await sched.execute_hoe_tiles(tiles=_tile_dicts(tiles), location_id=location_id)

            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "completedCount": res.get("completedCount", 0),
                    "fresh": False,
                    "message": res.get("message") or f"Hoe task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)

            # Updated candidates from fresh snapshot; if fresh=False, report unknown instead of math
            if fresh and fresh_snap:
                planting_payload = fresh_snap.get("payload", {}).get("planting") or {}
                c_tiles = planting_payload.get("candidateTiles") or {}
                candidate_summary = {
                    "tilledEmptyCount": c_tiles.get("tilledEmptyCount", "unknown"),
                    "tillableCount": c_tiles.get("tillableCount", "unknown"),
                    "fresh": True,
                }
            else:
                candidate_summary = {
                    "tilledEmptyCount": "unknown",
                    "tillableCount": "unknown",
                    "fresh": False,
                }

            effects = res.get("effects", [])
            affected_tiles = [
                e["tile"]
                for e in effects
                if isinstance(e, dict) and e.get("state") == "hoed" and "tile" in e
            ]
            if not affected_tiles and isinstance(res.get("details"), dict):
                affected_tiles = res["details"].get("hoedTiles", [])

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "affectedTiles": affected_tiles,
                "candidateTiles": candidate_summary,
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = effects
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Hoe tiles rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Hoe tiles failed: {ex}") from None

    @mcp.tool()
    async def plant_seeds(
        seed_item_id: str, tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], detail: bool = False,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Plant specified seeds into the declared tilled empty tiles.

        Consumes seeds from companion backpack. Protects existing crops.
        Returns terminalState, counts, plantedTiles, remainingSeedStack, and status.
        """
        pre_rev_getter = getattr(sched, "latest_world_revision", 0)
        pre_rev = pre_rev_getter() if callable(pre_rev_getter) else int(pre_rev_getter)
        try:
            res = await sched.execute_plant_seeds(seed_item_id=seed_item_id, tiles=_tile_dicts(tiles), location_id=location_id)

            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "completedCount": res.get("completedCount", 0),
                    "fresh": False,
                    "message": res.get("message") or f"Planting task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)

            # Updated remaining stack and candidates from fresh snapshot;
            # if fresh=False, report unknown instead of math
            if fresh and fresh_snap:
                planting_payload = fresh_snap.get("payload", {}).get("planting") or {}
                seeds_list = planting_payload.get("seeds") or []
                matched_seed = next(
                    (
                        s
                        for s in seeds_list
                        if str(s.get("itemId", "")).lower() == seed_item_id.lower()
                        or str(s.get("name", "")).lower() == seed_item_id.lower()
                    ),
                    None,
                )
                remaining_seed_stack: int | str = (
                    matched_seed.get("stack", 0) if matched_seed else 0
                )
                c_tiles = planting_payload.get("candidateTiles") or {}
                candidate_summary = {
                    "tilledEmptyCount": c_tiles.get("tilledEmptyCount", "unknown"),
                    "tillableCount": c_tiles.get("tillableCount", "unknown"),
                    "fresh": True,
                }
            else:
                remaining_seed_stack = "unknown"
                candidate_summary = {
                    "tilledEmptyCount": "unknown",
                    "tillableCount": "unknown",
                    "fresh": False,
                }

            effects = res.get("effects", [])
            planted_tiles = [
                e["tile"]
                for e in effects
                if isinstance(e, dict) and e.get("state") == "planted" and "tile" in e
            ]
            if not planted_tiles and isinstance(res.get("details"), dict):
                planted_tiles = res["details"].get("plantedTiles", [])

            response = {
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "plantedTiles": planted_tiles,
                "remainingSeedStack": remaining_seed_stack,
                "candidateTiles": candidate_summary,
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = effects
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Plant seeds rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Plant seeds failed: {ex}") from None

    @mcp.tool()
    async def ship_items(
        items: Annotated[list[ItemQuantity], BeforeValidator(_unwrap_item_array)], detail: bool = False,
        max_game_minutes: int = 0
    ) -> dict[str, Any]:
        """Ship specified items from companion inventory to the farm shipping bin.

        Estimated value is reported; funds will be settled overnight by the game engine.
        Accepts items list where each item has itemId and count (1..36 items).
        Returns terminalState, completedCount, estimatedValue, shippedItems,
        remainingBackpackCounts, shippingBinTotalCount, fresh, details, error,
        and effects (if detail=True).
        """
        pre_rev_getter = getattr(sched, "latest_world_revision", 0)
        pre_rev = pre_rev_getter() if callable(pre_rev_getter) else int(pre_rev_getter)
        try:
            res = await sched.execute_ship_items(items=_item_dicts(items), max_game_minutes=max_game_minutes)

            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "completedCount": res.get("completedCount", 0),
                    "fresh": False,
                    "message": res.get("message") or f"Shipping task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)

            details = res.get("details") or {}
            shipped_items = details.get("shippedItems", [])
            estimated_total_value = details.get("estimatedTotalValue", 0)
            shipping_bin_total_count = details.get("shippingBinTotalCount", 0)
            remaining_backpack_counts = details.get("remainingBackpackCounts", {})

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "estimatedValue": estimated_total_value,
                "estimatedTotalValue": estimated_total_value,
                "shippedItems": shipped_items,
                "remainingBackpackCounts": remaining_backpack_counts,
                "shippingBinTotalCount": shipping_bin_total_count,
                "fresh": fresh,
                "details": details,
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = res.get("effects", [])
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Ship items rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Ship items failed: {ex}") from None

    @mcp.tool()
    async def purchase_items(
        items: Annotated[list[ItemQuantity], BeforeValidator(_unwrap_item_array)],
        budget_limit: int,
        shop_id: str = "SeedShop",
        detail: bool = False,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Purchase items from a shop counter using native game wallet transactions.

        Requires companion to be inside the target shop location and adjacent to the counter.
        Companion mode buys directly here after navigate_to; do not insert the
        player-only open_shop operation. The adapter rechecks the native shop state.
        In player mode, resolve any native shop-choice question first, then keep the
        matching ShopMenu open while purchasing; another shop or held cursor item blocks it.
        Stock, limited stock status, dynamic unit price, and budget/funds limits are evaluated.
        Accepts items list where each item has itemId and count (1..36 items).
        budget_limit is this command's cost check, filled by the model from the native
        quote and actual funds. Do not ask the player to configure a purchase allowance.
        There is no per-day spending cap in either command or free mode.
        Returns terminalState, completedCount, totalCost, remainingBudget, purchasedItems,
        skippedItems, skipReason, availableMoneyAfter, rollbackPerformed, fresh, details, error,
        and effects (if detail=True).
        """
        pre_rev_getter = getattr(sched, "latest_world_revision", 0)
        pre_rev = pre_rev_getter() if callable(pre_rev_getter) else int(pre_rev_getter)
        command_id = command_id or f"purchase-{uuid.uuid4().hex[:16]}"
        try:
            # All purchase paths share pending-result recovery and actual cost
            # accounting in the scheduler, without a daily spending allowance.
            res = await sched.execute_purchase_items(
                items=_item_dicts(items),
                budget_limit=budget_limit,
                shop_id=shop_id,
                command_id=command_id,
            )

            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "completedCount": res.get("completedCount", 0),
                    "fresh": False,
                    "commandId": command_id,
                    "message": res.get("message") or f"Purchase task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            if res.get("status") in {"rejected", "replayed"}:
                return {
                    "status": res.get("status"),
                    "commandId": res.get("commandId", command_id),
                    "terminalState": res.get("terminalState"),
                    "totalCost": res.get("totalCost", 0),
                    "error": res.get("error"),
                    "message": res.get("message"),
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)

            details = res.get("details") or {}
            purchased_items = details.get("purchasedItems", [])
            skipped_items = details.get("skippedItems", [])
            total_cost = details.get("totalCost")
            remaining_budget = details.get("remainingBudget", 0)
            available_money_after = details.get("availableMoneyAfter")
            skip_reason = details.get("skipReason")
            rollback_performed = details.get("rollbackPerformed", False)

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "commandId": command_id,
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "totalCost": total_cost,
                "remainingBudget": remaining_budget,
                "purchasedItems": purchased_items,
                "skippedItems": skipped_items,
                "skipReason": skip_reason,
                "availableMoneyAfter": available_money_after,
                "rollbackPerformed": rollback_performed,
                "fresh": fresh,
                "details": details,
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = res.get("effects", [])
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Purchase items rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Purchase items failed: {ex}") from None

    @mcp.tool()
    async def query_route(location_id: str, tile: dict[str, int] | None = None) -> dict[str, Any]:
        """Preview a native route without moving. Omit tile only for a public adjacent-map exit on the actor's current map; resolvedDestinationTile is the actual native landing, never guessed. A fresh reachable=true proof clears this exact navigation branch; unrelated targets stay blocked. Estimate excludes deliberation and service time."""
        try:
            result = await sched.query_route(location_id=location_id, tile=tile)
            if result.get("reachable") is True:
                await record_observation_recovery(result, lambda sid:
                    work_for_run().observe_branch_recovery(sid, operation="navigate_to",
                        target={"location_id": location_id, "tile": result.get("resolvedDestinationTile") or tile}, evidence=result))
            return result
        except SchedulerError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def navigate_to(
        location_id: str,
        tile_x: ChestCoordinate | None = None,
        tile_y: ChestCoordinate | None = None,
        x: ChestCoordinate | None = None,
        y: ChestCoordinate | None = None,
        tile: dict[str, ChestCoordinate] | None = None,
        landmark: str | None = None,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Navigate the companion across maps to a destination tile or dynamic interaction point.

        Discovers valid routes dynamically using live map warps and doors, walks continuously
        on each map, and transitions across map boundaries.
        Destination coordinates can be provided via `tile` (dict with 'x', 'y'), or `tile_x`/`tile_y`, or `x`/`y`.
        If tile is omitted, resolves native dynamic interaction points from active game data
        (e.g. shop counter interactionTile when navigating to a shop), or a public outgoing
        warp on the actor's current map for an adjacent exploration destination. Other
        locations require an observed tile or staged travel. No coordinates are guessed.
        Returns the visited location sequence and final position.
        """
        if tile is not None and isinstance(tile, dict):
            resolved_x = tile.get("x")
            resolved_y = tile.get("y")
        elif tile_x is not None or tile_y is not None:
            resolved_x = tile_x
            resolved_y = tile_y
        else:
            resolved_x = x
            resolved_y = y

        target_tile: dict[str, int] | None = None
        if resolved_x is not None and resolved_y is not None:
            if (
                isinstance(resolved_x, bool)
                or isinstance(resolved_y, bool)
                or not isinstance(resolved_x, int)
                or not isinstance(resolved_y, int)
                or resolved_x < 0
                or resolved_y < 0
            ):
                raise ToolError("Coordinates (tile_x/tile_y, x/y, or tile with x, y) must be non-negative integers.")
            target_tile = {"x": resolved_x, "y": resolved_y}
        elif resolved_x is not None or resolved_y is not None:
            raise ToolError("Both x and y coordinates must be specified if one is provided.")

        if not isinstance(location_id, str) or not location_id.strip():
            raise ToolError("location_id must be a non-empty string.")

        try:
            call_kwargs: dict[str, Any] = {
                "location_id": location_id.strip(),
                "tile": target_tile,
            }
            if landmark is not None:
                call_kwargs["landmark"] = landmark
            res = await sched.execute_navigate_to(**call_kwargs)
            term = res.get("terminalState", "unknown")
            is_executing = (res.get("status") == "executing" or term == "running")
            if is_executing:
                nav_status = "executing"
            elif term in ("succeeded", "completed"):
                nav_status = "success"
            else:
                nav_status = "failed"

            details = res.get("details") or {}
            visited = details.get("visitedLocations") or [location_id]
            final_loc = details.get("finalLocation", location_id)
            final_tile = details.get("finalTile", target_tile)
            target_adjusted = bool(details.get("targetAdjusted", False))
            requested_tile = details.get("requestedTile", target_tile)

            response = {
                "terminalState": term,
                "status": nav_status,
                "visitedLocations": visited,
                "finalLocation": final_loc,
                "finalTile": final_tile,
                "targetAdjusted": target_adjusted,
                "requestedTile": requested_tile,
                "details": details,
                "error": res.get("error"),
            }
            if is_executing:
                response["inProgress"] = True
                response["taskId"] = res.get("taskId")
                response["message"] = res.get("message") or f"Navigation task is executing on companion in background (taskId={res.get('taskId')})."
            if detail:
                response["effects"] = res.get("effects", [])
                response["resources"] = res.get("resources")
                response["rawResult"] = res
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Navigate to rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Navigate to failed: {ex}") from None

    @mcp.tool()
    async def plant_crop_workflow(
        seed_item_id: str | None = None,
        crop_name_or_id: str | None = None,
        count: int = 1,
        target_tiles: list[dict[str, int]] | None = None,
        auto_till: bool = True,
        auto_water: bool = True,
        withdraw_from_chest: bool = True,
        chest_tile: dict[str, int] | None = None,
        location_id: str = "Farm",
        detail: bool = False,
    ) -> dict[str, Any]:
        """Retired composite entry: preserve the signature and explicit migration error."""
        raise ToolError(PLANT_WORKFLOW_PLAN_ERROR)

    @mcp.tool()
    async def observe_farming_helpers(location_id: str = "Farm") -> dict[str, Any]:
        """Observe nearby refill tiles, collectible groundItems, standing groundObjects and choppable trees.

        groundItems are existing drops/collectibles for pickup_items. groundObjects
        are weeds, stones, twigs or placed objects; their stack describes the object,
        not a material yield. Use their clearTool when choosing clearance work.
        For map-wide collectible items use observe_production. This nearby scan
        also provides refill_watering_can/chop_tree targets and carried tools.
        """
        try:
            return await sched.query_farming_helpers(location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Failed to observe farming helpers: {ex}") from None

    @mcp.tool()
    async def observe_crafting(location_id: str = "Farm") -> dict[str, Any]:
        """Read unlocked real recipes and available companion crafting materials."""
        try:
            return await sched.query_crafting(location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def craft_items(recipe_name: str, item_count: int = 1, location_id: str = "Farm") -> dict[str, Any]:
        """Craft an observed known recipe 1..64 times, consuming companion materials."""
        try:
            return await sched.craft_items(recipe_name=recipe_name, item_count=item_count, location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def move_building(building_name: str, tile: dict[str, int], location_id: str = "Farm") -> dict[str, Any]:
        """Move one building by observed occupant GUID to explicit origin tile.

        Native occupied-space and entrance checks apply. Single-player only.
        """
        try:
            return await sched.move_building(building_name=building_name, tile=tile, location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def read_reference_image(image_id: str | None = None) -> CallToolResult:
        """View all selected local inspiration images and sources; optional image_id views one again.

        Optional references are not current-world observations or required building content.
        If the client offloads pixels, open the returned image attachments before interpreting them.
        No network, arbitrary filenames or shell access is needed.
        """
        from .reference_images import read_reference_image as read_image
        manifest = Path(run_dir) / "config/reference-images/manifest.json" if run_dir else None
        return read_image(image_id, manifest)

    @mcp.tool()
    async def observe_map_image(location_id: str = "Farm", region: SpatialRegion | None = None) -> CallToolResult:
        """See a native map PNG for design, only when player is already on that map.
        If the client offloads pixels, open the returned image attachment before visual review.

        Omit region for the full map; {x,y,width,height} returns a real local view.
        Image includes absolute tile bounds, pixel mapping and capture revision.
        Use full views for composition, close-ups for objects/occlusion/edges,
        and observe_farm_space for exact tile identities. Does not move the player.
        """
        try:
            result = await sched.query_map_image(location_id=location_id, region=region.model_dump() if isinstance(region, SpatialRegion) else region)
            image_path = Path(str(result.get("path") or "")).resolve()
            allowed_root = (Path(tempfile.gettempdir()) / "StardewAI.Companion" / "map-images").resolve()
            if (not image_path.is_relative_to(allowed_root) or image_path.suffix.lower() != ".png"
                    or not image_path.is_file()):
                raise ToolError("Native map image path is outside the controlled image directory or missing")
            if image_path.stat().st_size > 32 * 1024 * 1024:
                raise ToolError("Native map image exceeds 32 MiB")
            data = image_path.read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ToolError("Native map image is not PNG")
            metadata = {key: value for key, value in result.items() if key != "path"}
            metadata["kind"] = "map_image"
            return CallToolResult(content=[Image(data=data, format="png").to_image_content(),
                TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))], structuredContent=metadata)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def observe_farm_space(location_id: str = "Farm", region: SpatialRegion | None = None) -> dict[str, Any]:
        """Read native map rows, occupant summaries/footprints and warps on demand.

        Omit region for full-map rows and summary counts; occupant details are
        bounded to 128. Pass {x,y,width,height} for target-area details (up to
        512); narrow the region if occupantsTruncated is true. Missing truncated
        details do not mean empty land. Rows are relative to returned offset;
        occupants/warps retain absolute coordinates, mapWidth/mapHeight are full-map
        dimensions. Inspect the row legend before selecting construction tiles.
        """
        try:
            return await sched.query_farm_space(location_id=location_id,
                                               region=region.model_dump() if isinstance(region, SpatialRegion) else region)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def place_items(tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], item_id: str, location_id: str = "Farm") -> dict[str, Any]:
        """Place copies of one real backpack item at the declared tiles.

        Walk to an adjacent standing tile, then apply native placement rules,
        inventory consumption and partial results.
        Upper items may coexist with flooring/grass when native rules permit;
        existing objects, trees, crops and entrances remain protected.
        """
        try:
            return await sched.place_items(tiles=_tile_dicts(tiles), item_id=item_id, location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def remove_items(tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], item_id: str, location_id: str = "Farm") -> dict[str, Any]:
        """Recover matching placed items into backpack; item_id is required identity.

        Use observed identities and explicit tiles. Native mismatches are skipped.
        """
        try:
            return await sched.remove_items(tiles=_tile_dicts(tiles), item_id=item_id, location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def rest(target_stamina: float) -> dict[str, Any]:
        """Rest the companion in a real bed until the chosen stamina target, then wake; the result includes finalLocation and finalTile.

        Choose the target from remaining work; full recovery is optional. Player mode uses native sleep instead.
        """
        try:
            return await sched._execute_native_action("rest", {"locationId": "FarmHouse", "targetStamina": target_stamina},
                                                      tiles=[], timeout_seconds=600)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def observe_production(location_id: str = "Farm") -> dict[str, Any]:
        """Inspect existing drops/collectible produce, refill sources and edible foods.

        Coop eggs are ground items: use their observed tiles with pickup_items;
        animal currentProduce is not evidence of an egg's ground position.
        Weeds, stones, twigs and placed chests are not pickup work; inspect
        farming helpers or farm space when a clearing task needs those objects.
        Choose subsequent food/refill/material tasks from these facts as needed.
        """
        try:
            return await sched.query_production(location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def observe_building_services(location_id: str = "Farm") -> dict[str, Any]:
        """Inspect building/animal service catalogs (ordinary items: query_shop), prices/materials, clear construction site candidates,
        current service gates and today's resolved owner schedule (departure times).

        Observe housing buildingId/residentCount with observe_livestock before buying;
        animals temporarily outside still occupy housing. The model chooses services
        within player authorization and checks whether it can arrive before the
        owner leaves the counter. Sites are observations, not reservations; choose
        another clear site after occupation unless the player fixed the location.
        """
        try:
            return await sched.query_building_services(location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def build_building(building_type: str, tile: dict[str, int], budget_limit: int,
                             location_id: Literal["Farm"] = "Farm") -> dict[str, Any]:
        """Order a native building using observed catalog requirements and real materials/money.

        Navigate to the carpenter's ScienceHouse counter first; location_id and tile
        specify the remote Farm construction site. The game controls construction days.
        This operation places the order directly at the counter; keep native menus
        closed rather than opening CarpenterMenu before calling it.
        """
        try:
            return _native_action_response("build-building", await sched.build_building(
                building_type=building_type, tile=tile, budget_limit=budget_limit, location_id=location_id))
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def upgrade_building(building_name: str, building_type: str, budget_limit: int,
                               location_id: Literal["Farm"] = "Farm") -> dict[str, Any]:
        """At the carpenter counter, order an observed upgrade of an exact building GUID.

        location_id='Farm' is the building's map, not the visited ScienceHouse counter.
        Uses native requirements, construction delay, companion materials and budget.
        """
        try:
            return _native_action_response("upgrade-building", await sched.upgrade_building(
                building_name=building_name, building_type=building_type,
                budget_limit=budget_limit, location_id=location_id))
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def purchase_animal(building_name: str, animal_type: str, animal_name: str, budget_limit: int,
                              location_id: Literal["Farm"] = "Farm") -> dict[str, Any]:
        """At AnimalShop's counter, buy one observed animal for an exact compatible building GUID.

        location_id='Farm' is the destination building's map, not the visited AnimalShop.
        Native stock, housing capacity and companion money determine the purchase. Fill budget_limit from the observed native price;
        no daily purchase allowance is required. Each step purchases one animal.
        Keep the service menu closed; this operation performs the native counter transaction.
        """
        try:
            return _native_action_response("purchase-animal", await sched.purchase_animal(
                building_name=building_name, animal_type=animal_type, animal_name=animal_name,
                budget_limit=budget_limit, location_id=location_id))
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def eat_food(item_id: str, location_id: str = "Farm") -> dict[str, Any]:
        """Consume one observed edible backpack item through the controlled actor's native food lifecycle.

        item_id selects the first matching backpack stack; its exact slot/quality
        and native consumption/recovery are reported. Additional food and
        follow-up operations may be queued in order in the same turn.
        """
        try:
            return _native_action_response("eat-food", await sched.eat_food(item_id=item_id, location_id=location_id))
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def observe_machines(location_id: str = "Farm") -> dict[str, Any]:
        """Observe placed machines only: idle / processing (minutes left) / ready output.

        Returns real native state. `isReady=true` means collect_machine can collect it.
        Observe before creating a machineReady todo for an exact location/tile.
        Processing minutes are informative, not a reason to poll repeatedly.
        """
        try:
            result = await sched.query_machines(location_id=location_id)
            await record_observation_recovery(result, lambda sid:
                work_for_run().observe_machine_recovery(sid, location_id, result.get("machines") or []))
            return result
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Failed to observe machines: {ex}") from None

    @mcp.tool()
    async def observe_livestock(location_id: str = "Farm") -> dict[str, Any]:
        """Observe real livestock on a specified map, including when companion is elsewhere: buildings (hay, capacity, door), animals and produce state.

        Each animal carries its last observed tile plus the native harvest type/tool so
        the model can pick the applicable action instead of guessing.
        Fullness and wasPetToday are separate care facts; a fed animal is not
        necessarily petted. Check both when the player's goal includes care.
        Prefer animalId for pet/produce actions; residentCount includes outdoor
        residents and is the housing occupancy, unlike the current indoor animalCount.
        """
        try:
            return await sched.query_livestock(location_id=location_id)
        except SchedulerError as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Failed to observe livestock: {ex}") from None

    def _latest_livestock_group() -> dict[str, Any]:
        cached = sched.latest_snapshot
        payload = cached.get("payload") if isinstance(cached, dict) else None
        group = payload.get("livestock") if isinstance(payload, dict) else None
        if not isinstance(group, dict):
            raise ToolError(
                "No livestock observation is available yet; call observe_livestock first."
            )
        return group

    def _resolve_animal_tile(animal_name: str, animal_id: str | None = None) -> dict[str, Any]:
        group = _latest_livestock_group()
        candidates: list[dict[str, Any]] = []
        for building in group.get("buildings") or []:
            if isinstance(building, dict):
                candidates.extend(a for a in (building.get("animals") or []) if isinstance(a, dict))
        candidates.extend(a for a in (group.get("roamingAnimals") or []) if isinstance(a, dict))
        wanted = animal_name.strip().lower()
        for animal in candidates:
            if (str(animal.get("animalId")) == animal_id if animal_id else str(animal.get("name", "")).strip().lower() == wanted) and isinstance(animal.get("tile"), dict):
                return {"x": int(animal["tile"]["x"]), "y": int(animal["tile"]["y"])}
        raise ToolError(
            f"animal '{animal_name}' has no observed tile; call observe_livestock first "
            "or pass an explicit tile."
        )

    @mcp.tool()
    async def cut_grass(tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], location_id: str = "Farm") -> dict[str, Any]:
        """Cut observed grass or clear dead crops with the companion's real Scythe.

        Use this for query_planting_options.clearWithScytheTiles. Dead crops are
        removed while their tilled soil stays; living crops are protected.
        Dense grass is cut with visible native swings until removed or the job
        stops; completed tiles are clear, so partial results need re-observation.
        Cutting does not guarantee hay per tile; silo capacity and native randomness
        apply. Replant with real Grass Starter via place_items when appropriate.
        """
        try:
            return _native_action_response("cut-grass", await sched.cut_grass(tiles=_tile_dicts(tiles), location_id=location_id))
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def refill_watering_can(location_id: str = "Farm", max_tiles: int = 4, tiles: Annotated[list[TileXY] | None, BeforeValidator(_unwrap_item_array)] = None) -> dict[str, Any]:
        """Refill the companion's watering can at native refill tiles on its own map.

        Uses the game's own CanRefillWateringCanOnTile judgement; never a hardcoded
        water capacity or cost. Returns an actionable error when no refill tile is known.
        """
        try:
            res = await sched.refill_watering_can(location_id=location_id, max_tiles=max_tiles,
                                                  tiles=_tile_dicts(tiles) if tiles is not None else None)
            return _native_action_response("refill-watering-can", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Refill watering can failed: {ex}") from None

    @mcp.tool()
    async def apply_fertilizer(
        tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)],
        fertilizer_item_id: str,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Apply one explicit fertilizer item from the companion backpack onto explicit tilled tiles.

        The native fertilizer rules (`HoeDirt.CheckApplyFertilizerRules`) decide
        applicability; an already-fertilized or invalid tile is skipped with a reason.
        """
        try:
            res = await sched.apply_fertilizer(
                tiles=_tile_dicts(tiles), fertilizer_item_id=fertilizer_item_id, location_id=location_id
            )
            return _native_action_response("apply-fertilizer", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Apply fertilizer failed: {ex}") from None

    @mcp.tool()
    async def clear_debris(tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], location_id: str = "Farm") -> dict[str, Any]:
        """Clear observed weeds, stones, twigs or boulders; use cut_grass for Grass terrain and chop_tree for trees, stumps or logs. Unreachable targets require clearing a reachable approach or choosing another route first.

        The native tool choice is the game's own rule: weeds with the Hoe/Axe, stones
        with the Pickaxe, twigs with the Axe/Pickaxe. Crops, machines, chests and
        buildings are protected. Grass and dead crops are terrain: use cut_grass
        for those, including query_planting_options.clearWithScytheTiles.
        When the companion genuinely lacks the required tool
        the action returns an actionable ``missing-tool:<Tool>`` precondition, never a
        fake success.
        """
        try:
            res = await sched.clear_debris(tiles=_tile_dicts(tiles), location_id=location_id)
            return _native_action_response("clear-debris", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Clear debris failed: {ex}") from None

    @mcp.tool()
    async def pickup_items(tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], location_id: str = "Farm") -> dict[str, Any]:
        """Pick up dropped debris / spawned items on explicitly selected tiles.

        Uses the native debris collect path and the native ground-item check action.
        completedCount counts handled targets; effects.collectedItems reports accepted
        items with their own itemId, quality and stack, including partial collection.
        """
        try:
            res = await sched.pickup_items(tiles=_tile_dicts(tiles), location_id=location_id)
            return _native_action_response("pickup-items", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Pickup items failed: {ex}") from None

    @mcp.tool()
    async def chop_tree(tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], location_id: str = "Farm") -> dict[str, Any]:
        """Chop explicitly selected wild trees, giant stumps or hollow logs with the companion's own Axe.

        Every swing is a real native Axe.DoFunction (companion stamina, native damage
        and drops). Fruit trees are protected (protected-tree); a tile without a
        choppable target returns no-tree; a missing Axe returns missing-tool:Axe.
        """
        try:
            res = await sched.chop_tree(tiles=_tile_dicts(tiles), location_id=location_id)
            return _native_action_response("chop-tree", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Chop tree failed: {ex}") from None

    @mcp.tool()
    async def insert_machine(
        tile: dict[str, Any],
        item_id: str,
        item_count: int = 1,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Insert an explicit companion item stack into an explicit machine tile.

        Runs the game's own drop-in validation (probe first, then the real action);
        an incompatible item returns machine-rejected-input instead of consuming it.
        """
        try:
            res = await sched.insert_machine(
                tile=tile, item_id=item_id, item_count=item_count, location_id=location_id
            )
            return _native_action_response("insert-machine", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Insert machine failed: {ex}") from None

    @mcp.tool()
    async def collect_machine(tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)], location_id: str = "Farm") -> dict[str, Any]:
        """Collect ready output from explicit machine tiles using the native check action."""
        try:
            res = await sched.collect_machine(tiles=_tile_dicts(tiles), location_id=location_id)
            return _native_action_response("collect-machine", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Collect machine failed: {ex}") from None

    @mcp.tool()
    async def pet_animal(
        animal_name: str = "",
        tile: dict[str, Any] | None = None,
        location_id: str = "Farm",
        animal_id: str | None = None,
    ) -> dict[str, Any]:
        """Pet one named animal via the native FarmAnimal.pet path.

        The tile defaults to the animal's last observed position from observe_livestock.
        Check sleepingBlocksPetting; when true, arrange petting next day.
        That field describes the native sleep gate, not route or proximity eligibility.
        """
        try:
            target = tile or _resolve_animal_tile(animal_name, animal_id)
            res = await sched.pet_animal(animal_name=animal_name, tile=target, location_id=location_id, animal_id=animal_id)
            return _native_action_response("pet-animal", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Pet animal failed: {ex}") from None

    @mcp.tool()
    async def collect_animal_produce(
        animal_name: str = "",
        tile: dict[str, Any] | None = None,
        location_id: str = "Farm",
        animal_id: str | None = None,
    ) -> dict[str, Any]:
        """Collect one animal's produce through its applicable native path.

        Drop-overnight produce is collected with the native ground check action; produce
        that needs a tool (milk pail / shears) is collected with the companion's own
        native tool. Only when the companion genuinely carries no such tool does the
        action return an actionable ``missing-tool:<Tool>`` precondition.
        """
        try:
            target = tile or _resolve_animal_tile(animal_name, animal_id)
            res = await sched.collect_animal_produce(
                animal_name=animal_name, tile=target, location_id=location_id, animal_id=animal_id
            )
            return _native_action_response("collect-animal-produce", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Collect animal produce failed: {ex}") from None

    @mcp.tool()
    async def feed_animals(building_name: str) -> dict[str, Any]:
        """Refill the building's native hay troughs; full troughs are skipped (already-full).

        This places hay for animals to eat naturally; it does not directly change animal fullness
        or guarantee every animal is immediately fed. Decide from observed trough/fullness facts.
        Native overnight updates reset fullness, so a current zero alone does not prove a hay
        shortage; assess actual trough supply together with animal state.
        Native overnight processing resets fullness to zero; a current zero alone is not a hay
        deficit. Check actual trough supply and animal state before choosing this action.

        The companion must be inside that building and the building must have hay;
        otherwise an actionable no-hay/wrong-map reason is returned.
        """
        try:
            res = await sched.feed_animals(building_name=building_name)
            return _native_action_response("feed-animals", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Feed animals failed: {ex}") from None

    @mcp.tool()
    async def toggle_animal_door(
        tiles: Annotated[list[TileXY] | None, BeforeValidator(_unwrap_item_array)] = None,
        building_name: str | None = None,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Open/close an animal building door through the native Building.ToggleAnimalDoor path.

        Pass either explicit door/building tiles or a building_name resolved from
        observe_livestock. The companion must be adjacent to the door.
        """
        try:
            res = await sched.toggle_animal_door(tiles=_tile_dicts(tiles) if tiles is not None else None,
                                                 building_name=building_name, location_id=location_id)
            return _native_action_response("toggle-animal-door", res)
        except ToolError:
            raise
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Toggle animal door failed: {ex}") from None

    @mcp.tool()
    async def wait(game_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)]) -> dict[str, Any]:
        """Stand still and let native game time pass for the requested minutes (10-minute clock ticks). Use when choosing to wait for opening hours or an event; with thinking-pause enabled, repeated observations do not advance time. Does not fast-forward, sleep, heal or queue later work. Returns elapsed time; cancellation, native UI or a day change returns control for a fresh decision."""
        try:
            return _native_action_response("wait", await sched.execute_wait(game_minutes))
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def sleep() -> dict[str, Any]:
        """End the current day natively: walk to the bed, sleep, and settle the new day.

        Player mode only. The mod runs the native bed-sleep sequence (fade,
        end-of-night menus, new day) and the terminal result reports the new
        day facts. It resolves and walks the actual home/bed route from the
        current map; do not invent an interior coordinate from exterior door
        coordinates before this tool. An optional preceding navigation must
        use its destination map's observed entry/tile. In companion mode the
        native side rejects the command; do
        not plan sleep around the companion's own rest controller.
        """
        try:
            res = await sched.execute_sleep()
            return _native_action_response("sleep", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Sleep failed: {ex}") from ex
