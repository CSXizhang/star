"""Shared MCP schemas, projections and compatibility surfaces; no server ownership."""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from stardew_ai_runtime.decision_policy import project_context
from stardew_ai_runtime.game_tools import GAME_TOOLS


def _compact_plan_task(task: dict[str, Any]) -> dict[str, Any]:
    steps = task.get("steps") or []
    result = {key: task[key] for key in ("id", "goal_id", "title", "status", "completion_condition", "dependencies") if key in task}
    result["stepCount"] = len(steps)
    result["completedStepCount"] = sum(step.get("status") == "completed" for step in steps)
    result["effectCount"] = sum(len(step.get("effects") or []) for step in steps)
    result["stepStates"] = [{key: step[key] for key in ("id", "operation", "status", "outcome", "reason_code", "command_id") if step.get(key) is not None} for step in steps]
    result["detailsAvailable"] = True
    return result


def compact_work_overview(overview: dict[str, Any]) -> dict[str, Any]:
    """Model response projection only; durable work and internal consumers stay full.

    Never summarizes away authorization constraints or failure reasons. Native
    effect coordinates/target arrays are available via the same tool's detail flag.
    """
    result = dict(overview)
    decision = overview.get("decision") or {}
    selected_goal = decision.get("goalScope") or decision.get("goalId")
    selected_task = decision.get("taskId") or (overview.get("lastJob") or {}).get("taskId")
    goals = overview.get("goals") or []
    tasks = overview.get("tasks") or []
    result["goalCount"], result["taskCount"] = len(goals), len(tasks)
    result["goals"] = []
    ranked_goals = sorted(goals, key=lambda g: (g.get("id") != selected_goal, g.get("status") != "active"))
    for goal in ranked_goals[:3]:
        row = {key: goal[key] for key in ("id", "text", "source", "priority", "status") if key in goal}
        # milestoneSpec duplicates the goal/todos; other constraints remain exact.
        row["constraints"] = {key: value for key, value in (goal.get("constraints") or {}).items() if key != "milestoneSpec"}
        project = goal.get("project") or {}
        row["project"] = project_context(project)
        if isinstance(row["project"].get("summary"), str):
            row["project"]["summary"] = row["project"]["summary"][:320]
        if isinstance(row["project"].get("openQuestions"), list):
            row["project"]["openQuestions"] = row["project"]["openQuestions"][:3]
        row["detailsAvailable"] = bool(project or (goal.get("constraints") or {}).get("milestoneSpec"))
        result["goals"].append(row)
    ranked_tasks = sorted(tasks, key=lambda t: (t.get("id") != selected_task,
                         t.get("status") in {"completed", "cancelled"}))
    result["tasks"] = [_compact_plan_task(task) for task in ranked_tasks[:3]]
    result["currentTask"] = next((t for t in result["tasks"] if t.get("id") == selected_task), None)
    for key in ("dueTodos", "waitingConditions", "blockedBranches", "blockedDependencies", "anomalies"):
        if isinstance(overview.get(key), list):
            entries = sorted(overview[key], key=lambda row: row.get("goalId", row.get("goal_id")) != selected_goal)
            result[key] = entries[:6]
            result[key + "Count"] = len(entries)
    next_step = overview.get("nextStep")
    if isinstance(next_step, dict):
        result["nextStep"] = {key: value for key, value in next_step.items() if key != "params"}
        params = next_step.get("params") or {}
        result["nextStep"]["params"] = {key: value for key, value in params.items() if not isinstance(value, (dict, list))}
        if isinstance(params.get("tiles"), list):
            result["nextStep"]["targetCount"] = len(params["tiles"])
        result["nextStep"]["detailsAvailable"] = bool(params)
    result["recentExecutions"] = []
    for execution in (overview.get("recentExecutions") or [])[:2]:
        row = {key: execution[key] for key in ("command_id", "task_id", "step_id", "operation", "outcome", "reason_code", "game_date", "snapshot_revision") if execution.get(key) is not None}
        row["effectCount"] = len(execution.get("effects") or [])
        result["recentExecutions"].append(row)
    if overview.get("lastJob") and isinstance(overview["lastJob"], dict):
        result["lastJob"] = {key: value for key, value in overview["lastJob"].items() if key != "effects"}
        if "effectCount" not in result["lastJob"]:
            result["lastJob"]["effectSampleCount"] = len(overview["lastJob"].get("effects") or [])
    evidence = overview.get("plantingEvidence")
    if isinstance(evidence, dict):
        result["plantingEvidence"] = {key: value for key, value in evidence.items() if key not in {"batches", "recentBatches"}}
        result["plantingEvidence"]["batches"] = [{key: value for key, value in batch.items() if key != "tiles"} for batch in evidence.get("batches", [])[:3]]
        result["plantingEvidence"]["detailsAvailable"] = True
    result["detailsAvailable"] = True
    result["detailHint"] = "Use detail=True for full project/layout, step targets and native effects. For partial/unknown outcomes inspect those facts and remaining targets before replanning; never replay the original target list blindly."
    return result


