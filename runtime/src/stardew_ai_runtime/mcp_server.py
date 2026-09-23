"""Model Context Protocol (MCP) Server for Stardew AI Companion (Farming V1).

Exposes streamlined farming tools (work overview, status, queries, water, harvest,
deposit, organize, task control) to MCP clients via stdio transport.
All tool requests pass through the thin policy/scheduler layer, strictly
enforcing single-task execution, input validation, and loopback security.
"""

from __future__ import annotations

import time
import functools
from stardew_ai_runtime.job_feedback import compact_job_feedback
import argparse
import asyncio
import inspect
import logging
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.plan_executor import (
    PlanExecutor,
)
from stardew_ai_runtime.plan_executor import (
    classify_step_outcome as _classify_step_outcome,
)
from stardew_ai_runtime.scheduler import (
    CompanionScheduler,
    NoActiveTaskError,
    PolicyViolationError,
    SchedulerError,
    extract_chest_summary,
    extract_non_tool_items,
)
from stardew_ai_runtime.wiki import WikiLookup
from stardew_ai_runtime.work_state import WorkStateError, WorkStore

# Ensure UTF-8 I/O for Windows consoles
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger("stardew_ai_runtime.mcp_server")


async def _safe_wait_for_fresh_snapshot(
    sched: Any, pre_rev: int, timeout: float = 1.0
) -> tuple[dict[str, Any] | None, bool]:
    """Safely invokes wait_for_fresh_snapshot on scheduler if available and awaitable."""
    wait_fn = getattr(sched, "wait_for_fresh_snapshot", None)
    if not callable(wait_fn):
        return None, False
    try:
        call_res = wait_fn(pre_rev, timeout=timeout)
        if inspect.isawaitable(call_res):
            return await call_res
        return None, False
    except Exception:
        return None, False


DEFAULT_MCP_INSTRUCTIONS = """You are the AI Companion for Stardew Valley.
You assist the farmer directly in the game world using available MCP tools.

Guidelines:
1. Real-time context is injected:
   - Every request already carries a compact context block (date/weather/location/
     stamina/funds, companion backpack item names/counts/free slots and tool
     resources, saved goals/agreements, the current task's next step, waiting
     conditions and anomalies). Treat it as authoritative and do not re-query it;
     fields the game did not publish are shown as "unknown" rather than guessed.
   - Tool results are post-execution facts. Do not call a query tool again only to
     confirm what a tool already returned.

2. Common actions are exposed directly:
   - `plant_crop_workflow(crop_name_or_id="Carrot", count=3, auto_till=True, auto_water=True)`
     finds seeds in the backpack or farm chests, tills, plants and waters.
   - `water_auto(max_tiles=25)` waters unwatered crop tiles; `harvest_auto(max_tiles=16)`
     harvests mature crops; `navigate_to(location_id, tile=...)` moves the companion.
   - Uncommon operations (shopping, chests, shipping, wiki) are disclosed by group:
     call `discover_capabilities(group)` for a short schema
     (farm/shopping/chest/movement/knowledge), then `call_capability(tool, params)`
     to run the exact same real implementation.

3. Work memory (two write entry points):
   - `submit_plan(tasks=[...], goal_id=... | goal_text=...)` submits or revises a
     short plan of deterministic steps for a goal.
   - `remember_intent(intent=..., kind="goal"|"todo", ...)` records a long-term
     goal or a future cross-day todo. Model-created goals are always source=agent;
     never claim the player authorised something they did not.
   - The runtime worker advances committed plans and records real outcomes itself.
     Do not try to mark an unexecuted step successful; there is no tool for it.
     Wake only for meaningful deviations, new choices or new player instructions.

4. World Boundaries:
   - All interactions happen purely inside the game world through these MCP tools.
   - Do NOT attempt to read local files, browse external directories, inspect processes, or use shell commands.
"""


def _accepts_command_id(method: Any) -> bool:
    """True when a scheduler method can carry the stable plan command id."""
    try:
        return "command_id" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


def _is_satisfied_skip(reason: str | None) -> bool:
    if not reason:
        return False
    r = reason.lower().strip()
    return (
        r.startswith("already-")
        or r.startswith("already_")
        or r in ("no-work", "no_work", "not-ready", "no-produce")
    )


def _native_action_response(skill: str, res: dict[str, Any]) -> dict[str, Any]:
    """Maps a real scheduler result onto the shared effects/completed/remaining contract.

    A running or rejected task is never reported as success; ``partial`` and
    ``unknown`` stay distinct from ``completed``.
    """
    status = res.get("status")
    terminal = res.get("terminalState")
    error = res.get("error") if isinstance(res.get("error"), dict) else {}
    details = res.get("details") if isinstance(res.get("details"), dict) else {}
    effects = res.get("effects") if isinstance(res.get("effects"), list) else []

    skipped_effects = [e for e in effects if isinstance(e, dict) and e.get("state") == "skipped"]
    unfulfilled_skips = [e for e in skipped_effects if not _is_satisfied_skip(e.get("reason"))]
    first_unfulfilled_reason = unfulfilled_skips[0].get("reason") if unfulfilled_skips else None

    completed_count = int(res.get("completedCount") or 0)
    if unfulfilled_skips and terminal in ("succeeded", "none", None):
        terminal = "partially-succeeded" if completed_count > 0 else "rejected"

    if status == "executing" or terminal == "running":
        outcome, reason = "unknown", "IN_PROGRESS"
    elif terminal == "succeeded":
        outcome, reason = "completed", "OK"
    elif terminal == "partially-succeeded":
        outcome = "partial"
        reason = str(error.get("code") or details.get("reasonCode") or first_unfulfilled_reason or "PARTIAL")
    elif terminal == "rejected":
        outcome = "rejected"
        reason = str(error.get("code") or details.get("reasonCode") or first_unfulfilled_reason or "REJECTED")
    elif terminal == "cancelled":
        outcome, reason = "cancelled", "CANCELLED"
    elif status == "no-work" or terminal == "none":
        outcome, reason = "completed", "NO_WORK"
    else:
        outcome, reason = "failed", str(error.get("code") or "FAILED")

    return {
        "skill": skill,
        "outcome": outcome,
        "goalSatisfied": outcome == "completed",
        "terminalState": terminal,
        "status": status,
        "taskId": res.get("taskId"),
        "effects": effects,
        "completedCount": completed_count,
        "skippedCount": res.get("skippedCount", 0),
        "failedCount": res.get("failedCount", 0),
        "remaining": details.get("skipped") if outcome == "partial" else None,
        "reasonCode": reason,
        "reasonMessage": error.get("message"),
        "playerActionRequired": bool(res.get("playerActionRequired")),
        "snapshotRevision": res.get("worldRevision"),
    }


# Plan steps may only invoke these existing operations, through their real
# scheduler implementation. Unknown operations are rejected; unknown parameters
# are stripped with a warning before dispatch (persisted steps may carry stale keys).
_PLAN_OPERATION_CALLS: dict[str, tuple[str, frozenset[str]]] = {
    "get_work_overview": ("get_work_overview", frozenset({"detail"})),
    "get_status": ("get_status", frozenset({"detail"})),
    "query_farm_work": ("query_farm_work", frozenset()),
    "query_inventory": ("query_inventory", frozenset()),
    "query_chests": ("query_chests", frozenset()),
    "query_planting_options": ("query_planting_options", frozenset({"detail"})),
    "query_shop": ("query_shop", frozenset({"shop_id", "detail", "item_id", "name", "is_seed"})),
    "water_zone": ("execute_water_zone", frozenset({"center_x", "center_y", "radius", "include_empty_tiles"})),
    "water_auto": ("water_auto", frozenset({"max_tiles", "include_empty_tiles"})),
    "harvest_auto": ("harvest_auto", frozenset({"max_tiles"})),
    "deposit_to_chest": ("deposit_to_chest", frozenset({"chest_x", "chest_y", "item_ids"})),
    "withdraw_from_chest": (
        "withdraw_from_chest",
        frozenset({"chest_x", "chest_y", "items", "item_id", "count", "location_id"}),
    ),
    "organize_chest": ("organize_chest", frozenset({"chest_x", "chest_y"})),
    "hoe_tiles": ("execute_hoe_tiles", frozenset({"tiles", "location_id"})),
    "plant_seeds": ("execute_plant_seeds", frozenset({"seed_item_id", "tiles", "location_id"})),
    "ship_items": ("execute_ship_items", frozenset({"items", "location_id"})),
    "purchase_items": (
        "execute_purchase_items",
        frozenset({"items", "budget_limit", "shop_id", "location_id"}),
    ),
    "navigate_to": ("execute_navigate_to", frozenset({"location_id", "tile", "landmark"})),
    "plant_crop_workflow": (
        "plant_crop_workflow",
        frozenset(
            {
                "crop_name_or_id",
                "count",
                "target_tiles",
                "auto_till",
                "auto_water",
                "withdraw_from_chest",
                "chest_tile",
                "location_id",
                "water",
            }
        ),
    ),
    "pause_task": ("pause_task", frozenset()),
    "resume_task": ("resume_task", frozenset()),
    "cancel_task": ("cancel_task", frozenset({"reason"})),
    # Grouped observation + native agricultural/husbandry actions. Every entry runs
    # the same real scheduler method as the corresponding MCP tool.
    "observe_farming_helpers": ("query_farming_helpers", frozenset({"location_id"})),
    "observe_machines": ("query_machines", frozenset({"location_id"})),
    "observe_livestock": ("query_livestock", frozenset()),
    "refill_watering_can": ("refill_watering_can", frozenset({"tiles", "location_id", "max_tiles"})),
    "apply_fertilizer": (
        "apply_fertilizer",
        frozenset({"tiles", "fertilizer_item_id", "location_id"}),
    ),
    "clear_debris": ("clear_debris", frozenset({"tiles", "location_id"})),
    "pickup_items": ("pickup_items", frozenset({"tiles", "location_id"})),
    "chop_tree": ("chop_tree", frozenset({"tiles", "location_id"})),
    "insert_machine": ("insert_machine", frozenset({"tile", "item_id", "item_count", "location_id"})),
    "collect_machine": ("collect_machine", frozenset({"tiles", "location_id"})),
    "pet_animal": ("pet_animal", frozenset({"animal_name", "tile", "location_id"})),
    "collect_animal_produce": (
        "collect_animal_produce",
        frozenset({"animal_name", "tile", "location_id"}),
    ),
    "feed_animals": ("feed_animals", frozenset({"building_name"})),
    "toggle_animal_door": (
        "toggle_animal_door",
        frozenset({"tiles", "building_name", "location_id"}),
    ),
}