def _unwrap_item_array(value: Any) -> Any:
    """Accept the provider's unambiguous XML-style array wrapper only."""
    wrapped = False
    for _ in range(8):
        if isinstance(value, dict) and set(value) == {"item"}:
            wrapped = True
            value = value["item"]
        elif isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and set(value[0]) == {"item"}:
            wrapped = True
            value = value[0]["item"]
        else:
            break
    return [value] if wrapped and not isinstance(value, list) else value


def _normalize_wire_arrays(value: Any, annotation: Any) -> Any:
    """Unwrap only fields whose declared type is an array; keep other objects intact."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _normalize_wire_arrays(value, args[0])
    if origin in (Union, UnionType):
        array_type = next((variant for variant in args if get_origin(variant) is list), None)
        return _normalize_wire_arrays(value, array_type) if array_type else value
    if origin is list:
        value = _unwrap_item_array(value)
        if isinstance(value, list):
            result = []
            def append_item(item, depth=0):
                if depth > 8:
                    raise ValueError("Provider array wrapper nesting exceeds 8")
                if isinstance(item, dict) and set(item) == {"item"}:
                    wrapped_item = _unwrap_item_array(item)
                    if isinstance(wrapped_item, list):
                        if len(wrapped_item) != 1:
                            raise ValueError("Each array item wrapper must contain exactly one item")
                        for child in wrapped_item:
                            append_item(child, depth + 1)
                        return
                result.append(_normalize_wire_arrays(item, args[0]))
            for item in value:
                append_item(item)
            return result
    return value


def _reject_boolean_integer(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("Boolean values are not integers")
    return value


def _reject_integer_booleans(value: Any, annotation: Any) -> None:
    """Keep native integer policy when Pydantic parses strings losslessly."""
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is int:
        _reject_boolean_integer(value)
    elif origin is Annotated:
        _reject_integer_booleans(value, args[0])
    elif origin in (Union, UnionType) and bool not in args:
        for variant in args:
            _reject_integer_booleans(value, variant)
    elif origin is list and isinstance(value, list):
        for item in value:
            _reject_integer_booleans(item, args[0])
    elif origin is dict and isinstance(value, dict):
        for item in value.values():
            _reject_integer_booleans(item, args[1])


ChestCoordinate = Annotated[int, BeforeValidator(_reject_boolean_integer), Field(ge=0)]
ItemCount = Annotated[int, BeforeValidator(_reject_boolean_integer), Field(ge=1)]


class SpatialRegion(BaseModel):
    """An absolute native map rectangle for bounded observation."""

    model_config = ConfigDict(extra="forbid")
    x: int = Field(ge=0, strict=True)
    y: int = Field(ge=0, strict=True)
    width: int = Field(ge=1, strict=True)
    height: int = Field(ge=1, strict=True)


class TileXY(BaseModel):
    """One non-negative native map coordinate for tile-array tool parameters.

    Keeps the wire contract of the former ``list[dict[str, Any]]`` parameters but
    validates every entry: x/y must be integers (booleans and non-numeric strings
    are rejected), numerically coercible wire values (e.g. mcode's ``"65"``) stay
    accepted, and coordinates must be non-negative like _TILE_SHAPE declares.
    """

    x: Annotated[int, BeforeValidator(_reject_boolean_integer)] = Field(ge=0)
    y: Annotated[int, BeforeValidator(_reject_boolean_integer)] = Field(ge=0)


class InteractionTileXY(BaseModel):
    """Adjacent native interaction coordinate, including one tile beyond map edges."""
    x: Annotated[int, BeforeValidator(_reject_boolean_integer)] = Field(ge=-1)
    y: Annotated[int, BeforeValidator(_reject_boolean_integer)] = Field(ge=-1)


class ItemQuantity(BaseModel):
    """Canonical native item quantity; strings are decoded by the wire adapter.

    Boolean, fractional, negative and missing counts remain invalid.
    """
    model_config = ConfigDict(extra="forbid")
    itemId: str = Field(min_length=1)
    count: Annotated[int, BeforeValidator(_reject_boolean_integer)] = Field(ge=1)
    quality: Annotated[int, BeforeValidator(_reject_boolean_integer)] | None = Field(default=None, ge=0)


def _item_dicts(items):
    return [item.model_dump(exclude_none=True) if isinstance(item, BaseModel) else item
            for item in items] if items is not None else None


def _tile_dicts(tiles: Any) -> list[dict[str, Any]]:
    """Normalize TileXY models (or plain coordinate dicts) for the scheduler API.

    FastMCP keeps validated sub-models as model instances; the guarded plan path
    already reduced them to plain dicts. Both shapes pass through unchanged in
    content so the scheduler interface stays dict-based.
    """
    return [tile.model_dump() if isinstance(tile, BaseModel) else tile for tile in (tiles or [])]


class ShortJobStep(BaseModel):
    """One native operation within a bounded semantic job."""

    model_config = ConfigDict(extra="forbid")
    operation: str = Field(description="Discoverable plan operation, e.g. water_auto or harvest_auto")
    params: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None
    wait: dict[str, Any] | None = Field(default=None, description="Must be absent; future waits are intent")

    @model_validator(mode="before")
    @classmethod
    def normalize_bounded_count(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("params"), dict):
            value = {**value, "params": dict(value["params"])}
            params = value["params"]
            if "maxTiles" in params:
                if "max_tiles" in params and params["max_tiles"] != params["maxTiles"]:
                    raise ValueError("Conflicting maxTiles/max_tiles; supply one max_tiles value")
                params["max_tiles"] = params.pop("maxTiles")
        return value


class ShortJobTask(BaseModel):
    """Exactly one current job, with 1..32 bounded native steps."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    steps: Annotated[list[ShortJobStep], BeforeValidator(_unwrap_item_array)] = Field(min_length=1, max_length=32)
    id: str | None = None
    completionCondition: str = ""
    dependencies: list[str] = Field(default_factory=list, max_length=0, description="Must be empty; select subsequent business next decision")

    @model_validator(mode="before")
    @classmethod
    def normalize_single_operation(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for alias in ("label", "reason"):
            if alias in value:
                if "title" in value and value["title"] != value[alias]:
                    raise ValueError(f"Conflicting title/{alias}; supply one title")
                value["title"] = value.pop(alias)
        if "operation" in value or "type" in value:
            if "steps" in value:
                raise ValueError("Ambiguous task: supply steps only, not a second top-level operation/type")
            operation = value.pop("operation", None)
            alias = value.pop("type", None)
            if operation is not None and alias is not None and operation != alias:
                raise ValueError("Conflicting operation/type; supply one operation")
            value["steps"] = [{"operation": operation or alias, "params": value.pop("params", {})}]
        return value


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


_TILE_SHAPE = {"type": "object", "required": ["x", "y"],
               "properties": {"x": {"type": "integer", "minimum": 0},
                              "y": {"type": "integer", "minimum": 0}}}
_ITEM_COUNT_SHAPE = {"type": "object", "required": ["itemId", "count"],
                     "properties": {"itemId": {"type": "string", "minLength": 1},
                                    "count": {"type": "integer", "minimum": 1}}}
_EXECUTION_SCOPE_SCHEMA = {
    "anyOf": [
        {"type": "null"},
        {"type": "object", "maxProperties": 0},
        {"type": "object", "required": ["locationId", "tiles"], "properties": {
            "locationId": {"type": "string", "minLength": 1},
            "tiles": {"type": "array", "items": {"anyOf": [
                _TILE_SHAPE,
                {"type": "array", "minItems": 2, "maxItems": 2,
                 "items": {"type": "integer", "minimum": 0}},
            ]}},
        }},
    ],
    "examples": [{"locationId": "Farm", "tiles": [{"x": 69, "y": 18}, {"x": 64, "y": 23}]}, {}],
}
_CAPABILITY_EXAMPLES = {
    "navigate_to": {"location_id": "Farm", "tile": {"x": 55, "y": 17}},
    "withdraw_from_chest": {"chest_x": 55, "chest_y": 17,
                            "items": [{"itemId": "(O)475", "count": 20}]},
    "deposit_to_chest": {"chest_x": 55, "chest_y": 17, "item_ids": ["(O)475"]},
    "hoe_tiles": {"tiles": [{"x": 69, "y": 18}, {"x": 64, "y": 23}]},
    "plant_seeds": {"seed_item_id": "(O)475", "tiles": [{"x": 69, "y": 18}, {"x": 64, "y": 23}]},
    "water_tiles": {"tiles": [{"x": 69, "y": 18}, {"x": 64, "y": 23}], "location_id": "Farm"},
    "water_zone": {"center_x": 69, "center_y": 18, "radius": 0},
    "water_auto": {"max_tiles": 100},
    "harvest_auto": {"max_tiles": 100},
    "plant_crop_workflow": {"seed_item_id": "(O)475", "count": 20},
    "purchase_items": {"items": [{"itemId": "(O)475", "count": 20}], "budget_limit": 1000},
    "ship_items": {"items": [{"itemId": "(O)24", "count": 10}]},
    "query_planting_options": {"location_id": "Farm", "region": {"x": 60, "y": 15, "width": 20, "height": 20}},
    "manage_goal": {"action": "create", "text": "种完已交付的土豆种子"},
    "manage_todo": {"action": "create", "intent": "体力恢复后接续播种", "trigger": {"type": "resource", "resource": "stamina", "minAmount": 100}},
    "manage_plan": {"goal_id": "observed-active-goal-id", "tasks": [{"title": "浇水", "steps": [{"operation": "water_auto", "params": {"max_tiles": 100}}]}]},
    "read_guidance": {"topic": "execution"},
    "query_wiki": {"query": "Potato"},
    "pet_animal": {"animal_id": "observed-animal-id", "location_id": "Farm"},
    "collect_animal_produce": {"animal_id": "observed-animal-id", "location_id": "Farm"},
    "toggle_animal_door": {"building_name": "observed-building-name", "location_id": "Farm"},
}


def capability_parameter_shape(key: str, value: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local references so discovery carries usable nested shapes."""
    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(part) for part in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return resolve({**schema.get("$defs", {}).get(name, {}),
                            **{k: v for k, v in node.items() if k != "$ref"}})
        return {k: resolve(v) for k, v in node.items() if k not in {"title", "$defs"}}

    shape = resolve(value)
    nullable = any(part.get("type") == "null" for part in shape.get("anyOf", []))
    nested = None
    if key in {"tiles", "target_tiles"}:
        nested = {"type": "array", "minItems": 0 if key == "target_tiles" else 1, "items": _TILE_SHAPE}
    elif key in {"tile", "chest_tile"}:
        nested = _TILE_SHAPE
    elif key == "items":
        nested = {"type": "array", "minItems": 1, "items": _ITEM_COUNT_SHAPE}
    if nested is not None:
        shape = {"anyOf": [nested, {"type": "null"}]} if nullable else nested
        if isinstance(value, dict):
            shape = {**shape, **{key: value[key] for key in ("default", "description") if key in value}}
    return shape


def capability_example(name: str, schema: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    """Examples are parameter objects, not claims about the current world."""
    if name in _CAPABILITY_EXAMPLES:
        return _CAPABILITY_EXAMPLES[name]

    def sample(shape: dict[str, Any]) -> Any:
        if "default" in shape and shape["default"] is not None:
            return shape["default"]
        if "enum" in shape:
            return shape["enum"][0]
        if "anyOf" in shape:
            return sample(next((v for v in shape["anyOf"] if v.get("type") != "null"), {}))
        if shape.get("type") == "object":
            fields = shape.get("properties", {})
            return {k: sample(fields[k]) for k in shape.get("required", [])}
        if shape.get("type") == "array":
            return [sample(shape.get("items", {}))]
        if shape.get("type") == "boolean":
            return False
        if shape.get("type") in {"integer", "number"}:
            return shape.get("minimum", 1)
        return "observed-id"

    return {key: sample(properties[key]) for key in schema.get("required", [])}


DEFAULT_MCP_INSTRUCTIONS = """
Use the supplied companion core instructions. query_game observes the game;
submit_work executes one action immediately and returns its native result.
Observe remaining work before choosing the next action; do not create a queue.
A timeout is not proof of failure: check current activity and world state, then
retry only remaining work once the previous action has stopped.
Use consult for packaged guidance and wiki knowledge. Keep player agreements
and long-term goals, not duplicate inventories or execution plans.
water_auto selects observed dry crops; max_tiles limits one batch, not the goal.
For a restricted area or nearest N crops, pass an observed target_tiles subset.
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

    response = {
        "skill": skill,
        "outcome": outcome,
        "goalSatisfied": outcome == "completed",
        "terminalState": terminal,
        "status": status,
        "taskId": res.get("taskId"),
        "effects": effects,
        "completedCount": completed_count,
        "completedCountUnit": "action-targets",
        "completionScope": "This submitted action; material delivery and the player's wider goal are separate.",
        "skippedCount": res.get("skippedCount", 0),
        "failedCount": res.get("failedCount", 0),
        "remaining": details.get("skipped") if outcome == "partial" else None,
        "reasonCode": reason,
        "reasonMessage": error.get("message"),
        "playerActionRequired": bool(res.get("playerActionRequired")),
        "snapshotRevision": res.get("worldRevision"),
        "details": {key: value for key, value in details.items() if key not in {"completed", "skipped", "failed"}},
    }
    for key in ("commandId", "resources", "inventoryDelta", "resourceDelta"):
        if res.get(key) is not None:
            response[key] = res[key]
    if skill in {"clear-debris", "chop-tree", "cut-grass"}:
        response["quantityMeaning"] = (
            "completedCount counts processed targets. effects itemId/stack identify the affected objects, "
            "not materials obtained. details.inventoryDelta, when provided, is the measured command-interval backpack change; "
            "afterWork shows current inventory and collectible drops. Neither proves who caused every change.")
    return response


# Plan steps may only invoke these existing operations, through their real
# scheduler implementation. Unknown operations are rejected; unknown parameters
# reject semantic parameters outside the operation contract before dispatch.
_PLAN_OPERATION_CALLS: dict[str, tuple[str, frozenset[str]]] = {
    "learned_action": ("learned_action", frozenset({"capability", "version", "arguments"})),
    "observe_player_activities": ("observe_player_activities", frozenset()),
    "talk_npc": ("talk_npc", frozenset({"npc_name", "location_id"})),
    "interact_tile": ("interact_tile", frozenset({"tile", "location_id"})),
    "fish": ("fish", frozenset({"facing", "location_id"})),
    "attack": ("attack", frozenset({"facing", "location_id"})),
    "native_menu_select": ("native_menu_select", frozenset({"choice_index", "location_id", "text"})),
    "get_work_overview": ("get_work_overview", frozenset({"detail"})),
    "get_status": ("get_status", frozenset({"detail"})),
    "query_farm_work": ("query_farm_work", frozenset()),
    "query_inventory": ("query_inventory", frozenset()),
    "query_chests": ("query_chests", frozenset()),
    "query_planting_options": ("query_planting_options", frozenset({"detail", "location_id", "region"})),
    "query_route": ("query_route", frozenset({"location_id", "tile"})),
    "query_shop": ("query_shop", frozenset({"shop_id", "detail", "item_id", "name", "is_seed"})),
    "water_tiles": ("execute_tiles", frozenset({"tiles", "location_id", "include_empty_tiles"})),
    "water_zone": ("execute_water_zone", frozenset({"center_x", "center_y", "radius", "include_empty_tiles"})),
    "water_auto": ("water_auto", frozenset({"max_tiles", "include_empty_tiles", "target_tiles", "location_id"})),
    "harvest_auto": ("harvest_auto", frozenset({"max_tiles", "target_tiles", "location_id"})),
    "deposit_to_chest": ("deposit_to_chest", frozenset({"chest_x", "chest_y", "item_ids", "items", "location_id"})),
    "withdraw_from_chest": (
        "withdraw_from_chest",
        frozenset({"chest_x", "chest_y", "items", "item_id", "count", "location_id"}),
    ),
    "organize_chest": ("organize_chest", frozenset({"chest_x", "chest_y"})),
    "hoe_tiles": ("execute_hoe_tiles", frozenset({"tiles", "location_id"})),
    "plant_seeds": ("execute_plant_seeds", frozenset({"seed_item_id", "tiles", "location_id"})),
    "ship_items": ("execute_ship_items", frozenset({"items", "location_id", "max_game_minutes"})),
    "purchase_items": (
        "execute_purchase_items",
        frozenset({"items", "budget_limit", "shop_id", "location_id"}),
    ),
    "navigate_to": ("execute_navigate_to", frozenset({"location_id", "tile", "landmark"})),
    "pause_task": ("pause_task", frozenset()),
    "sleep": ("execute_sleep", frozenset()),
    "open_shop": ("open_shop", frozenset({"shop_id", "location_id"})),
    "open_chest": ("open_chest", frozenset({"chest_x", "chest_y", "location_id"})),
    "native_advance": ("execute_native_advance", frozenset({"allow_skip"})),
    "native_answer": ("execute_native_answer", frozenset({"choice_index"})),
    "resume_task": ("resume_task", frozenset()),
    "cancel_task": ("cancel_task", frozenset({"reason"})),
    # Grouped observation + native agricultural/husbandry actions. Every entry runs
    # the same real scheduler method as the corresponding MCP tool.
    "observe_farming_helpers": ("query_farming_helpers", frozenset({"location_id"})),
    "observe_crafting": ("query_crafting", frozenset({"location_id"})),
    "craft_items": ("craft_items", frozenset({"recipe_name", "item_count", "location_id"})),
    "move_building": ("move_building", frozenset({"building_name", "tile", "location_id"})),
    "observe_farm_space": ("query_farm_space", frozenset({"location_id", "region"})),
    "place_items": ("place_items", frozenset({"tiles", "item_id", "location_id"})),
    "remove_items": ("remove_items", frozenset({"tiles", "item_id", "location_id"})),
    "observe_machines": ("query_machines", frozenset({"location_id"})),
    "observe_production": ("query_production", frozenset({"location_id"})),
    "observe_building_services": ("query_building_services", frozenset({"location_id"})),
    "eat_food": ("eat_food", frozenset({"item_id", "location_id"})),
    "build_building": ("build_building", frozenset({"building_type", "tile", "budget_limit", "location_id"})),
    "upgrade_building": ("upgrade_building", frozenset({"building_name", "building_type", "budget_limit", "location_id"})),
    "purchase_animal": ("purchase_animal", frozenset({"building_name", "animal_type", "animal_name", "budget_limit", "location_id"})),
    "observe_livestock": ("query_livestock", frozenset({"location_id"})),
    "refill_watering_can": ("refill_watering_can", frozenset({"tiles", "location_id", "max_tiles"})),
    "apply_fertilizer": (
        "apply_fertilizer",
        frozenset({"tiles", "fertilizer_item_id", "location_id"}),
    ),
    "clear_debris": ("clear_debris", frozenset({"tiles", "location_id"})),
    "cut_grass": ("cut_grass", frozenset({"tiles", "location_id"})),
    "pickup_items": ("pickup_items", frozenset({"tiles", "location_id"})),
    "chop_tree": ("chop_tree", frozenset({"tiles", "location_id"})),
    "rest": ("rest", frozenset({"target_stamina"})),
    "insert_machine": ("insert_machine", frozenset({"tile", "item_id", "item_count", "location_id"})),
    "collect_machine": ("collect_machine", frozenset({"tiles", "location_id"})),
    "pet_animal": ("pet_animal", frozenset({"animal_name", "animal_id", "tile", "location_id"})),
    "collect_animal_produce": (
        "collect_animal_produce",
        frozenset({"animal_name", "animal_id", "tile", "location_id"}),
    ),
    "feed_animals": ("feed_animals", frozenset({"building_name"})),
    "toggle_animal_door": (
        "toggle_animal_door",
        frozenset({"tiles", "building_name", "location_id"}),
    ),
}


# Compatibility tool groups used by full/internal callers. The game surface is
# the fixed eight GAME_TOOLS, registered over these canonical implementations.
# full remains the generic default; internal serves the optional legacy worker.
# light and the legacy life alias share live tools and execution-time admission.
# An explicit environment surface overrides the CLI, except internal workers.
BASE_TOOLS = frozenset(
    {
        # overview / status
        "get_work_overview",
        "read_reload_history",
        "get_status",
        # grouped observation (on-demand: never mixes backpack/chests into a farm tool)
        "observe_farming_helpers",
        "observe_production",
        "observe_map_image",
        "observe_crafting",
        "observe_farm_space",
        "observe_machines",
        "observe_livestock",
        # common actions exposed directly
        "plant_seeds",
        "eat_food",
        "water_auto",
        "harvest_auto",
        "navigate_to",
        "refill_watering_can",
        # write entry points
        "submit_plan",
        "request_player_decision",
        "remember_intent",
        # player controls
        "autonomy_status",
        "set_autonomy",
        "pause_task",
        "resume_task",
        "cancel_task",
        "sleep",
        "player_control",
        "open_shop",
        # grouped discovery + invocation
        "discover_capabilities",
        "call_capability",
    }
)
LEGACY_GAME_TOOLS = BASE_TOOLS | {"work_plan_overview", "query_wiki", "read_guidance",
                            "manage_milestones", "manage_companion"}

LIGHT_TOOLS = GAME_TOOLS

# Harness-only additions on top of the game surface (never shown to the model).
# ``dispatch_plan_operation`` is the worker's write path: it runs one validated
# plan operation through ``execute_plan_operation`` so the persisted stable
# command id actually reaches the real scheduler/native command (``call_capability``
# rejects the harness-only ``command_id`` parameter, which is exactly the round6
# STEP_DISPATCH_FAILED root cause).
INTERNAL_TOOLS = LEGACY_GAME_TOOLS | {"reconcile_plan_command", "dispatch_plan_operation", "cancel_plan_command"}

# Same declarations on every event; admission controls native access.
LIFE_TOOLS = LIGHT_TOOLS
# Base tools grouped for on-demand disclosure.
CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "farm": (
        "query_farm_work",
        "query_planting_options",
        "water_zone",
        "water_tiles",
        "water_auto",
        "harvest_auto",
        "hoe_tiles",
        "plant_seeds",
    ),
    "farming_helpers": (
        "observe_production",
        "eat_food",
        "observe_farming_helpers",
        "refill_watering_can",
        "apply_fertilizer",
        "clear_debris",
        "cut_grass",
        "pickup_items",
    ),
    "farm_space": ("observe_map_image", "observe_farm_space", "place_items", "remove_items", "move_building"),
    "crafting": ("observe_crafting", "craft_items"),
    "building_services": ("observe_building_services", "build_building", "upgrade_building", "purchase_animal"),
    "forestry": ("chop_tree",),
    "machines": ("observe_machines", "insert_machine", "collect_machine"),
    "livestock": (
        "observe_livestock",
        "pet_animal",
        "collect_animal_produce",
        "feed_animals",
        "toggle_animal_door",
    ),
    "shopping": ("open_shop", "query_shop", "purchase_items", "ship_items"),
    "chest": (
        "open_chest",
        "query_inventory",
        "query_chests",
        "deposit_to_chest",
        "withdraw_from_chest",
        "organize_chest",
    ),
    "movement": ("get_status", "query_route", "navigate_to"),
    "native_ui": ("get_status", "observe_player_activities", "talk_npc", "interact_tile", "fish", "attack", "native_menu_select", "native_advance", "native_answer"),
    "knowledge": ("read_guidance", "query_wiki"),
    "learning": ("inspect_capabilities", "develop_capability", "prepare_capability_builder", "manage_capability", "learned_query", "learned_action"),
    "memory": ("save_memory", "save_method", "recall_memory", "forget_memory", "manage_goal", "manage_plan", "manage_todo", "record_decision_note", "work_plan_overview"),
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
            "gameTime": {"type": "gameTime", "year": "integer", "season": "spring|summer|fall|winter",
                         "day": "integer 1..28", "timeOfDay": "native HHMM 600..2600"},
            "resource": {"type": "resource", "resource": "stamina|water", "minAmount": "positive number"},
            "machineReady": {"type": "machineReady", "locationId": "observed map ID",
                             "tile": {"x": "observed integer", "y": "observed integer"}},
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
        "purpose": "Select exactly ONE current semantic short job; subsequent business requires a new model decision.",
        "parameters": {
            "tasks": "array, required, exactly 1 task: {title, steps:[{operation, params}], id?, completionCondition?}. 1..32 sequential native operations, including navigation across maps and related supplies/preparation. No task dependencies or future waits; partial or uncertain native results stop the job.",
            "goal_id": "string | null. Existing active goal id from context; may be omitted only when this decision has one bound active goal, or goal_text is supplied.",
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
                    "steps": [{"operation": "water_auto", "params": {"max_tiles": 2, "target_tiles": [{"x": 62, "y": 27}, {"x": 66, "y": 23}]}}],
                }
            ],
        },
        "invalid_example": {
            "tasks": [{"id": "t1", "steps": [{"operation": "run_shell", "params": {}}]}]
        },
        "errors": "Unknown operations and step parameters are rejected before job selection. Building and animal services require location_id='Farm' for the target building; navigate separately to the service counter.",
    },
}


def resolve_surface(full: bool | None = None, surface: str | None = None) -> str:
    """Resolve the exposure surface: "full" (default), "light", "internal" or "life".

    Generic clients retain the full legacy list. In-game conversations and work
    use light; life remains a compatibility alias. The environment overrides the
    CLI profile, except internal workers always retain their reconciliation tools.
    """
    if full is True:
        return "full"
    env_candidate = (os.getenv("STARDEW_MCP_SURFACE") or "").strip().lower()
    cli_candidate = (surface or "").strip().lower()
    if cli_candidate in {"internal", "worker"}:
        # Harness-only surface: never downgradeable by an inherited env var.
        return "internal"
    candidate = env_candidate or cli_candidate
    if candidate in {"light", "game"}:
        return "light"
    if candidate in {"internal", "worker"}:
        return "internal"
    if candidate in {"life", "companion"}:
        return "life"
    if candidate in {"full", "legacy", "complete"}:
        return "full"
    if full is False:
        # Legacy explicit "full=False" callers asked for the game surface; an
        # explicitly set env var already had its chance to override above.
        return "light"
    legacy = os.getenv("STARDEW_MCP_FULL", "").strip().lower()
    if legacy in {"1", "true", "yes", "on"}:
        return "full"
    if legacy in {"0", "false", "no", "off"}:
        # Explicit legacy opt-out of the full list means the game surface.
        return "light"
    return "full"


class _McodeFastMCP(FastMCP):
    """Avoid mcode appending a second copy of an already structured result.

    Keep the structured result and output schema for MCP validation. Other clients
    retain FastMCP's backward-compatible JSON text; mixed media and errors survive.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        result = await super().call_tool(name, arguments)
        if not (isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict)):
            return result
        content, structured = result
        retained = []
        for block in content:
            if isinstance(block, TextContent) and block.annotations is None and block.meta is None:
                try:
                    if json.loads(block.text) == structured:
                        continue
                except ValueError:
                    pass
            retained.append(block)
        return retained, structured