# Lightweight game surface. Base tools stay reachable through
# discover_capabilities/call_capability.
#
# Selection is explicit so the generic MCP entry stays fully compatible:
#   * ``--surface full`` (default, and ``STARDEW_MCP_SURFACE=full``) lists every
#     tool exactly as before this change, including the legacy ``manage_*`` and
#     ``run_next_step`` harness tools;
#   * ``--surface light`` (or ``--light`` / ``STARDEW_MCP_SURFACE=light``) lists
#     only this set: the common actions the model should call directly, the two
#     write entry points, player controls and group discovery. It is what the game
#     provider profile (.kimi-code/mcp.json) requests.
#   * ``--surface internal`` is the ChatBridge plan worker's surface: light plus
#     the read-only reconcile tool, never exposed to the model.
# The light set is not a fixed tool count; it is this named set.
BASE_TOOLS = frozenset(
    {
        # overview / status
        "get_work_overview",
        "get_status",
        # grouped observation (on-demand: never mixes backpack/chests into a farm tool)
        "observe_farming_helpers",
        "observe_machines",
        "observe_livestock",
        # common actions exposed directly
        "plant_crop_workflow",
        "water_auto",
        "harvest_auto",
        "navigate_to",
        "refill_watering_can",
        # write entry points
        "submit_plan",
        "remember_intent",
        # player controls
        "autonomy_status",
        "set_autonomy",
        "pause_task",
        "resume_task",
        "cancel_task",
        # grouped discovery + invocation
        "discover_capabilities",
        "call_capability",
    }
)
LIGHT_TOOLS = BASE_TOOLS

# Harness-only additions on top of the game surface (never shown to the model).
# ``dispatch_plan_operation`` is the worker's write path: it runs one validated
# plan operation through ``execute_plan_operation`` so the persisted stable
# command id actually reaches the real scheduler/native command (``call_capability``
# rejects the harness-only ``command_id`` parameter, which is exactly the round6
# STEP_DISPATCH_FAILED root cause).
INTERNAL_TOOLS = LIGHT_TOOLS | {"reconcile_plan_command", "dispatch_plan_operation"}

# Base tools grouped for on-demand disclosure.
CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "farm": (
        "query_farm_work",
        "query_planting_options",
        "water_zone",
        "water_auto",
        "harvest_auto",
        "hoe_tiles",
        "plant_seeds",
    ),
    "farming_helpers": (
        "observe_farming_helpers",
        "refill_watering_can",
        "apply_fertilizer",
        "clear_debris",
        "pickup_items",
    ),
    "forestry": ("chop_tree",),
    "machines": ("observe_machines", "insert_machine", "collect_machine"),
    "livestock": (
        "observe_livestock",
        "pet_animal",
        "collect_animal_produce",
        "feed_animals",
        "toggle_animal_door",
    ),
    "shopping": ("query_shop", "purchase_items", "ship_items"),
    "chest": (
        "query_inventory",
        "query_chests",
        "deposit_to_chest",
        "withdraw_from_chest",
        "organize_chest",
    ),
    "movement": ("get_status", "navigate_to"),
    "knowledge": ("query_wiki",),
    "memory": ("manage_goal", "manage_plan", "manage_todo", "work_plan_overview"),
}

# The real nested schema for the two model-facing write entry points. The model
# used to guess trigger kinds ("daily"/"date"/"next_day"); this is the authoritative
# shape, published on demand through discover_capabilities(group="memory").
MEMORY_WRITE_SCHEMA: dict[str, Any] = {
    "remember_intent": {
        "purpose": "Record a long-term goal (kind='goal') or a future cross-day todo (kind='todo').",
        "parameters": {
            "intent": "string, required, non-empty. What to remember.",
            "kind": "'goal' | 'todo' (default 'goal').",
            "goal_id": "string | null. Optional parent goal for a todo.",
            "priority": "integer (default 0). Only for kind='goal'.",
            "trigger": "object, required for kind='todo'. See trigger shapes below.",
            "expiry": "object | null. {'year':int,'season':str,'day':int} latest day the todo stays valid.",
        },
        "trigger_shapes": {
            "calendar": {
                "type": "calendar",
                "year": "integer, required",
                "season": "spring|summer|fall|winter, required",
                "day": "integer 1..28, required",
                "example": {"type": "calendar", "year": 2, "season": "spring", "day": 3},
            },
            "inventory": {
                "type": "inventory",
                "itemId": "string, required (qualified id, e.g. (O)368)",
                "minCount": "integer >= 1, required",
                "example": {"type": "inventory", "itemId": "(O)368", "minCount": 5},
            },
            "crop": {
                "type": "crop",
                "cropId": "string, required",
                "state": "mature|harvestable|unwatered (default mature)",
                "example": {"type": "crop", "cropId": "(O)24", "state": "harvestable"},
            },
        },
        "invalid_examples": [
            {"type": "daily"},
            {"type": "date", "date": "spring 3"},
            {"type": "next_day"},
        ],
        "valid_examples": [
            {"intent": "攒钱买鸡", "kind": "goal", "priority": 1},
            {
                "intent": "春季第3天给胡萝卜地施肥",
                "kind": "todo",
                "trigger": {"type": "calendar", "year": 2, "season": "spring", "day": 3},
            },
        ],
    },
    "submit_plan": {
        "purpose": "Submit or revise a short deterministic plan of allowed operations.",
        "parameters": {
            "tasks": "array, required, >=1 task. Each: {id, title, completionCondition?, dependencies?, steps[]}.",
            "goal_id": "string | null. Existing active goal id.",
            "goal_text": "string | null. Used when goal_id is absent (creates an agent goal).",
            "replace": "boolean (default false). Supersedes the goal's still-pending tasks only.",
        },
        "step_shape": {
            "operation": "one of the discoverable plan operations",
            "params": "object with that operation's exact keys (see discover_capabilities)",
        },
        "valid_example": {
            "goal_text": "把胡萝卜种好",
            "tasks": [
                {
                    "id": "t1",
                    "title": "浇水",
                    "steps": [{"operation": "water_auto", "params": {"max_tiles": 10}}],
                }
            ],
        },
        "invalid_example": {
            "tasks": [{"id": "t1", "steps": [{"operation": "run_shell", "params": {}}]}]
        },
        "errors": "An unknown operation is rejected with the allowed list; an unknown step parameter is stripped with a warning before dispatch.",
    },
}


def resolve_surface(full: bool | None = None, surface: str | None = None) -> str:
    """Resolve the exposure surface: "full" (default), "light" or "internal".

    Generic MCP clients keep the complete legacy tool list by default so no
    existing tool name disappears on upgrade. Only callers that explicitly ask
    for the game surface (CLI flag or env var) get the trimmed list; the internal
    plan worker additionally selects the harness-only surface.
    """
    if full is True:
        return "full"
    if full is False:
        # Legacy explicit "full=False" callers asked for the game surface.
        return "light"
    candidate = (surface or os.getenv("STARDEW_MCP_SURFACE") or "").strip().lower()
    if candidate in {"light", "game"}:
        return "light"
    if candidate in {"internal", "worker"}:
        return "internal"
    if candidate in {"full", "legacy", "complete"}:
        return "full"
    legacy = os.getenv("STARDEW_MCP_FULL", "").strip().lower()
    if legacy in {"1", "true", "yes", "on"}:
        return "full"
    if legacy in {"0", "false", "no", "off"}:
        # Explicit legacy opt-out of the full list means the game surface.
        return "light"
    return "full"


def create_mcp_server(
    run_dir: Path | str | None = None,
    scheduler: CompanionScheduler | None = None,
    server_name: str = "stardew-companion",
    instructions: str | None = None,
    full: bool | None = None,
    surface: str | None = None,
) -> FastMCP:
    """Creates and configures a FastMCP server backed by CompanionScheduler.

    By default the complete legacy tool list is exposed (generic compatibility).
    Pass ``surface="light"`` (or ``--surface light`` / ``STARDEW_MCP_SURFACE=light``)
    for the game surface, where the common actions plus the two write entry points
    are listed directly and every other base operation stays reachable through
    ``discover_capabilities``/``call_capability`` (same real implementation).
    ``surface="internal"`` adds the harness-only reconcile tool for the plan worker.
    ``full=True``/``STARDEW_MCP_FULL=1`` forces the full list.
    """
    resolved_surface = resolve_surface(full, surface)
    inst = instructions if instructions is not None else "每次模型决策仅选择一个语义短作业。submit_plan只接受一个task，可包含同一业务范围内的导航及多步原生操作。不同业务必须下一次模型选择；remember_intent及旧计划仅记录意图。工具返回job-selected只代表选择，效果未执行；运行时完成后返回真实精简终态。信任作业结果，不重复逐格核查。自由模式自动再次调用模型，不等待玩家逐步审批。"
    mcp = FastMCP(server_name, instructions=inst)
    sched = scheduler or CompanionScheduler(run_dir=run_dir)
    # Populated at the end from the real registered tools; shared with the
    # discovery/call closures so both surfaces expose every base op.
    base_tools: dict[str, Any] = {}
    base_schemas: dict[str, dict[str, Any]] = {}

    def autonomy_for_run() -> AutonomyController:
        actual_run_dir = getattr(sched, "run_dir", None) or run_dir
        return AutonomyController(Path(actual_run_dir or ".") / "data" / "autonomy-state.json")

    def wiki_for_run() -> WikiLookup:
        actual_run_dir = getattr(sched, "run_dir", None) or run_dir
        return WikiLookup(Path(actual_run_dir or ".") / "data" / "wiki-cache.json")

    def work_for_run() -> WorkStore:
        actual_run_dir = getattr(sched, "run_dir", None) or run_dir
        return WorkStore(Path(actual_run_dir or ".") / "data" / "work-state.json")

    async def execute_plan_operation(
        operation: str,
        params: dict[str, Any],
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one validated plan step through the real scheduler/wiki implementation.

        ``command_id`` is the stable plan command id persisted before dispatch. It
        is forwarded to the real scheduler method (and from there to the native
        Mod command) so the persisted id is the idempotency identity, not a label.
        Composite operations derive deterministic sub-command ids from it.
        """
        entry = _PLAN_OPERATION_CALLS.get(operation)
        allowed = entry[1] if entry else frozenset()

        def _params_match(step_p: dict[str, Any], call_p: dict[str, Any]) -> bool:
            if step_p == call_p:
                return True
            if allowed:
                return {k: v for k, v in (step_p or {}).items() if k in allowed} == {k: v for k, v in (call_p or {}).items() if k in allowed}
            return False

        if operation not in {"cancel_task", "pause_task"}:
            sid = await current_save_id()
            state = work_for_run().state(sid)
            d = state.decision
            task = next((t for t in state.tasks if t.id == d.get("taskId")), None)
            if not task or task.status == "cancelled" or state.paused or d.get("expires", 0) <= time.time() or d.get("finished") or not any(
                step.command_id == command_id and step.operation == operation and _params_match(step.params, params) and step.status == "running"
                for step in task.steps
            ):
                raise ToolError("UNAUTHORIZED_JOB_COMMAND: requires claimed step of this model-selected job")
        if operation == "query_wiki":
            query = str(params.get("query") or "")
            return await asyncio.to_thread(wiki_for_run().lookup, query)
        if entry is None:
            raise PolicyViolationError(f"operation '{operation}' is not an allowed plan operation")
        method_name, allowed = entry
        unknown = set(params) - set(allowed)
        if unknown:
            logger.warning(
                "Stripping unknown parameter(s) %s for operation '%s' before execution",
                sorted(unknown),
                operation,
            )
            params = {k: v for k, v in params.items() if k in allowed}
        method = getattr(sched, method_name, None)
        if method is None:
            raise SchedulerError(f"scheduler does not implement '{operation}'")
        call_params = dict(params)
        if command_id and _accepts_command_id(method):
            call_params["command_id"] = command_id
        return await method(**call_params)

    async def reconcile_native_command(command_id: str) -> dict[str, Any] | None:
        """Reconcile a persisted command id against the Mod's cached result.

        Read-only: never dispatches. Returns None when the native side has no
        terminal result for this id (which keeps the step ``unknown`` instead of
        pretending it succeeded or re-sending it).
        """
        if not command_id:
            return None
        try:
            return await sched.reconcile_command(command_id, timeout=0.2)
        except Exception:
            logger.debug("Reconcile failed for %s", command_id, exc_info=True)
            return None

    def classify_step_outcome(result: Any) -> tuple[str, str | None]:
        return _classify_step_outcome(result)

    async def current_save_id() -> str:
        current = await sched.get_status()
        sid = current.get("saveId")
        if not sid or sid == "unknown":
            raise SchedulerError("The connected Mod did not publish a current saveId.")
        return str(sid)

    async def disable_autonomy_after_task_control() -> None:
        """Make pause/cancel stop future autonomous submissions for this save.

        The existing MCP scheduler remains the owner of the execution socket;
        ChatBridge observes this shared state and cancels its active autonomous
        chat task, which closes the agent's MCP connection and reaches Mod's
        existing transport-disconnect cancellation path.
        """
        try:
            sid = await current_save_id()
            autonomy_for_run().set_enabled(sid, False)
        except Exception:
            logger.debug("Could not update autonomy state after task control", exc_info=True)

    @mcp.tool()
    async def get_work_overview(detail: bool = False) -> dict[str, Any]:
        """Get consolidated farm work, companion backpack, and candidate chests overview.

        Returns crop/soil counts, backpack space/items, and candidate chests with merge status.
        """
        try:
            return await sched.get_work_overview(detail=detail)
        except SchedulerError as ex:
            raise ToolError(f"Failed to get work overview: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error getting work overview: {ex}") from None

    @mcp.tool()
    async def get_status(detail: bool = False) -> dict[str, Any]:
        """Get companion status (position, stamina, water, task) and world time/weather.

        ``detail=True`` keeps the real native sections the compact decision context
        reads (weather icon, companion wallet, backpack inventory, chests, planting,
        shop) instead of dropping them in the condensed view. Never invents a
        missing field: anything the Mod did not publish stays absent/unknown.
        """
        try:
            res = await sched.get_status()
            if detail:
                enriched = dict(res)
                # ``latest_snapshot`` is the raw world.snapshot payload the Mod
                # pushed; it never triggers a new query.
                cached = getattr(sched, "latest_snapshot", None)
                payload = cached.get("payload") if isinstance(cached, dict) else None
                raw = payload if isinstance(payload, dict) else {}
                for section in ("inventory", "chests", "planting", "shop"):
                    value = raw.get(section)
                    if isinstance(value, dict):
                        enriched[section] = value
                return enriched
            comp = res.get("companion", {})
            world = res.get("world", {})
            return {
                "companion": {
                    "tileX": comp.get("tileX", 0),
                    "tileY": comp.get("tileY", 0),
                    "activity": comp.get("activity", "idle"),
                    "stamina": comp.get("stamina", 0.0),
                    "waterCanLevel": comp.get("waterCanLevel", 0),
                    "currentTask": comp.get("currentTask"),
                    # Real thin-observer wallet; absent stays absent/unknown.
                    "availableMoney": comp.get("availableMoney"),
                    "moneyStatus": comp.get("moneyStatus"),
                },
                "world": {
                    "timeOfDay": world.get("timeOfDay", 600),
                    "isRaining": world.get("isRaining", False),
                    # Real native weather icon (isRaining=false is not "clear").
                    "weatherIcon": world.get("weatherIcon"),
                },
                "saveId": res.get("saveId", "unknown"),
                "worldRevision": res.get("worldRevision", 0),
            }
        except SchedulerError as ex:
            raise ToolError(f"Failed to get status: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error getting status: {ex}") from None

    @mcp.tool()
    async def query_farm_work(detail: bool = False) -> dict[str, Any]:
        """Query unwatered soil and mature crop counts from snapshot. Full lists if detail=True."""
        try:
            res = await sched.query_farm_work()
            if detail:
                return res
            fw = res.get("farmWork", {})
            return {
                "farmWork": {
                    "matureCropCount": fw.get("matureCropCount", 0),
                    "tilledUnwateredCount": fw.get("tilledUnwateredCount", 0),
                    "cropUnwateredCount": fw.get("cropUnwateredCount"),
                    "isTruncated": fw.get("isTruncated", False),
                },
                "worldRevision": res.get("worldRevision", 0),
            }
        except SchedulerError as ex:
            raise ToolError(f"Failed to query farm work: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error querying farm work: {ex}") from None

    @mcp.tool()
    async def autonomy_status(save_id: str | None = None) -> dict[str, Any]:
        """Read player-controlled autonomy state for one save; disabled by default."""
        sid = await current_save_id()
        if save_id is not None and save_id != sid:
            raise ToolError("save_id must match the currently connected save")
        state_dict = dict(autonomy_for_run().state(sid).__dict__)
        state_dict.update({
            "failureCount": state_dict.get("failure_count", 0),
            "breakerTripped": state_dict.get("breaker_tripped", False),
            "breakerCooldownUntil": state_dict.get("breaker_cooldown_until"),
            "breakerReason": state_dict.get("breaker_reason"),
        })
        return {"saveId": sid, **state_dict}

    @mcp.tool()
    async def set_autonomy(
        enabled: bool,
        save_id: str | None = None,
        goal: str | None = None,
        budget_limit: int | None = None,
        box_preference: str | None = None,
    ) -> dict[str, Any]:
        """Enable or disable lightweight autonomy and persist its short preferences per save."""
        sid = await current_save_id()
        if save_id is not None and save_id != sid:
            raise ToolError("save_id must match the currently connected save")
        try:
            autonomy = autonomy_for_run()
            autonomy.set_preferences(sid, goal=goal, budget_limit=budget_limit, box_preference=box_preference)
            state = autonomy.set_enabled(sid, enabled)
            state_dict = dict(state.__dict__)
            state_dict.update({
                "failureCount": state_dict.get("failure_count", 0),
                "breakerTripped": state_dict.get("breaker_tripped", False),
                "breakerCooldownUntil": state_dict.get("breaker_cooldown_until"),
                "breakerReason": state_dict.get("breaker_reason"),
            })
            return {"saveId": sid, **state_dict}
        except ValueError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def work_plan_overview() -> dict[str, Any]:
        """Relevant-only work memory: active goals, current short tasks, next step, waiting todos, anomalies.

        Use this once instead of repeatedly listing tasks and re-querying status. It
        reads only the latest cached snapshot; it never issues a new farm query.
        """
        sid = await current_save_id()
        store = work_for_run()
        decisions = store.recover(sid)
        snapshot = sched.latest_snapshot
        payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
        world = payload.get("world", {}) if isinstance(payload, dict) else {}
        game_date = {
            "year": world.get("year"),
            "season": world.get("season"),
            "day": world.get("dayOfMonth"),
        }
        overview = store.overview(sid, snapshot=payload if payload else None, game_date=game_date)
        overview["dueTodos"] = store.evaluate_todos(sid, snapshot=snapshot, game_date=game_date)
        overview["recoveryDecisions"] = decisions
        return overview

    @mcp.tool()
    async def manage_goal(
        action: str,
        goal_id: str | None = None,
        text: str | None = None,
        priority: int | None = None,
        constraints: dict[str, Any] | None = None,
        source: str = "agent",
    ) -> dict[str, Any]:
        """Create/revise/pause/resume/cancel/list long-term goals for the current save.

        Model-created goals are recorded with source=agent; the model cannot label a
        goal as a user instruction. Player-authored goals come from the chat path.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            if action == "list":
                return {"saveId": sid, "goals": store.list_goals(sid)}
            if action == "create":
                if source != "agent":
                    raise WorkStateError("model-created goals must use source='agent'")
                goal = store.add_goal(
                    sid, text or "", source="agent", priority=priority or 0, constraints=constraints
                )
                return {"saveId": sid, "goal": asdict(goal)}
            if action == "revise":
                goal = store.revise_goal(
                    sid, goal_id or "", text=text, priority=priority, constraints=constraints
                )
                return {"saveId": sid, "goal": asdict(goal)}
            if action in {"pause", "resume"}:
                goal = store.revise_goal(
                    sid, goal_id or "", status="paused" if action == "pause" else "active"
                )
                return {"saveId": sid, "goal": asdict(goal)}
            if action == "cancel":
                goal = store.cancel_goal(sid, goal_id or "")
                return {"saveId": sid, "goal": asdict(goal)}
            raise WorkStateError(f"unsupported goal action '{action}'")
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def manage_plan(
        goal_id: str,
        tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a structured short plan for a goal, or list its tasks.

        Each task has id/title/completionCondition/dependencies and steps; every step
        is an operation name plus structured params, validated against the allowed
        scheduler operations. Dependency cycles are rejected.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            if tasks:
                created = store.create_plan(sid, goal_id, tasks)
                return {"saveId": sid, "tasks": [asdict(t) for t in created]}
            return {"saveId": sid, "tasks": store.list_tasks(sid, goal_id)}
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def manage_todo(
        action: str,
        todo_id: str | None = None,
        intent: str | None = None,
        trigger: dict[str, Any] | None = None,
        goal_id: str | None = None,
        expiry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create/list/complete/cancel cross-day todos, or evaluate which are due now.

        Triggers are calendar (year/season/day), inventory (itemId/minCount) or crop
        conditions. Due evaluation uses only the latest native snapshot and date; the
        stored goal memory is never treated as the latest inventory fact.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            if action == "list":
                return {"saveId": sid, "todos": store.list_todos(sid)}
            if action == "create":
                todo = store.add_todo(
                    sid,
                    intent=intent or "",
                    trigger=trigger or {},
                    goal_id=goal_id,
                    expiry=expiry,
                )
                return {"saveId": sid, "todo": asdict(todo)}
            if action in {"complete", "cancel"}:
                todo = store.complete_todo(
                    sid, todo_id or "", "done" if action == "complete" else "cancelled"
                )
                return {"saveId": sid, "todo": asdict(todo)}
            if action == "due":
                snapshot = sched.latest_snapshot
                payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
                world = payload.get("world", {}) if isinstance(payload, dict) else {}
                game_date = {
                    "year": world.get("year"),
                    "season": world.get("season"),
                    "day": world.get("dayOfMonth"),
                }
                return {
                    "saveId": sid,
                    "due": store.evaluate_todos(sid, snapshot=snapshot, game_date=game_date),
                }
            raise WorkStateError(f"unsupported todo action '{action}'")
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def submit_plan(
        tasks: list[dict[str, Any]],
        goal_id: str | None = None,
        goal_text: str | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Select ONE semantic short job for the CURRENT provider decision.

        One task may combine navigation and bounded steps of one business kind.
        A second job in this decision is rejected. Future plans are memory only.

        Prefer this over the legacy ``manage_plan`` tool. Exact shape::

            tasks = [
              {"id": "t1", "title": "浇水", "completionCondition": "无未浇水作物",
               "dependencies": [], "steps": [
                 {"operation": "water_auto", "params": {"max_tiles": 10}}]}
            ]

        Every ``operation`` must be one of the discoverable plan operations; an
        unknown operation is rejected with the allowed list. ``params`` should
        contain only that operation's documented keys — unknown keys are stripped
        with a warning before dispatch rather than rejecting the step. Pass an existing
        active ``goal_id`` or a ``goal_text`` to resolve/create an agent goal. With
        ``replace=True`` the goal's still-pending tasks are superseded; work already
        running, partial or unknown is preserved. Call
        discover_capabilities("memory") for the same schema.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            result = store.submit_plan(
                sid,
                tasks=tasks,
                goal_id=goal_id,
                goal_text=goal_text,
                replace=replace,
                decision_token=os.environ.get("STARDEW_DECISION_TOKEN"),
            )
            return {"saveId": sid, "executionScope": "one_short_job", **result}
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def remember_intent(
        intent: str,
        kind: str = "goal",
        goal_id: str | None = None,
        priority: int = 0,
        trigger: dict[str, Any] | None = None,
        expiry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Remember a long-term goal (kind='goal') or a future cross-day todo (kind='todo').

        Model-created goals are always recorded with source=agent, so the model can
        never label its own idea as a player instruction.

        Exact ``trigger`` shapes (call discover_capabilities("memory") for the same
        schema)::

            {"type": "calendar", "year": 2, "season": "spring", "day": 3}
            {"type": "inventory", "itemId": "(O)368", "minCount": 5}
            {"type": "crop", "cropId": "(O)24", "state": "harvestable"}

        Invalid (rejected with the allowed list): {"type": "daily"},
        {"type": "date", "date": "spring 3"}, {"type": "next_day"}.
        A trigger is required for kind='todo' and ignored for kind='goal'.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            result = store.remember_intent(
                sid,
                intent=intent,
                kind=kind,
                goal_id=goal_id,
                priority=priority,
                trigger=trigger,
                expiry=expiry,
            )
            return {"saveId": sid, **result}
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def run_next_step() -> dict[str, Any]:
        """Harness worker: claim one ready plan step, execute it, and commit the result.

        This is the same execution path the ChatBridge internal plan worker uses
        (shared ``PlanExecutor``): the stable command id is persisted before
        dispatch and forwarded to the real scheduler/native command, and
        completion is committed only from the real execution result. Returns
        status=idle without any LLM wake when the plan has no executable work.
        """
        sid = await current_save_id()
        store = work_for_run()

        def _snapshot_state() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            # Explicit waiting conditions are evaluated against the latest cached
            # native snapshot only; this never issues a new farm query.
            cached = sched.latest_snapshot
            payload = cached.get("payload") if isinstance(cached, dict) else None
            if not isinstance(payload, dict):
                return None, None
            world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
            game_date = {
                "year": world.get("year"),
                "season": world.get("season"),
                "day": world.get("dayOfMonth"),
            }
            return payload, game_date

        executor = PlanExecutor(
            store,
            dispatch=execute_plan_operation,
            reconcile=reconcile_native_command,
            snapshot_provider=_snapshot_state,
        )
        execution = await executor.run_once(sid, f"mcp-{uuid.uuid4().hex[:8]}", recover=True)
        result = execution.as_dict()
        snapshot, game_date = _snapshot_state()
        if execution.status == "idle":
            return {
                **result,
                "hasExecutableWork": False,
                "overview": store.overview(sid, snapshot=snapshot, game_date=game_date),
            }
        return {
            **result,
            "hasExecutableWork": store.has_ready_step(
                sid, snapshot=snapshot, game_date=game_date
            ),
            "overview": store.overview(sid, snapshot=snapshot, game_date=game_date),
        }

    @mcp.tool()
    async def harvest_and_store(
        chest_x: int,
        chest_y: int,
        max_tiles: int = 16,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Harvest mature crops and deposit the harvested non-tool items into the named chest.

        Only the chest coordinates and item ids supplied by the caller are touched;
        no chest is chosen automatically.
        """
        harvest = await sched.harvest_auto(max_tiles=max_tiles)
        harvest_effects = harvest.get("effects") or []
        if harvest.get("status") == "executing" or harvest.get("terminalState") == "running":
            return {
                "outcome": "unknown",
                "goalSatisfied": False,
                "effects": harvest_effects,
                "remaining": {"stage": "harvest"},
                "reasonCode": "HARVEST_IN_PROGRESS",
                "snapshotRevision": harvest.get("worldRevision"),
                "status": "executing",
                "terminalState": "running",
                "stage": "harvest",
                "harvest": harvest,
            }
        harvest_ok = harvest.get("terminalState") == "succeeded"
        if harvest.get("status") == "no-work" or harvest.get("terminalState") == "none":
            return {
                "outcome": "completed",
                "goalSatisfied": True,
                "effects": [],
                "remaining": {"stage": None},
                "reasonCode": "NO_WORK",
                "snapshotRevision": harvest.get("worldRevision"),
                "status": "succeeded",
                "stage": "done",
                "harvest": harvest,
                "message": "No mature crops to harvest; nothing to store.",
            }
        if not harvest_ok and not harvest.get("completedCount"):
            return {
                "outcome": "partial",
                "goalSatisfied": False,
                "effects": harvest_effects,
                "remaining": {"stage": "harvest"},
                "reasonCode": "HARVEST_FAILED",
                "snapshotRevision": harvest.get("worldRevision"),
                "status": "partial",
                "stage": "harvest",
                "harvest": harvest,
            }

        deposit = await sched.deposit_to_chest(chest_x=chest_x, chest_y=chest_y, item_ids=item_ids)
        deposit_effects = deposit.get("effects") or []
        if deposit.get("status") == "executing" or deposit.get("terminalState") == "running":
            return {
                "outcome": "unknown",
                "goalSatisfied": False,
                "effects": harvest_effects + deposit_effects,
                "remaining": {"stage": "deposit"},
                "reasonCode": "DEPOSIT_IN_PROGRESS",
                "snapshotRevision": deposit.get("worldRevision"),
                "status": "executing",
                "terminalState": "running",
                "stage": "deposit",
                "harvest": harvest,
                "deposit": deposit,
            }
        deposit_ok = deposit.get("terminalState") == "succeeded"
        goal_satisfied = harvest_ok and deposit_ok
        return {
            "outcome": "completed" if goal_satisfied else "partial",
            "goalSatisfied": goal_satisfied,
            "effects": harvest_effects + deposit_effects,
            "remaining": {"stage": None if goal_satisfied else "deposit"},
            "reasonCode": "OK" if goal_satisfied else "STORE_INCOMPLETE",
            "snapshotRevision": deposit.get("worldRevision") or harvest.get("worldRevision"),
            "status": "succeeded" if goal_satisfied else "partial",
            "stage": "done",
            "harvest": harvest,
            "deposit": deposit,
        }

    @mcp.tool()
    async def discover_capabilities(group: str | None = None) -> dict[str, Any]:
        """Discover base tool groups and their short parameter schema on demand.

        Returns one group at a time (farm/shopping/chest/movement/knowledge) so the
        full base schemas are not carried in every request. Invoke a discovered tool
        with call_capability(name, params).
        """
        if group is not None and group not in CAPABILITY_GROUPS:
            raise ToolError(
                f"unknown capability group '{group}'; available: {', '.join(sorted(CAPABILITY_GROUPS))}"
            )
        selected = {group: CAPABILITY_GROUPS[group]} if group else CAPABILITY_GROUPS
        groups: dict[str, list[dict[str, Any]]] = {}
        for group_name, names in selected.items():
            entries: list[dict[str, Any]] = []
            for name in names:
                schema = base_schemas.get(name, {})
                parameters = schema.get("parameters") or {}
                properties = parameters.get("properties") or {}
                description = str(schema.get("description") or "").split("\n")[0]
                entries.append(
                    {
                        "name": name,
                        "description": description[:140],
                        "required": list(parameters.get("required") or []),
                        "parameters": {
                            key: (value.get("type") if isinstance(value, dict) else "any")
                            for key, value in properties.items()
                        },
                    }
                )
            groups[group_name] = entries

        response: dict[str, Any] = {
            "groups": groups,
            "callWith": "call_capability(tool, params)",
            "fullToolListExposed": resolved_surface == "full",
        }
        if group in (None, "memory"):
            # The exact nested write schema, so the model never has to guess
            # trigger kinds such as "daily"/"date"/"next_day".
            response["memoryWriteSchema"] = MEMORY_WRITE_SCHEMA
        return response

    @mcp.tool()
    async def call_capability(tool: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a base capability by name with structured params.

        This runs the exact same implementation, validation and result shape as the
        corresponding base tool; it is not a parallel code path.
        """
        if tool not in base_tools:
            raise ToolError(f"unknown capability '{tool}'; call discover_capabilities first")
        schema = base_schemas.get(tool, {})
        parameters = schema.get("parameters") or {}
        properties = parameters.get("properties") or {}
        required = parameters.get("required") or []
        args = dict(params or {})
        if properties:
            unknown = sorted(key for key in args if key not in properties)
            if unknown:
                raise ToolError(f"unsupported parameter(s) for '{tool}': {', '.join(unknown)}")
        missing = sorted(key for key in required if key not in args)
        if missing:
            raise ToolError(f"missing required parameter(s) for '{tool}': {', '.join(missing)}")
        try:
            result = await base_tools[tool](**args)
        except ToolError:
            raise
        except (PolicyViolationError, SchedulerError, NoActiveTaskError) as ex:
            raise ToolError(str(ex)) from None
        return {"tool": tool, "result": result}

    @mcp.tool()
    async def query_wiki(query: str) -> dict[str, Any]:
        """Look up strategy knowledge only when needed; returns short results with source and cache status."""
        try:
            return await asyncio.to_thread(wiki_for_run().lookup, query)
        except ValueError as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Wiki lookup unavailable: {ex}") from None

    @mcp.tool()
    async def water_zone(
        center_x: int, center_y: int, radius: int = 0, include_empty_tiles: bool = False, detail: bool = False
    ) -> dict[str, Any]:
        """Water crops around center (center_x, center_y) with radius 0 (1x1), 1 (3x3), or 2 (5x5).

        Returns terminalState, counts, and remaining unwatered crops.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            zone_kwargs = {"center_x": center_x, "center_y": center_y, "radius": radius}
            if include_empty_tiles:
                zone_kwargs["include_empty_tiles"] = True
            res = await sched.execute_water_zone(**zone_kwargs)
            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "completedCount": res.get("completedCount", 0),
                    "fresh": False,
                    "message": res.get("message") or f"Water zone task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)
            remaining_unwatered = None
            if fresh and fresh_snap:
                fw = fresh_snap.get("payload", {}).get("farmWork") or {}
                remaining_unwatered = fw.get("tilledUnwateredCount")

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "remainingUnwateredCount": remaining_unwatered,
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = res.get("effects", [])
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Water zone execution rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Water zone execution failed: {ex}") from None

    @mcp.tool()
    async def water_auto(max_tiles: int = 25, include_empty_tiles: bool = False, detail: bool = False) -> dict[str, Any]:
        """Water crops only by default; include empty prepared soil only when explicitly enabled."""
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            auto_kwargs = {"max_tiles": max_tiles}
            if include_empty_tiles:
                auto_kwargs["include_empty_tiles"] = True
            res = await sched.water_auto(**auto_kwargs)
            if res.get("status") == "no-work":
                return {
                    "status": "no-work",
                    "terminalState": "none",
                    "targetCount": 0,
                    "remainingUnwateredCount": 0,
                    "fresh": True,
                    "message": res.get("message", "No unwatered tilled tiles found."),
                }

            if res.get("status") == "executing":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "targetCount": res.get("targetCount", 0),
                    "completedCount": res.get("completedCount", 0),
                    "remainingUnwateredCount": res.get("remainingUnwateredCount"),
                    "fresh": False,
                    "message": res.get("message", "Task is executing on companion in background."),
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)
            remaining_unwatered = None
            if fresh and fresh_snap:
                fw = fresh_snap.get("payload", {}).get("farmWork") or {}
                remaining_unwatered = fw.get("tilledUnwateredCount")

            details = res.get("details") or {}
            failed_tiles = details.get("failedTiles") or []
            skipped_tiles = details.get("skippedTiles") or []
            blocked_targets = []
            for ft in failed_tiles:
                if isinstance(ft, dict):
                    reason = str(ft.get("reason", ""))
                    if any(k in reason.lower() for k in ("obstacle", "replan", "unreachable", "blocked", "obstruction")):
                        t = ft.get("tile") or {}
                        blocked_targets.append({"x": t.get("x"), "y": t.get("y"), "reason": reason, "retryable": False})
            for st in skipped_tiles:
                if isinstance(st, dict):
                    reason = str(st.get("reason", ""))
                    if any(k in reason.lower() for k in ("obstruction", "unreachable", "blocked", "obstacle")):
                        t = st.get("tile") or {}
                        blocked_targets.append({"x": t.get("x"), "y": t.get("y"), "reason": reason, "retryable": False})

            watered_tiles = [
                {"x": eff["tile"]["x"], "y": eff["tile"]["y"]}
                for eff in res.get("effects", [])
                if isinstance(eff, dict) and eff.get("state") == "watered" and isinstance(eff.get("tile"), dict)
            ]

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "targetCount": res.get("targetCount", 0),
                "remainingUnwateredCount": remaining_unwatered,
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if watered_tiles:
                response["wateredTiles"] = watered_tiles
            if blocked_targets:
                response["blockedTargets"] = blocked_targets
                response["actionableSummary"] = (
                    f"Non-retryable blocked targets: {len(blocked_targets)} tiles blocked by dynamic obstacles or actors "
                    f"(maximum replans exceeded). Do not immediately retry identical water_auto until the blocking actor/obstacle moves. "
                    f"You may use water_zone on other reachable areas."
                )
            if detail:
                response["targetTiles"] = res.get("targetTiles", [])
                response["effects"] = res.get("effects", [])
                response["isTruncated"] = res.get("isTruncated", False)
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Auto watering rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Auto watering failed: {ex}") from None


    @mcp.tool()
    async def query_inventory(detail: bool = False) -> dict[str, Any]:
        """Query companion backpack: freeSlots, capacity, non-tool items. Full if detail=True."""
        try:
            res = await sched.query_inventory()
            if detail:
                return res
            inv = res.get("inventory", {})
            return {
                "inventory": {
                    "capacity": inv.get("capacity", 0),
                    "freeSlots": inv.get("freeSlots", 0),
                    "nonToolItems": extract_non_tool_items(inv.get("slots", [])),
                },
                "worldRevision": res.get("worldRevision", 0),
            }
        except SchedulerError as ex:
            raise ToolError(f"Failed to query inventory: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error querying inventory: {ex}") from None

    @mcp.tool()
    async def query_chests(
        item_id: str | None = None,
        name: str | None = None,
        is_seed: bool | None = None,
        page: int = 1,
        page_size: int = 20,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Query farm chests: coordinates, capacity, freeSlots, items.
        Supports filtering by item_id, name, is_seed, and pagination (default 20/page).
        Concise summary by default (items truncated per chest); full if detail=True.
        """
        try:
            res = await sched.query_chests()
            all_chests = res.get("chests", [])

            def matches_filter(item: dict[str, Any]) -> bool:
                if item_id is not None:
                    iid = str(item.get("itemId", "")).lower()
                    target_id = item_id.strip().lower()
                    if iid != target_id and not iid.endswith(target_id):
                        return False
                if name is not None:
                    iname = str(item.get("name", "")).lower()
                    if name.strip().lower() not in iname:
                        return False
                if is_seed is not None:
                    cat = item.get("category")
                    iid = str(item.get("itemId", "")).lower()
                    iname = str(item.get("name", "")).lower()
                    is_it_seed = (
                        cat == -74
                        or "seed" in iname
                        or "starter" in iname
                        or "spores" in iname
                        or "seed" in iid
                    )
                    if is_it_seed != is_seed:
                        return False
                return True

            has_filter = (item_id is not None) or (name is not None) or (is_seed is not None)

            filtered_chests = []
            for c in all_chests:
                tile = c.get("tile", {})
                cx = tile.get("x", 0)
                cy = tile.get("y", 0)
                contents = c.get("contents", [])

                if has_filter:
                    matched_items = [it for it in contents if matches_filter(it)]
                    if not matched_items:
                        continue
                    if detail:
                        c_copy = dict(c)
                        c_copy["contents"] = matched_items
                        filtered_chests.append(c_copy)
                    else:
                        summary = extract_chest_summary(c, cx, cy, None, detail=False)
                        matched_in_summary = [it for it in summary["items"] if matches_filter(it)]
                        filtered_chests.append({
                            "tile": summary["tile"],
                            "capacity": summary["capacity"],
                            "freeSlots": summary["freeSlots"],
                            "itemCount": summary["itemCount"],
                            "matchingItemCount": len(matched_items),
                            "items": matched_in_summary[:6],
                            "itemsTruncated": len(matched_items) > 6,
                            "hasDuplicateStacks": summary["hasDuplicateStacks"],
                            "hasMergeableStacks": summary["hasMergeableStacks"],
                        })
                else:
                    if detail:
                        filtered_chests.append(c)
                    else:
                        summary = extract_chest_summary(c, cx, cy, None, detail=False)
                        filtered_chests.append({
                            "tile": summary["tile"],
                            "capacity": summary["capacity"],
                            "freeSlots": summary["freeSlots"],
                            "itemCount": summary["itemCount"],
                            "items": summary["items"],
                            "itemsTruncated": summary["itemsTruncated"],
                            "hasDuplicateStacks": summary["hasDuplicateStacks"],
                            "hasMergeableStacks": summary["hasMergeableStacks"],
                        })

            safe_page_size = max(1, min(page_size, 50))
            safe_page = max(1, page)
            start_idx = (safe_page - 1) * safe_page_size
            end_idx = start_idx + safe_page_size
            paged_chests = filtered_chests[start_idx:end_idx]
            total_pages = max(1, (len(filtered_chests) + safe_page_size - 1) // safe_page_size)

            return {
                "chests": paged_chests,
                "totalChests": len(all_chests),
                "matchingChests": len(filtered_chests),
                "page": safe_page,
                "pageSize": safe_page_size,
                "totalPages": total_pages,
                "isTruncated": bool(res.get("isTruncated", False)) or (len(filtered_chests) > safe_page_size),
                "worldRevision": res.get("worldRevision", 0),
            }
        except SchedulerError as ex:
            raise ToolError(f"Failed to query chests: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error querying chests: {ex}") from None

    @mcp.tool()
    async def query_planting_options(detail: bool = False) -> dict[str, Any]:
        """Query companion seeds, season, tillable dirt and tilled empty tiles for planting.

        Returns available seeds, hoe availability, tillable and hoed tile candidates within range.
        Default is concise for agent decision; pass detail=True for full tile lists & metadata.
        """
        try:
            return await sched.query_planting_options(detail=detail)
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
        """Query shop stock, prices, operating status (open/closed), locationId,
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
    async def harvest_auto(max_tiles: int = 16, detail: bool = False) -> dict[str, Any]:
        """Auto-harvest up to max_tiles (1..64) mature crops. Stops early if inventory is full.

        Returns remaining mature crop count and updated backpack space.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            res = await sched.harvest_auto(max_tiles=max_tiles)
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
        chest_x: int, chest_y: int, item_ids: list[str] | None = None, detail: bool = False
    ) -> dict[str, Any]:
        """Deposit non-tool items into chest at (chest_x, chest_y). Tools are never deposited.

        Stops early if chest is full. Returns updated chest and backpack status.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            res = await sched.deposit_to_chest(
                chest_x=chest_x,
                chest_y=chest_y,
                item_ids=item_ids,
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
        chest_x: int,
        chest_y: int,
        item_id: str | None = None,
        count: int = 1,
        items: list[dict[str, Any]] | None = None,
        location_id: str = "Farm",
        detail: bool = False,
    ) -> dict[str, Any]:
        """Withdraw items from chest at (chest_x, chest_y) into companion backpack.

        Specify either item_id and count, or a list of items [{itemId, count}].
        Returns updated chest and backpack status.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            res = await sched.withdraw_from_chest(
                chest_x=chest_x,
                chest_y=chest_y,
                items=items,
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
    async def organize_chest(chest_x: int, chest_y: int, detail: bool = False) -> dict[str, Any]:
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
        tiles: list[dict[str, int]], detail: bool = False
    ) -> dict[str, Any]:
        """Use hoe on specified dirt tiles (1..64 coordinates) to till them.

        Protects existing crops and objects.
        Returns terminalState, counts, affectedTiles, candidateTiles (fresh=True/False), and status.
        """
        pre_rev_getter = getattr(sched, "latest_world_revision", 0)
        pre_rev = pre_rev_getter() if callable(pre_rev_getter) else int(pre_rev_getter)
        try:
            res = await sched.execute_hoe_tiles(tiles=tiles)

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
        seed_item_id: str, tiles: list[dict[str, int]], detail: bool = False
    ) -> dict[str, Any]:
        """Plant specified seeds into tilled empty tiles (1..64 coordinates).

        Consumes seeds from companion backpack. Protects existing crops.
        Returns terminalState, counts, plantedTiles, remainingSeedStack, and status.
        """
        pre_rev_getter = getattr(sched, "latest_world_revision", 0)
        pre_rev = pre_rev_getter() if callable(pre_rev_getter) else int(pre_rev_getter)
        try:
            res = await sched.execute_plant_seeds(seed_item_id=seed_item_id, tiles=tiles)

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
        items: list[dict[str, Any]], detail: bool = False
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
            res = await sched.execute_ship_items(items=items)

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
        items: list[dict[str, Any]],
        budget_limit: int,
        shop_id: str = "SeedShop",
        detail: bool = False,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Purchase items from a shop counter using native game wallet transactions.

        Requires companion to be inside the target shop location and adjacent to the counter.
        Stock, limited stock status, dynamic unit price, and budget/funds limits are evaluated.
        Accepts items list where each item has itemId and count (1..36 items).
        budget_limit specifies the maximum currency authorized for this purchase call.
        Returns terminalState, completedCount, totalCost, remainingBudget, purchasedItems,
        skippedItems, skipReason, availableMoneyAfter, rollbackPerformed, fresh, details, error,
        and effects (if detail=True).
        """
        pre_rev_getter = getattr(sched, "latest_world_revision", 0)
        pre_rev = pre_rev_getter() if callable(pre_rev_getter) else int(pre_rev_getter)
        sid = await current_save_id()
        autonomy = autonomy_for_run()
        auto_state = autonomy.state(sid)
        is_free = auto_state.mode == "free"
        effective_budget = budget_limit
        command_id = command_id or f"purchase-{uuid.uuid4().hex[:16]}"
        quoted_cost: int | None = None
        try:
            if is_free:
                if command_id in auto_state.settled_spend_commands:
                    return {"status": "replayed", "terminalState": "succeeded", "commandId": command_id,
                            "totalCost": 0, "message": "该购买 commandId 已结算，未重复购买。"}
                # Before accepting a different purchase, reconcile every older
                # reservation. An unverified spend blocks further shopping.
                for pending_id in list(auto_state.spend_reservations):
                    if pending_id == command_id:
                        continue
                    recovered = await sched.reconcile_command(pending_id)
                    if recovered is None:
                        return {
                            "status": "rejected",
                            "terminalState": "unknown",
                            "commandId": command_id,
                            "error": "AUTONOMY_PENDING_RECONCILIATION",
                            "message": "存在未核对的购买命令，核对完成前不会继续购物。",
                        }
                    recovered_details = recovered.get("details") if isinstance(recovered, dict) else None
                    recovered_details = recovered_details if isinstance(recovered_details, dict) else {}
                    recovered_terminal = recovered.get("terminalState") if isinstance(recovered, dict) else None
                    recovered_cost = recovered_details.get("totalCost")
                    settleable = recovered_terminal in {"succeeded", "failed", "cancelled"} and isinstance(recovered_cost, int) and recovered_cost >= 0
                    autonomy.settle_spend(pending_id, recovered_cost if settleable else None, unknown=not settleable)
                    if not settleable:
                        return {
                            "status": "rejected", "terminalState": "unknown", "commandId": command_id,
                            "error": "AUTONOMY_PENDING_RECONCILIATION",
                            "message": "存在结果未知的购买命令，核对完成前不会继续购物。",
                        }
                auto_state = autonomy.state(sid)
                active = getattr(sched, "active_task", None)
                active_matches = bool(active and getattr(active, "requested_command_id", None) == command_id)
                if command_id in auto_state.spend_reservations and not active_matches:
                    # A reconnect/retry must first recover the original command;
                    # never blind-dispatch a second purchase for the same ID.
                    recovered = await sched.reconcile_command(command_id)
                    if recovered is None:
                        return {
                            "status": "executing",
                            "terminalState": "running",
                            "inProgress": True,
                            "commandId": command_id,
                            "message": "原购买命令仍待核对，已保留预算；核对完成前不会重新购物。",
                        }
                    recovered_details = recovered.get("details") if isinstance(recovered, dict) else None
                    recovered_details = recovered_details if isinstance(recovered_details, dict) else {}
                    recovered_terminal = recovered.get("terminalState") if isinstance(recovered, dict) else None
                    recovered_cost = recovered_details.get("totalCost")
                    settleable = recovered_terminal in {"succeeded", "failed", "cancelled"} and isinstance(recovered_cost, int) and recovered_cost >= 0
                    autonomy.settle_spend(sid, command_id, recovered_cost if settleable else None, unknown=not settleable)
                    if not settleable:
                        return {
                            "status": "executing", "terminalState": "unknown", "inProgress": True,
                            "commandId": command_id, "details": recovered_details,
                            "message": "原购买命令结果仍未知，已保留预算。",
                        }
                    return {
                        "status": "executed", "terminalState": recovered_terminal,
                        "commandId": command_id, "taskId": recovered.get("taskId"),
                        "totalCost": recovered_cost, "details": recovered_details,
                        "error": recovered.get("error"),
                    }
                remaining = max(0, (auto_state.budget_limit or 0) - auto_state.daily_spend - sum(auto_state.spend_reservations.values()))
                effective_budget = min(budget_limit, remaining)
                if effective_budget <= 0:
                    return {"status": "rejected", "terminalState": "none", "error": "AUTONOMY_BUDGET_EXHAUSTED", "message": "自由模式每日购买预算不足。"}
                # Quote from the native shop snapshot. Never infer a price.
                shop = await sched.query_shop(shop_id=shop_id, detail=True)
                native_items = {str(item.get("itemId")): item for item in (shop.get("items") or []) if isinstance(item, dict)}
                quote = 0
                for requested in items:
                    native = native_items.get(str(requested.get("itemId")))
                    price = native.get("price") if native else None
                    count = requested.get("count")
                    if not isinstance(price, int) or price < 0 or not isinstance(count, int):
                        raise ToolError("无法取得原生报价，已阻止自由模式购买。")
                    quote += price * count
                quoted_cost = quote
                # Pass the already-reduced call allowance; reserve_spend applies
                # the shared daily remaining formula again under its lock.
                try:
                    autonomy.reserve_spend(sid, command_id, quoted_cost, limit=effective_budget)
                except ValueError:
                    return {
                        "status": "rejected",
                        "terminalState": "none",
                        "commandId": command_id,
                        "error": "AUTONOMY_BUDGET_EXHAUSTED",
                        "message": "自由模式每日购买预算不足。",
                    }
            res = await sched.execute_purchase_items(
                items=items,
                budget_limit=effective_budget,
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

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)

            details = res.get("details") or {}
            purchased_items = details.get("purchasedItems", [])
            skipped_items = details.get("skippedItems", [])
            total_cost = details.get("totalCost")
            if is_free:
                terminal = res.get("terminalState")
                terminal_states = {"succeeded", "failed", "cancelled"}
                settleable = terminal in terminal_states and isinstance(total_cost, int) and total_cost >= 0
                autonomy.settle_spend(
                    sid,
                    command_id,
                    total_cost if settleable else None,
                    unknown=not settleable,
                )
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
    async def navigate_to(
        location_id: str,
        tile_x: int | None = None,
        tile_y: int | None = None,
        x: int | None = None,
        y: int | None = None,
        tile: dict[str, int] | None = None,
        landmark: str | None = None,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Navigate the companion across maps to a destination tile or dynamic interaction point.

        Discovers valid routes dynamically using live map warps and doors, walks continuously
        on each map, and transitions across map boundaries.
        Destination coordinates can be provided via `tile` (dict with 'x', 'y'), or `tile_x`/`tile_y`, or `x`/`y`.
        If tile is omitted, resolves native dynamic interaction points from active game data
        (e.g. shop counter interactionTile when navigating to a shop). For other locations, explicit
        destination coordinates must be provided. Hardcoded coordinates are unsupported.
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
        """Composite farming workflow tool: acquires seeds (backpack or chest),
        identifies planting tiles, tills dirt if necessary (auto_till), plants seeds,
        and waters planted crops (auto_water).

        Recommended for natural language requests such as 'plant 3 carrots' or 'take seeds from chest and plant them'.
        Returns structured outcome with seedsPlanted, tilesHoed, tilesWatered, and pendingDecision if blocked.
        """
        crop_param = seed_item_id or crop_name_or_id
        if not crop_param or not isinstance(crop_param, str):
            raise ToolError("Must provide seed_item_id or crop_name_or_id as a non-empty string.")
        try:
            res = await sched.plant_crop_workflow(
                crop_name_or_id=crop_param.strip(),
                count=count,
                target_tiles=target_tiles,
                auto_till=auto_till,
                water=auto_water,
                withdraw_from_chest=withdraw_from_chest,
                chest_tile=chest_tile,
                location_id=location_id,
            )
            if not detail and "effects" in res:
                res = dict(res)
                res.pop("effects", None)
            return res
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Plant crop workflow rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Plant crop workflow failed: {ex}") from None

    @mcp.tool()
    async def observe_farming_helpers(location_id: str = "Farm") -> dict[str, Any]:
        """Observe the farming-help group only: native refill-water tiles, ground items, fertilized tiles, choppable trees.

        This is the on-demand group surface: it never dumps the backpack or chests.
        Use it to choose explicit tiles for refill_watering_can / clear_debris /
        pickup_items / chop_tree.
        """
        try:
            return await sched.query_farming_helpers(location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Failed to observe farming helpers: {ex}") from None

    @mcp.tool()
    async def observe_machines(location_id: str = "Farm") -> dict[str, Any]:
        """Observe placed machines only: idle / processing (minutes left) / ready output.

        Returns real native state. `isReady=true` means collect_machine can collect it.
        """
        try:
            return await sched.query_machines(location_id=location_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Failed to observe machines: {ex}") from None

    @mcp.tool()
    async def observe_livestock() -> dict[str, Any]:
        """Observe livestock only: buildings (hay, capacity, door), animals and their produce state.

        Each animal carries its last observed tile plus the native harvest type/tool so
        the model can pick the applicable action instead of guessing.
        """
        try:
            return await sched.query_livestock()
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

    def _resolve_animal_tile(animal_name: str) -> dict[str, Any]:
        group = _latest_livestock_group()
        candidates: list[dict[str, Any]] = []
        for building in group.get("buildings") or []:
            if isinstance(building, dict):
                candidates.extend(a for a in (building.get("animals") or []) if isinstance(a, dict))
        candidates.extend(a for a in (group.get("roamingAnimals") or []) if isinstance(a, dict))
        wanted = animal_name.strip().lower()
        for animal in candidates:
            if str(animal.get("name", "")).strip().lower() == wanted and isinstance(animal.get("tile"), dict):
                return {"x": int(animal["tile"]["x"]), "y": int(animal["tile"]["y"])}
        raise ToolError(
            f"animal '{animal_name}' has no observed tile; call observe_livestock first "
            "or pass an explicit tile."
        )

    @mcp.tool()
    async def refill_watering_can(location_id: str = "Farm", max_tiles: int = 4, tiles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Refill the companion's watering can at native refill tiles on its own map.

        Uses the game's own CanRefillWateringCanOnTile judgement; never a hardcoded
        water capacity or cost. Returns an actionable error when no refill tile is known.
        """
        try:
            res = await sched.refill_watering_can(location_id=location_id, max_tiles=max_tiles, tiles=tiles)
            return _native_action_response("refill-watering-can", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Refill watering can failed: {ex}") from None

    @mcp.tool()
    async def apply_fertilizer(
        tiles: list[dict[str, Any]],
        fertilizer_item_id: str,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Apply one explicit fertilizer item from the companion backpack onto explicit tilled tiles.

        The native fertilizer rules (`HoeDirt.CheckApplyFertilizerRules`) decide
        applicability; an already-fertilized or invalid tile is skipped with a reason.
        """
        try:
            res = await sched.apply_fertilizer(
                tiles=tiles, fertilizer_item_id=fertilizer_item_id, location_id=location_id
            )
            return _native_action_response("apply-fertilizer", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Apply fertilizer failed: {ex}") from None

    @mcp.tool()
    async def clear_debris(tiles: list[dict[str, Any]], location_id: str = "Farm") -> dict[str, Any]:
        """Clear explicitly selected native weeds, stones or twigs on the given tiles.

        The native tool choice is the game's own rule: weeds with the Hoe/Axe, stones
        with the Pickaxe, twigs with the Axe/Pickaxe. Crops, machines, chests and
        buildings are protected. When the companion genuinely lacks the required tool
        the action returns an actionable ``missing-tool:<Tool>`` precondition, never a
        fake success.
        """
        try:
            res = await sched.clear_debris(tiles=tiles, location_id=location_id)
            return _native_action_response("clear-debris", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Clear debris failed: {ex}") from None

    @mcp.tool()
    async def pickup_items(tiles: list[dict[str, Any]], location_id: str = "Farm") -> dict[str, Any]:
        """Pick up dropped debris / spawned items on explicitly selected tiles.

        Uses the native debris collect path and the native ground-item check action.
        """
        try:
            res = await sched.pickup_items(tiles=tiles, location_id=location_id)
            return _native_action_response("pickup-items", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Pickup items failed: {ex}") from None

    @mcp.tool()
    async def chop_tree(tiles: list[dict[str, Any]], location_id: str = "Farm") -> dict[str, Any]:
        """Chop explicitly selected wild trees, giant stumps or hollow logs with the companion's own Axe.

        Every swing is a real native Axe.DoFunction (companion stamina, native damage
        and drops). Fruit trees are protected (protected-tree); a tile without a
        choppable target returns no-tree; a missing Axe returns missing-tool:Axe.
        """
        try:
            res = await sched.chop_tree(tiles=tiles, location_id=location_id)
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
    async def collect_machine(tiles: list[dict[str, Any]], location_id: str = "Farm") -> dict[str, Any]:
        """Collect ready output from explicit machine tiles using the native check action."""
        try:
            res = await sched.collect_machine(tiles=tiles, location_id=location_id)
            return _native_action_response("collect-machine", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Collect machine failed: {ex}") from None

    @mcp.tool()
    async def pet_animal(
        animal_name: str,
        tile: dict[str, Any] | None = None,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Pet one named animal via the native FarmAnimal.pet path.

        The tile defaults to the animal's last observed position from observe_livestock.
        """
        try:
            target = tile or _resolve_animal_tile(animal_name)
            res = await sched.pet_animal(animal_name=animal_name, tile=target, location_id=location_id)
            return _native_action_response("pet-animal", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Pet animal failed: {ex}") from None

    @mcp.tool()
    async def collect_animal_produce(
        animal_name: str,
        tile: dict[str, Any] | None = None,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Collect one animal's produce through its applicable native path.

        Drop-overnight produce is collected with the native ground check action; produce
        that needs a tool (milk pail / shears) is collected with the companion's own
        native tool. Only when the companion genuinely carries no such tool does the
        action return an actionable ``missing-tool:<Tool>`` precondition.
        """
        try:
            target = tile or _resolve_animal_tile(animal_name)
            res = await sched.collect_animal_produce(
                animal_name=animal_name, tile=target, location_id=location_id
            )
            return _native_action_response("collect-animal-produce", res)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Collect animal produce failed: {ex}") from None

    @mcp.tool()
    async def feed_animals(building_name: str) -> dict[str, Any]:
        """Feed every animal inside one animal building interior through the native feed path.

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
        tiles: list[dict[str, Any]] | None = None,
        building_name: str | None = None,
        location_id: str = "Farm",
    ) -> dict[str, Any]:
        """Open/close an animal building door through the native Building.ToggleAnimalDoor path.

        Pass either explicit door/building tiles or a building_name resolved from
        observe_livestock. The companion must be adjacent to the door.
        """
        try:
            resolved = tiles
            if not resolved and building_name:
                group = _latest_livestock_group()
                resolved = []
                for building in group.get("buildings") or []:
                    if not isinstance(building, dict):
                        continue
                    names = {str(building.get("indoorsName", "")).lower(), str(building.get("buildingType", "")).lower()}
                    if building_name.strip().lower() in names and isinstance(building.get("doorTile"), dict):
                        resolved = [{"x": int(building["doorTile"]["x"]), "y": int(building["doorTile"]["y"])}]
                        break
            if not resolved:
                raise ToolError(
                    "provide tiles or a building_name known from observe_livestock"
                )
            res = await sched.toggle_animal_door(tiles=resolved, location_id=location_id)
            return _native_action_response("toggle-animal-door", res)
        except ToolError:
            raise
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Toggle animal door failed: {ex}") from None

    @mcp.tool()
    async def pause_task() -> dict[str, Any]:
        """Pause the currently active companion task."""
        await disable_autonomy_after_task_control()
        try:
            result = await sched.pause_task()
            return result
        except NoActiveTaskError as ex:
            raise ToolError(f"Cannot pause: {ex}") from None
        except SchedulerError as ex:
            raise ToolError(f"Pause failed: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected pause error: {ex}") from None

    @mcp.tool()
    async def resume_task() -> dict[str, Any]:
        """Resume the currently paused companion task."""
        try:
            return await sched.resume_task()
        except NoActiveTaskError as ex:
            raise ToolError(f"Cannot resume: {ex}") from None
        except SchedulerError as ex:
            raise ToolError(f"Resume failed: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected resume error: {ex}") from None

    @mcp.tool()
    async def cancel_task(
        reason: str = "User cancelled via MCP",
    ) -> dict[str, Any]:
        """Cancel the currently active companion task safely."""
        await disable_autonomy_after_task_control()
        try:
            result = await sched.cancel_task(reason=reason)
            return result
        except NoActiveTaskError as ex:
            raise ToolError(f"Cannot cancel: {ex}") from None
        except SchedulerError as ex:
            raise ToolError(f"Cancel failed: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected cancel error: {ex}") from None

    @mcp.tool()
    async def reconcile_plan_command(command_id: str) -> dict[str, Any]:
        """Read the Mod's cached terminal result for a persisted plan command id.

        Read-only: it never dispatches. The internal plan worker uses it to settle
        a step whose previous attempt is unconfirmed instead of re-dispatching it
        and risking a double charge/action. Returns ``found=False`` when the Mod
        has no terminal result for the id.
        """
        if not command_id:
            return {"found": False, "commandId": command_id, "reasonCode": "MISSING_COMMAND_ID"}
        payload = await reconcile_native_command(command_id)
        if not isinstance(payload, dict):
            return {"found": False, "commandId": command_id}
        return {"found": True, "commandId": command_id, "native": payload}

    @mcp.tool()
    async def dispatch_plan_operation(
        operation: str,
        params: dict[str, Any] | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Harness-only: execute one validated plan step with its stable command id.

        This is the internal plan worker's dispatch entry point. It runs the same
        real scheduler implementation, parameter validation and command-id
        forwarding as the ``run_next_step`` tool (both call
        ``execute_plan_operation``); it is not a parallel path and is never exposed
        on the model surface. The persisted plan command id is the idempotency
        identity the native Mod de-duplicates on.
        """
        try:
            raw_params = dict(params or {})
            entry = _PLAN_OPERATION_CALLS.get(operation)
            if entry is not None:
                _, allowed = entry
                unknown = set(raw_params) - set(allowed)
                if unknown:
                    logger.warning(
                        "Stripping unknown parameter(s) %s for operation '%s' before dispatch",
                        sorted(unknown),
                        operation,
                    )
                    filtered_params = {k: v for k, v in raw_params.items() if k in allowed}
                else:
                    filtered_params = raw_params
            else:
                filtered_params = raw_params
            return await execute_plan_operation(operation, filtered_params, command_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    mcp.scheduler = sched  # type: ignore[attr-defined]
    # Capture every real tool implementation for discovery/call, then trim the
    # default list to the lightweight surface unless the caller wants the full list.
    def protect_job(name, fn):
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            sid = await current_save_id()
            store = work_for_run()
            if name in {"pause_task", "cancel_task"}:
                store.revoke_decision(sid)
                return await fn(*args, **kwargs)
            if name == "plant_crop_workflow":
                raise ToolError("MULTIPLE_BUSINESSES: choose one short planting, watering or inventory job")
            if name not in _PLAN_OPERATION_CALLS:
                raise ToolError("SHORT_JOB_UNSUPPORTED: choose a discoverable native plan operation")
            if args:
                raise ToolError("Short job parameters must be named")
            try:
                selected = store.submit_plan(sid, goal_text="Current model-selected short job",
                    tasks=[{"title": name, "steps": [{"operation": name, "params": dict(kwargs)}]}],
                    decision_token=os.environ.get("STARDEW_DECISION_TOKEN"))
            except WorkStateError as ex:
                raise ToolError(str(ex)) from None
            return {"status": "job-selected", "taskId": selected["tasks"][0]["id"],
                    "effectStatus": "not_executed_yet", "nextBusiness": "new_model_decision_required"}
        return guarded

    readonly = {"get_status", "query_inventory", "query_chests", "query_farm_work", "query_wiki", "query_shop", "query_animals", "query_machines", "query_buildings", "query_debris", "query_location", "work_plan_overview"}
    for tool in mcp._tool_manager.list_tools():
        exempt = {"submit_plan", "remember_intent", "manage_goal", "manage_goals", "manage_plan", "manage_todo", "manage_todos", "call_capability", "set_autonomy", "autonomy_status", "run_next_step", "dispatch_plan_operation", "reconcile_plan_command", "work_plan_overview"}
        if tool.name not in exempt and tool.name not in readonly and not tool.name.startswith(("query_", "get_", "list_", "discover_", "observe_")):
            tool.fn = protect_job(tool.name, tool.fn)
        base_tools[tool.name] = tool.fn
        base_schemas[tool.name] = {
            "description": tool.description,
            "parameters": tool.parameters,
        }
    if resolved_surface == "light":
        allowed = LIGHT_TOOLS
    elif resolved_surface == "internal":
        allowed = INTERNAL_TOOLS
    else:
        allowed = None
    if allowed is not None:
        for name in sorted(base_tools):
            if name not in allowed:
                mcp.remove_tool(name)
    mcp.exposure_surface = resolved_surface  # type: ignore[attr-defined]

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="stardew-mcp-server",
        description="Model Context Protocol (MCP) Server for Stardew AI Companion",
    )
    parser.add_argument(
        "--run-dir",
        type=str,
        default=os.getenv("STARDEW_RUN_DIR"),
        help="Path to run directory containing transport-discovery.json (or set STARDEW_RUN_DIR)",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="stardew-companion",
        help="Server name reported in MCP initialize",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force the complete legacy base tool list (default surface)",
    )
    parser.add_argument(
        "--surface",
        choices=["full", "light", "internal"],
        default=None,
        help="Tool exposure surface: 'full' (generic default), 'light' (game) or 'internal' (plan worker)",
    )
    parser.add_argument(
        "--light",
        action="store_true",
        help="Alias for --surface light (game surface, base tools via discover/call)",
    )
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir) if args.run_dir else None
    surface = "light" if args.light else args.surface
    server = create_mcp_server(
        run_dir=run_dir, server_name=args.name, full=args.full or None, surface=surface
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
