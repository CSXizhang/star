"""Compact real-time decision context injected into every model decision.

Each normal chat / free-mode decision carries a small live context instead of
making the model re-query the world:

* date / weather / location / stamina / funds;
* companion backpack item names + counts, free slots and tool resources;
* the relevant long-term goals / player agreements;
* the current task's next step, waiting conditions and anomalies.

Rules enforced here:

* Only fields the native SMAPI snapshot actually published are emitted; anything
  the game did not send is reported as the string ``unknown`` rather than guessed
  or defaulted to a misleading zero. In particular a missing inventory ``stack``
  is ``unknown`` (never 1), and ``isRaining=false`` is never rendered as "clear"
  (snow and storms are not rain) — the native weather name/icon is used instead.
* Field names follow the real Mod schema (``WorldSnapshotPayload`` in
  ``src/StardewAI.Companion.Mod/Transport/TransportDtos.cs``): world
  ``currentLocation/timeOfDay/season/dayOfMonth/year/weather/isRaining``,
  companion ``locationId/tileX/tileY/stamina/maxStamina/waterCanLevel/
  availableMoney``, inventory ``capacity/freeSlots/slots[itemId/name/stack/isTool]``.
* Only bounded actionable tile and seed-source observations are included, never
  the full farm, chest contents or shop inventory.
* The context is rebuilt from the latest snapshot every time; old snapshots are
  never appended (so it cannot grow without bound). Note this bounds the *payload
  formatter*; it does not erase the provider's own earlier prompt history, which
  is only bounded by provider-session rotation.
* Tool results are post-execution facts: once the runtime has a result it is the
  latest fact, and the model is not asked to re-query to confirm it.

This module is pure formatting: it never touches the network or the store.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from stardew_ai_runtime.decision_policy import project_context

UNKNOWN = "unknown"

# Native UI-state segment (frozen wire contract): published by the C# mod in
# the snapshot's ``native`` section; player-mode snapshots may omit the legacy
# restState/bedtime fields entirely.
_NATIVE_SEGMENT_KEYS = (
    "eventUp", "eventSkippable", "eventIsFestival", "eventAllowsMovement", "dialogueUp", "dialogueSpeaker", "dialogueText",
    "isQuestionDialogue", "questionResponses", "activeMenu", "playerCanMove",
    "isTimePaused", "endOfNight", "programHold", "clockPauseOwner", "playerActionSupport", "namingInput",
)


def native_segment(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Project the native UI-state segment; absent fields stay absent."""
    if not isinstance(payload, dict):
        return None
    world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
    raw = payload.get("native") or world.get("native")
    native = raw if isinstance(raw, dict) else {}
    segment: dict[str, Any] = {}
    for key in _NATIVE_SEGMENT_KEYS:
        value = native.get(key)
        if value is None and key in payload:
            value = payload.get(key)
        if value is not None:
            segment[key] = value
    return segment or None


def _value(mapping: Any, key: str, default: Any = UNKNOWN) -> Any:
    if isinstance(mapping, dict):
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _payload_of(snapshot: Any) -> dict[str, Any]:
    """Accept either a raw envelope payload or a ``{"payload": {...}}`` snapshot."""
    if not isinstance(snapshot, dict):
        return {}
    payload = snapshot.get("payload")
    if isinstance(payload, dict):
        return payload
    return snapshot


def _revision_of(snapshot: Any) -> Any:
    if isinstance(snapshot, dict):
        revision = snapshot.get("worldRevision")
        if isinstance(revision, int):
            return revision
    return UNKNOWN


def objective_scope(goal: Any) -> dict[str, Any] | None:
    """Authoritative objective material, excluding project notes and epochs.

    The bridge can use this unabridged projection for its wake fingerprint. Older
    milestone goals keep their objective in the work-item specification.
    """
    if not isinstance(goal, dict):
        return None
    constraints = goal.get("constraints")
    constraints = constraints if isinstance(constraints, dict) else {}
    scope = constraints.get("objectiveScope")
    if not isinstance(scope, dict) and isinstance(goal.get("scope"), dict):
        scope = goal["scope"]  # Already formatted work contexts retain their scope.
    if isinstance(scope, dict):
        if isinstance(scope.get("workItems"), list):
            return {"workItems": scope["workItems"]}
        return {**{key: scope.get(key) for key in
                   ("summary", "targetDate", "plannedCount", "termsNote", "preparation")},
                **({"executionScope": scope["executionScope"]} if isinstance(scope.get("executionScope"), dict) else {})}
    spec = constraints.get("milestoneSpec")
    if not isinstance(spec, dict) or not isinstance(spec.get("todos"), list):
        return None
    return {"workItems": [
        {key: row.get(key) for key in ("key", "intent", "trigger", "expiry")}
        for row in spec["todos"] if isinstance(row, dict)
    ]}


def goal_context(goal: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
    """Small model projection of a goal, with its explicit saved scope.

    ``compact=True`` renders the incremental-turn one-liner (text + current
    stage/next action only); the full scope/executionScope projection is only
    for first-turn/full-context prompts.
    """
    result = {**({"id": goal["id"]} if goal.get("id") else {}),
              "text": goal.get("text"), "source": goal.get("source")}
    if goal.get("kind"):
        result["kind"] = goal.get("kind")
    if compact:
        project = goal.get("project") if isinstance(goal.get("project"), dict) else {}
        for key in ("phase", "nextAction"):
            value = project.get(key)
            if isinstance(value, str) and value:
                result[key] = value[:300]
        progress_log = project.get("progressLog")
        if isinstance(progress_log, list) and progress_log:
            last = progress_log[-1]
            if isinstance(last, dict) and last.get("note"):
                result["lastProgress"] = str(last.get("note"))[:200]
        return result
    constraints = goal.get("constraints")
    if isinstance(constraints, dict) and constraints:
        visible = {key: value for key, value in constraints.items()
                   if key not in {"milestoneSpec", "objectiveScope"}}
        if visible:
            result["constraints"] = visible
    scope = objective_scope(goal)
    if scope is not None:
        previous_scope = goal.get("scope") if isinstance(goal.get("scope"), dict) else {}
        truncated = bool(previous_scope.get("truncated"))
        compact = dict(scope)
        for key, limit in (("summary", 1200), ("termsNote", 600)):
            value = compact.get(key)
            if isinstance(value, str) and len(value) > limit:
                compact[key] = value[:limit]
                truncated = True
        for key in ("preparation", "workItems"):
            rows = compact.get(key)
            if isinstance(rows, list):
                compact[key] = rows[:6]
                truncated |= len(rows) > 6
        if "workItems" in compact:
            compact["workItems"] = [dict(row) for row in compact["workItems"]]
            for row in compact["workItems"]:
                if isinstance(row.get("intent"), str) and len(row["intent"]) > 1200:
                    row["intent"] = row["intent"][:1200]
                    truncated = True
        execution = compact.get("executionScope")
        if isinstance(execution, dict):
            execution = dict(execution)
            rows = execution.get("tiles")
            if isinstance(rows, list):
                execution["tiles"] = [dict(row) for row in rows[:128]]
                execution["tileCount"] = execution.get("tileCount", len(rows))
                execution["truncated"] = bool(execution.get("truncated")) or len(rows) > 128
                truncated |= execution["truncated"]
                if execution["truncated"] and isinstance(constraints, dict) and constraints.get("milestoneId"):
                    execution["detailQuery"] = {"tool": "manage_milestones", "params": {
                        "action": "list", "node_id": constraints["milestoneId"]}}
            compact["executionScope"] = execution
        result["scope"] = {**compact, "revision": goal.get("epoch", previous_scope.get("revision", UNKNOWN)),
                           "truncated": truncated}
    if goal.get("project"):
        result["project"] = project_context(goal["project"])
    return result


def _tiles(rows: Any, limit: int = 16) -> list[dict[str, int]]:
    return [{"x": row["x"], "y": row["y"]} for row in rows
            if isinstance(row, dict) and all(isinstance(row.get(key), int)
               and not isinstance(row[key], bool) for key in ("x", "y"))][:limit] if isinstance(rows, list) else []


def _truncated(rows: Any, native_flag: Any, limit: int) -> Any:
    if not isinstance(rows, list):
        return UNKNOWN
    if len(rows) > limit or native_flag is True:
        return True
    return False if native_flag is False else UNKNOWN


def _seed(row: dict[str, Any]) -> dict[str, Any]:
    result = {"itemId": _value(row, "itemId"), "name": _value(row, "name"),
              "count": _value(row, "stack")}
    for key in ("canPlantCurrentSeason", "seasons", "growthDays", "regrows", "isRaised"):
        if key in row:
            result[key] = row[key]
    return result


def _action_facts(payload: dict[str, Any], location: Any, revision: Any,
                  farm_work: dict[str, Any], farm_observed: bool,
                  work: dict[str, Any]) -> dict[str, Any]:
    """Project current native observations; absence never means empty stock."""
    planting = payload.get("planting")
    planting = planting if isinstance(planting, dict) else {}
    candidates = planting.get("candidateTiles")
    candidates = candidates if isinstance(candidates, dict) else {}
    facts: dict[str, Any] = {"worldRevision": revision}
    if planting:
        facts["planting"] = {
            "locationId": planting.get("locationId") or location, "observationStatus": planting.get("observationStatus","observed"),
            "scope": planting.get("scope","search-region"),
            "searchBounds": planting.get("searchBounds", UNKNOWN),
            "tiles": {key: {
                "count": _value(candidates, key + "Count"),
                "coordinates": ("on-demand: compare Farm regions, then query chosen bounds" if key == "tillable" and payload.get("actorMode") == "player" else _tiles(candidates.get(key + "Tiles"))),
                "truncated": _truncated(candidates.get(key + "Tiles"), candidates.get(key + "Truncated"), 16),
            } for key in ("tilledEmpty", "tillable")},
        }
    else:
        facts["planting"] = {"locationId": location, "observationStatus": "unknown",
                             "tiles": UNKNOWN}
    if payload.get("actorMode") == "player":
        facts["planting"]["layoutContract"] = "Routine tillable counts are local samples, not field recommendations. All Farm is queryable: global query_planting_options catalog, then requested bounds gives exact complete tiles; compare home/water paths and keep access. Existing tilledEmpty remains an observed asset, not a reason to scatter new fields."
    preparation = set()
    for goal in work.get("goals", []) if isinstance(work.get("goals"), list) else []:
        scope = objective_scope(goal) or {}
        keys = scope.get("preparation")
        if isinstance(keys, list):
            preparation.update(key for key in keys if isinstance(key, str))
        preparation.update(row.get("key") for row in scope.get("workItems", []) if isinstance(row, dict))
    operation = (work.get("nextStep") or {}).get("operation") if isinstance(work.get("nextStep"), dict) else None
    related = []
    if preparation & {"water", "plant", "plant-after", "production"} or operation in {"water_auto", "water_zone", "water_tiles"}:
        related.append(("unwatered", "cropUnwateredTiles", "cropUnwateredCount", "cropUnwateredTruncated"))
    if preparation & {"harvest", "production"} or operation == "harvest_auto":
        related.append(("harvestable", "matureCrops", "matureCropCount", "matureCropsTruncated"))
    if related:
        facts["farm"] = {"locationId": farm_work.get("locationId", "Farm"),
                         "observationStatus": "observed" if farm_observed else "not-observed",
                         "currentCompanionLocationId": location,
                         "needsNavigation": location != farm_work.get("locationId", "Farm") if location != UNKNOWN else UNKNOWN}
        for label, tiles_key, count_key, truncated_key in related:
            rows = farm_work.get(tiles_key) if farm_observed else None
            facts["farm"][label] = {"coordinates": _tiles(rows) if isinstance(rows, list) else UNKNOWN,
                                   "count": _value(farm_work, count_key) if farm_observed else UNKNOWN,
                                   "truncated": _truncated(rows, farm_work.get(truncated_key), 16)}

    seeds = planting.get("seeds")
    known_seeds = [row for row in seeds if isinstance(row, dict)] if isinstance(seeds, list) else []
    seed_ids = {row.get("itemId") for row in known_seeds if row.get("itemId")}
    sources: dict[str, Any] = {"inventory": {
        "observationStatus": "observed" if isinstance(seeds, list) else "unknown",
        "seeds": [_seed(row) for row in known_seeds[:16]] if isinstance(seeds, list) else UNKNOWN,
        "truncated": len(known_seeds) > 16 if isinstance(seeds, list) else UNKNOWN,
    }}
    chests = payload.get("chests")
    chests = chests if isinstance(chests, dict) else {}
    rows = chests.get("items")
    observed = isinstance(rows, list) and chests.get("observationStatus") not in {"not-observed", "unknown", "unavailable"}
    chest_sources = []
    uncertain = False
    total = 0
    for chest in rows if observed else []:
        if not isinstance(chest, dict) or not isinstance(chest.get("contents"), list):
            uncertain = True
            continue
        native_seeds = []
        for item in chest["contents"]:
            if not isinstance(item, dict):
                continue
            if item.get("isSeed") is True or item.get("itemId") in seed_ids:
                native_seeds.append(item)
            elif "isSeed" not in item:
                uncertain = True
        if native_seeds:
            total += len(native_seeds)
            chest_sources.append({"tile": chest.get("tile", UNKNOWN),
                                  "seeds": [_seed(row) for row in native_seeds[:16]],
                                  "truncated": len(native_seeds) > 16})
    # Native legacy chest snapshots scan Farm; explicit scope takes precedence.
    sources["chests"] = {"locationId": chests.get("locationId", "Farm"),
                         "observationStatus": "observed" if observed else "unknown",
                         "sources": chest_sources[:8] if observed else UNKNOWN,
                         "truncated": True if observed and (total > 16 or len(chest_sources) > 8)
                                      else _truncated(rows, chests.get("truncated"), len(rows)) if observed else UNKNOWN,
                         "queryNeeded": not observed or chests.get("truncated") is not False or uncertain or total > 16 or len(chest_sources) > 8}
    # Bound seed rows across all chest sources, not just within each chest.
    remaining = 16
    for chest in sources["chests"]["sources"] if observed else []:
        chest["truncated"] |= len(chest["seeds"]) > remaining
        chest["seeds"] = chest["seeds"][:remaining]
        remaining -= len(chest["seeds"])
    if observed:
        sources["chests"]["sources"] = [row for row in sources["chests"]["sources"] if row["seeds"]]
    facts["seedSources"] = sources
    return facts


def crop_water_facts(farm_work: dict[str, Any]) -> dict[str, Any]:
    """Explain native crop sets without interpreting omitted or truncated tiles."""
    dry = farm_work.get("cropUnwateredTiles")
    mature = farm_work.get("matureCrops")
    complete = (isinstance(dry, list) and isinstance(mature, list)
                and farm_work.get("cropUnwateredTruncated") is False
                and farm_work.get("matureCropsTruncated") is False
                and farm_work.get("cropUnwateredCount") == len(dry)
                and farm_work.get("matureCropCount") == len(mature))
    result: dict[str, Any] = {
        "meaning": "cropUnwateredTiles contains crops, never empty soil; tilledUnwateredCount includes BOTH occupied crop soil and empty soil, not empty soil alone; matureCrops contains harvestable crops only. unwateredNonHarvestableCount is a SUBSET of cropUnwateredCount, never an additional count: mature unwatered crops plus non-harvestable unwatered crops partition cropUnwateredCount. Neither list inventories all immature crops.",
        "harvestMethod": "matureCrops.requiresScythe is observed from the native crop: false means hand-pickable, true requires a scythe; absent/null is unknown. Use this fact to select hand-only targets without a wiki lookup. Scythe swings can affect neighboring crops.",
        "unwateredEmptySoilCount": UNKNOWN,
        "unwateredNonHarvestableCount": UNKNOWN,
        "unwateredNonHarvestableTiles": UNKNOWN,
        "complete": complete,
    }
    tilled_count = farm_work.get("tilledUnwateredCount")
    crop_count = farm_work.get("cropUnwateredCount")
    if (type(tilled_count) is int and type(crop_count) is int
            and 0 <= crop_count <= tilled_count):
        result["unwateredEmptySoilCount"] = tilled_count - crop_count
    if complete:
        harvestable = {(tile["x"], tile["y"]) for tile in mature}
        candidates = [{"x": tile["x"], "y": tile["y"]} for tile in dry
                      if (tile["x"], tile["y"]) not in harvestable]
        result.update(unwateredNonHarvestableCount=len(candidates),
                      unwateredNonHarvestableTiles=candidates[:16],
                      coordinatesTruncated=len(candidates) > 16)
    return result


def water_can_resources(companion: dict[str, Any]) -> dict[str, Any]:
    """One model-facing meaning for legacy native water quantity fields."""
    current = companion.get("waterCanLevel")
    capacity = companion.get("maxWaterCanLevel")
    # Publish observed fill state as a quantity relationship, independent of
    # watering-can upgrade grade. Missing or invalid observations stay unknown.
    known_current = type(current) is int and current >= 0
    known_capacity = type(capacity) is int and capacity > 0
    valid_pair = known_current and known_capacity and current <= capacity
    return {
        "currentWater": current,
        "capacity": capacity,
        "isFull": current == capacity if valid_pair else None,
        "isEmpty": current == 0 if known_current else None,
        "unit": "浇水次数",
        "upgradeLevel": None,
        "note": "currentWater 是壶内剩余水量，capacity 是装满后的容量；工具升级等级未被原生快照发布，不可从水量推断。",
    }


def build_decision_context(
    snapshot: Any,
    *,
    work: dict[str, Any] | None = None,
    origin: str = "chat",
    companion: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
    actor_mode: str | None = None,
    compact: bool = False,
) -> dict[str, Any]:
    """Build the compact, unknown-safe context block for one decision.

    Optional keyword-only args (contract §2):
      companion: profile dict with name/personality/playStyle/careFrequency
      memory:    render_for_context() output with agreements/preferences/recentEvents
      actor_mode: "player" relabels the actor as the sole native player (additive;
        absent/any other value keeps the companion rendering exactly)
      compact:   incremental-turn rendering — the main goal as a one-liner plus
        at most three active stage goals, the native UI-state segment, and no
        confirmedRequestTotals/history ledgers (ledgers stay available via the
        tools/read guidance instead of being re-sent in prompts).
    When omitted, the default behaviour is unchanged.
    """
    payload = _payload_of(snapshot)
    actor_mode = actor_mode or payload.get("actorMode")
    world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
    companion_snapshot = (
        payload.get("companion") if isinstance(payload.get("companion"), dict) else {}
    )
    inventory = payload.get("inventory") if isinstance(payload.get("inventory"), dict) else {}
    shop = payload.get("shop") if isinstance(payload.get("shop"), dict) else {}

    if not payload:
        res = {
            **({"lastResult": work["lastJob"]} if isinstance(work, dict) and work.get("lastJob") else {}),
            "origin": origin,
            "provenance": UNKNOWN,
            "date": UNKNOWN,
            "weather": UNKNOWN,
            "time": UNKNOWN,
            "location": UNKNOWN,
            "playerLocation": UNKNOWN,
            "stamina": UNKNOWN,
            "restState": UNKNOWN,
            "funds": UNKNOWN,
            "inventory": {"items": [], "freeSlots": UNKNOWN, "tools": []},
            "goals": [],
            "currentTask": {"nextStep": None, "waitingFor": [], "anomalies": []},
            "note": "native snapshot unavailable; fields unknown",
        }
        _attach_companion_memory(res, companion, memory)
        if isinstance(work, dict):
            if "plantingEvidence" in work:
                res["plantingEvidence"] = work["plantingEvidence"]
            if "paused" in work:
                res["paused"] = bool(work.get("paused"))
                res["currentTask"]["paused"] = bool(work.get("paused"))
            if "decision" in work:
                res["decision"] = dict(work.get("decision") or {})
                res["currentTask"]["decision"] = dict(work.get("decision") or {})
        return res

    date_fields = {
        "year": _value(world, "year"),
        "season": _value(world, "season"),
        "day": _value(world, "dayOfMonth"),
    }
    date: Any = date_fields
    if all(v == UNKNOWN for v in date_fields.values()):
        date = UNKNOWN

    # Weather: report the actual native weather icon when the game published it.
    # ``isRaining == false`` does NOT mean "clear" (snow/storm/debris are not
    # rain), so it is only surfaced as a raw known flag.
    weather_icon = world.get("weatherIcon")
    is_raining = world.get("isRaining")
    if weather_icon is not None:
        weather: Any = {"icon": weather_icon}
        if isinstance(is_raining, bool):
            weather["isRaining"] = is_raining
    elif isinstance(is_raining, bool):
        weather = {"isRaining": is_raining, "note": "native weather unavailable"}
    else:
        weather = UNKNOWN

    stamina = _value(companion_snapshot, "stamina")
    stamina_block: Any = stamina
    if stamina != UNKNOWN:
        stamina_block = {"current": stamina, "max": _value(companion_snapshot, "maxStamina")}

    location = _value(companion_snapshot, "locationId")
    if location == UNKNOWN:
        location = _value(world, "currentLocation")
    location_block: Any = location
    tile_x = companion_snapshot.get("tileX")
    tile_y = companion_snapshot.get("tileY")
    if location != UNKNOWN or tile_x is not None or tile_y is not None:
        location_block = {
            "name": location,
            "tileX": tile_x if tile_x is not None else UNKNOWN,
            "tileY": tile_y if tile_y is not None else UNKNOWN,
        }
    player_location = _value(world, "currentLocation")

    farm_work = payload.get("farmWork") or world.get("farmWork") or {}
    farm_work = farm_work if isinstance(farm_work, dict) else {}
    farm_scope = farm_work.get("locationId")
    farm_observed = farm_work.get("observationStatus") in {None,"observed"} and (
        farm_scope == "Farm" or (farm_scope is None and companion_snapshot.get("locationId") == "Farm"))

    # Funds: the companion wallet published in the native snapshot. The older
    # shop-section reading is only a fallback for pre-upgrade payloads.
    money = companion_snapshot.get("availableMoney")
    money_status = companion_snapshot.get("moneyStatus")
    if money is None:
        money = shop.get("availableMoney")
        money_status = shop.get("moneyStatus") if money_status is None else money_status
    funds: Any = UNKNOWN if money is None else money

    slots = inventory.get("slots") if isinstance(inventory.get("slots"), list) else []
    items: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        name = slot.get("name") or slot.get("itemId") or UNKNOWN
        # Never default a missing stack to 1: an absent count is unknown, not one.
        raw_count = slot.get("stack")
        if not isinstance(raw_count, int):
            raw_count = slot.get("count")
        count: Any = raw_count if isinstance(raw_count, int) else UNKNOWN
        identity = ({"itemId": slot["itemId"]}
                    if isinstance(slot.get("itemId"), str) and slot["itemId"] else {})
        if slot.get("isTool"):
            tools.append({**identity, "name": name})
        else:
            quality = slot.get("quality")
            quality = quality if isinstance(quality, int) and not isinstance(quality, bool) else UNKNOWN
            items.append({**identity, "name": name, "count": count, "quality": quality})

    inventory_block: dict[str, Any] = {
        "items": items[:24],
        "freeSlots": _value(inventory, "freeSlots"),
        "capacity": _value(inventory, "capacity"),
        "tools": tools,
    }
    if len(items) > 24:
        inventory_block["itemsTruncated"] = True

    water_can = companion_snapshot.get("waterCanLevel")
    tool_resources: dict[str, Any] = {}
    if water_can is not None:
        tool_resources["waterCan"] = water_can_resources(companion_snapshot)
    inventory_block["toolResources"] = tool_resources

    work = work if isinstance(work, dict) else {}
    overview_goals = work.get("goals") if isinstance(work.get("goals"), list) else []
    if compact:
        mains = [g for g in overview_goals
                 if isinstance(g, dict) and g.get("text") and g.get("kind", "main") == "main"]
        stages = [g for g in overview_goals
                  if isinstance(g, dict) and g.get("text") and g.get("kind", "main") != "main"]
        goals = ([goal_context(g, compact=True) for g in mains[:1]]
                 + [goal_context(g, compact=True) for g in stages[:3]])
    else:
        goals = [goal_context(g)
            for g in overview_goals
            if isinstance(g, dict) and g.get("text")
        ][:3]

    tasks = work.get("tasks") if isinstance(work.get("tasks"), list) else []
    waiting = [
        {"taskId": t.get("id"), "title": t.get("title")}
        for t in tasks
        if isinstance(t, dict) and t.get("status") == "waiting"
    ][:3]
    conditions = work.get("waitingConditions") if isinstance(work.get("waitingConditions"), list) else []
    # Pair each waiting task with the explicit native condition that must be
    # observed before it can run again; the model sees *why* it is waiting.
    conditions_by_task: dict[Any, list[dict[str, Any]]] = {}
    for row in conditions:
        if not isinstance(row, dict):
            continue
        conditions_by_task.setdefault(row.get("taskId"), []).append(
            {
                "stepId": row.get("stepId"),
                "operation": row.get("operation"),
                "condition": row.get("waitCondition"),
                "description": row.get("waitDescription"),
                "reasonCode": row.get("reasonCode"),
            }
        )
    for entry in waiting:
        entry["waitingOn"] = conditions_by_task.get(entry.get("taskId"), [])
    anomalies = work.get("anomalies") if isinstance(work.get("anomalies"), list) else []
    current_task = {
        "nextStep": work.get("nextStep"),
        "waitingFor": waiting,
        "anomalies": anomalies[:3],
        "blockedBranches": work.get("blockedBranches", [])[-5:],
    }
    if "paused" in work:
        current_task["paused"] = bool(work.get("paused"))
    if "decision" in work:
        current_task["decision"] = dict(work.get("decision") or {})

    native_clock = _value(world, "timeOfDay")
    clock_display = UNKNOWN
    if isinstance(native_clock, int) and not isinstance(native_clock, bool) and 0 <= native_clock <= 2600 and native_clock % 100 < 60:
        hours, minutes = divmod(native_clock, 100)
        clock_display = f"{hours % 24:02d}:{minutes:02d}" + (" (after midnight)" if hours >= 24 else "")

    context: dict[str, Any] = {
        "origin": origin,
        "provenance": "smapi_native_snapshot",
        "worldRevision": _revision_of(snapshot),
        "date": date,
        "weather": weather,
        "time": native_clock,
        "timeDisplay": clock_display,
        "timeFormat": "time is native HHMM, not elapsed minutes; 1000 means 10:00. Use timeDisplay when reporting the clock.",
        "location": location_block,
        "playerLocation": player_location,
        "stamina": stamina_block,
        "restState": _value(companion_snapshot, "restState"),
        "funds": funds,
        "resources": {
            "player": {"money": _value(world, "playerMoney"),
                       "stamina": _value(world, "playerStamina"),
                       "maxStamina": _value(world, "playerMaxStamina"),
                       "items": world.get("playerItems", UNKNOWN)},
            "companion": {"spendableMoney": funds, "moneyStatus": money_status or UNKNOWN,
                          "stamina": stamina_block, "inventory": inventory_block},
            "note": "玩家金币和伙伴可花钱包分别观察，不能相加或推定同一钱包；未知不是零",
        },
        "farmWork": {
            "locationId": "Farm",
            "observationStatus": farm_work.get("observationStatus", "observed" if farm_observed else "not-observed"),
            "scope": farm_work.get("scope", "location"),
            "visibleObservation": farm_work if farm_work.get("observationStatus") == "partial" else None,
            **{key: farm_work.get(key, UNKNOWN) if farm_observed else UNKNOWN
               for key in ("tilledUnwateredCount", "cropUnwateredCount", "matureCropCount", "deadCropCount",
                           "cultivatedPlots", "cultivatedPlotCount", "cultivatedPlotsTruncated", "totalCropCount")},
            **({"cropWaterFacts": crop_water_facts(farm_work)} if farm_observed else {}),
        },
        "duePreparation": [] if compact else work.get("duePreparation", []),
        "inventory": inventory_block,
        "goals": goals,
        "currentTask": current_task,
        "farmActionFacts": _action_facts(payload, location, _revision_of(snapshot),
                                         farm_work, farm_observed, work),
    }
    native = native_segment(payload)
    if native is not None:
        context["native"] = native
        context["nativeUiContract"] = {
            "revision": 5,
            "discoverGroup": "native_ui",
            "advanceOperation": "native_advance",
            "advanceParameters": {"allow_skip": "boolean (default false)"},
            "answerOperation": "native_answer",
            "answerParameters": {"choice_index": "nonnegative integer from observed questionResponses"},
            "namingOperation": "native_menu_select",
            "namingParameters": {"choice_index": 0, "location_id": "current observed location", "text": "explicit name from authorized intent; native namingInput describes current input"},
            "note": "activeMenu=CompanionConversation is the player's companion chat, not a world dialogue or choice; leave it open and continue companion farm work without native_advance/native_answer. clockPauseOwner=model-thinking is the normal decision pause, not human takeover or a blocked job. native_advance handles actual events/dialogue/menus only; it does not advance an idle clock. Model parameters do not include location_id; runtime supplies it.",
        }
    if "paused" in work:
        context["paused"] = bool(work.get("paused"))
    if "plantingEvidence" in work:
        context["plantingEvidence"] = work["plantingEvidence"]
    if "decision" in work:
        context["decision"] = dict(work.get("decision") or {})
    if isinstance(work.get("continuity"), dict):
        context["continuity"] = work["continuity"]
    if money_status is not None:
        context["fundsStatus"] = money_status
    if work.get("lastJob") and not compact:
        context["lastResult"] = work["lastJob"]
    if work.get("confirmedExecutionTotals") and not compact:
        context["confirmedExecutionTotals"] = work["confirmedExecutionTotals"]
        context["confirmedRequestTotals"] = work.get("confirmedRequestTotals", {})
        if origin.startswith("life-") and work["confirmedExecutionTotals"].get("hasUnconfirmedCommands"):
            # Chat snapshots arrive on time changes and terminal results, not on
            # every tile effect. Re-reading the cache during an active/cancelled
            # command cannot turn its earlier counts into current observations.
            for key in ("inventory", "resources", "farmWork", "farmActionFacts", "location", "stamina", "funds"):
                context[key] = UNKNOWN
            context["observationStatus"] = "pending_native_result"
            context["note"] = (
                "Native execution is still running or its terminal result is unconfirmed. "
                "Current inventory and field counts are unknown; missing ledger effects do not mean zero actual effects. "
                "Wait for confirmation before reporting a completed/cancelled quantity."
            )
    last_settled = work.get("lastSettledDay")
    if last_settled is not None:
        context["lastSettledDay"] = last_settled
    if actor_mode == "player":
        # The agent IS the sole native player: the legacy companion-labelled
        # blocks (location/stamina/inventory/funds) describe the player being
        # controlled, so keep the wire field names and relabel their meaning.
        context["actorMode"] = "player"
        context["actorMultiplayerId"] = str(payload.get("actorMultiplayerId")) if payload.get("actorMultiplayerId") is not None else UNKNOWN
        context["controlOwner"] = payload.get("controlOwner", UNKNOWN)
        context["actorNote"] = (
            "你是此存档唯一原生玩家（player 模式），直接控制玩家角色本人；"
            "以下 companion/伙伴 命名的字段（位置、体力、背包、资金、水壶）"
            "描述的都是你当前控制的角色本人，不是第二个农夫。"
        )
        farming = payload.get("farming") or {}
        context["nearbyObjects"] = {
            "locationId": farming.get("location"),
            "objects": [{**item, "locationId": farming.get("location", UNKNOWN)}
                        for item in farming["groundItems"] if isinstance(item, dict)]
                       if isinstance(farming.get("groundItems"), list) else UNKNOWN,
            "truncated": farming.get("groundItemsTruncated", True),
            "provenance": "native-current-location-observation",
        }
        context["shopObservation"] = shop
        # A small capability contract, not a scripted activity queue. Unknown
        # world facts remain unknown; the model chooses whether a targeted
        # observation is worth its cost with the resources already above.
        context["dayChoice"] = {
            "boundaryIsNotStrategy": True,
            "remainingResources": "Use time/stamina/funds/inventory above; necessary farm work is not the entire day.",
            "nativeOptions": {
                "travelAndForage": "movement + farming_helpers: observe one purposeful reachable area, pickup observed items",
                "tradeAndCraft": "shopping/crafting: observed prices and unlocked recipes only",
                "landAndResources": "farm/forestry: preserve doors/routes, compare regions including home/water distances",
                "story": "native_ui: actual event/dialogue, legal advance/skip and observed choices",
            },
            "publicActivityTools": "observe_player_activities reads current-map native exits (checked destination targetTile for standing; raw source/nativeLandingTile may be triggers, not standing destinations; no unlock assumed), NPCs/actions/native fixtures (mailbox, parcels, TV, bed)/shore/menu choices; Farm public interactions cover the whole map. talk_npc initiates actually adjacent dialogue; interact_tile uses native map/fixture actions; fish performs one native cast with real rod; attack is one native weapon swing; clear_debris mines observed stones with genuine pickaxe; native_menu_select handles observed item/elevator choices. Discover native_ui schema. Unlock/equipment remain unknown until native evidence; missing observations are not absent content.",
            "sleepDecision": "Record current state and why worthwhile achievable activities are exhausted or return/rest is preferable; watering complete alone is insufficient.",
        }
        clock = context.get("time")
        if isinstance(clock, int) and not isinstance(clock, bool) and 0 <= clock <= 2600 and clock % 100 < 60:
            hours, minutes = divmod(clock, 100)
            context["dayChoice"]["clock"] = {
                "nativeHHMM": clock,
                "display": f"{hours % 24:02d}:{minutes:02d}" + (" (after midnight)" if hours >= 24 else ""),
                "period": "after-midnight" if hours >= 24 else "night" if hours >= 20 else "evening" if hours >= 17 else "afternoon" if hours >= 12 else "morning",
                "minutesUntilNative0200": max(0, 26 * 60 - (hours * 60 + minutes)),
                "meaning": "Native HHMM is a clock, not elapsed minutes. 1120 means 11:20 AM, not late night. Return/rest is still a resource and activity decision, not a fixed bedtime.",
            }
        context["resources"]["note"] = "player模式两处资源命名均指同一原生玩家；不能相加。未知不是零。"
    _attach_companion_memory(context, None if actor_mode == "player" else companion, memory)
    return context


def _attach_companion_memory(
    context: dict[str, Any],
    companion: dict[str, Any] | None,
    memory: dict[str, Any] | None,
) -> None:
    """Attach the optional companion profile and shared-memory blocks (§2).

    ``companion`` is the profile projection (name/personality/playStyle/
    careFrequency); ``memory`` is ``CompanionMemoryStore.render_for_context``
    output (agreements/preferences/recentEvents). Both stay absent entirely
    when not provided, so the default behaviour is unchanged.
    """
    if isinstance(companion, dict) and companion:
        context["companion"] = {
            "name": companion.get("name"),
            "personality": companion.get("personality"),
            "playStyle": companion.get("playStyle"),
            "careFrequency": companion.get("careFrequency"),
        }
    if not isinstance(memory, dict):
        return
    # Layered projection (fact / speculation / user constraint): a
    # scope-limited negative observation must stay labelled as speculation so
    # a later wider observation can supersede it — absence of evidence inside
    # a scan region is never rendered as a global fact.
    layers: dict[str, Any] = {}
    for key, label in (("facts", "facts"), ("speculations", "speculations"), ("constraints", "constraints")):
        rows = memory.get(key)
        if isinstance(rows, list) and rows:
            layers[label] = [dict(r) for r in rows if isinstance(r, dict)]
    if layers:
        context["sharedMemory"] = layers
    agreements = memory.get("agreements")
    if isinstance(agreements, list) and agreements:
        context["agreements"] = [
            {"text": a.get("text"), "gameDate": a.get("gameDate")}
            for a in agreements
            if isinstance(a, dict)
        ]
    events = memory.get("recentEvents")
    if isinstance(events, list) and events:
        context["recentSharedEvents"] = [
            {"text": e.get("text"), "gameDate": e.get("gameDate")}
            for e in events
            if isinstance(e, dict)
        ]

def model_decision_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep one resource source in prompts without changing the public facts API."""
    result = dict(context)
    resources = result.get("resources")
    companion = resources.get("companion") if isinstance(resources, dict) else None
    if isinstance(companion, dict):
        for alias, canonical in (("inventory", "inventory"), ("stamina", "stamina"),
                                 ("funds", "spendableMoney"), ("fundsStatus", "moneyStatus")):
            if canonical in companion:
                result.pop(alias, None)
    # Historical ledgers remain queryable; unrelated past requests are not part
    # of the current decision. Keep the uncertainty flag and the current request.
    request_id = (result.get("decision") or {}).get("requestId")
    totals = result.get("confirmedRequestTotals")
    if isinstance(totals, dict):
        result["confirmedRequestTotals"] = {request_id: totals[request_id]} if request_id in totals else {}
    goals = result.get("goals") or []
    goal_ids = {g.get("id") for g in goals if isinstance(g, dict)}
    prep = result.get("duePreparation")
    if isinstance(prep, list):
        date = result.get("date") or {}
        def past(item):
            trigger = item.get("trigger") or {}
            try:
                return (str(trigger.get("year")) == str(date.get("year"))
                        and trigger.get("season") == date.get("season")
                        and int(trigger.get("day", 99)) < int(date.get("day", 0)))
            except (TypeError, ValueError):
                return False
        # A dated observation is not a fresh command. Preserve linked goals and
        # constraints, but don't repeat their long scope text in preparations.
        result["duePreparation"] = [
            {k: row[k] for k in ("id", "goalId", "trigger") if k in row}
            if row.get("goalId") in goal_ids else row
            for row in prep if isinstance(row, dict) and (row.get("goalId") or not past(row))]
        result["historicalPreparationCount"] = sum(1 for row in prep if isinstance(row, dict) and not row.get("goalId") and past(row))
    return result


def render_decision_context(context: dict[str, Any]) -> str:
    """Render the context as one compact JSON line for prompt injection."""
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


# ----------------------------------------------------------- incremental prompts
# A business provider session (same conversation id) receives the full rules,
# goals and context on its first turn; later turns carry a small always-present
# summary plus only the slices that changed since the last *successfully
# delivered* turn. Delivery state is tracked per (backend, save, cid) by the
# bridge in ``data/work-prompt-state.json``; a failed turn keeps the old state
# so the next turn re-sends the same batch (disconnect-redelivery semantics).
_INCREMENTAL_SLICES = (
    "lastResult", "decision", "currentTask", "waitingConditions",
    "duePreparation", "anomalies", "blockedBranches", "plantingEvidence", "dayChoice",
    "lastSettledDay",
)


def _slice_digest(value: Any) -> str:
    try:
        material = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        material = repr(value)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def build_compact_summary(context: dict[str, Any]) -> dict[str, Any]:
    """Small always-present summary for incremental turns.

    Carries date/time, location, funds/stamina/backpack highlights, control
    ownership/pause state, the main goal as a one-liner plus its current
    stage, and the native UI-state segment. Never carries the confirmed
    request/execution ledgers or full goal scopes.
    """
    summary: dict[str, Any] = {}
    for key in ("date", "time", "weather", "worldRevision", "actorMode",
                "controlOwner", "paused", "lastSettledDay", "fundsStatus", "wakeReason"):
        if key in context:
            summary[key] = context[key]
    location = context.get("location")
    if location is not None:
        summary["location"] = location
    player_location = context.get("playerLocation")
    if player_location is not None:
        summary["playerLocation"] = player_location
    stamina = context.get("stamina")
    if stamina is not None:
        summary["stamina"] = stamina
    funds = context.get("funds")
    if funds is not None:
        summary["funds"] = funds
    resources = context.get("resources")
    if isinstance(resources, dict):
        player = resources.get("player") if isinstance(resources.get("player"), dict) else None
        companion = resources.get("companion") if isinstance(resources.get("companion"), dict) else None
        summary["resources"] = {
            **({"playerMoney": player.get("money")} if isinstance(player, dict) and player.get("money") is not None else {}),
            **({"companionMoney": companion.get("spendableMoney"),
                "moneyStatus": companion.get("moneyStatus")} if isinstance(companion, dict) else {}),
        }
    inventory = context.get("inventory")
    if isinstance(inventory, dict):
        items = inventory.get("items")
        summary["inventory"] = {
            "items": items if isinstance(items, list) else [],
            "freeSlots": inventory.get("freeSlots", UNKNOWN),
            "tools": inventory.get("tools") if isinstance(inventory.get("tools"), list) else [],
        }
        tool_resources = inventory.get("toolResources")
        if isinstance(tool_resources, dict) and tool_resources:
            summary["inventory"]["toolResources"] = tool_resources
    native = context.get("native")
    if isinstance(native, dict):
        summary["native"] = native
    goals = context.get("goals")
    if isinstance(goals, list):
        main = next((g for g in goals if isinstance(g, dict) and g.get("kind") == "main"), None)
        stages = [g for g in goals if isinstance(g, dict) and g.get("kind") != "main"]
        summary["mainGoal"] = main
        summary["activeStages"] = [
            {k: g.get(k) for k in ("id", "text", "kind", "phase", "nextAction") if g.get(k) is not None}
            for g in stages[:3]
        ]
    return summary


def build_incremental_context(
    prev_state: dict[str, Any] | None, context: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Diff the current context against the last delivered state.

    Returns ``(incremental, new_state)``: the incremental block always
    contains the compact summary; its ``changes`` map holds only the slices
    whose digest changed since delivery (requests/terminal receipts, new or
    changed todos, location/resource movement, waiting tasks, anomalies).
    ``new_state`` is the fingerprint record to persist after a successful turn.
    """
    prev_slices = (prev_state or {}).get("slices") or {}
    changes: dict[str, Any] = {}
    slices: dict[str, str] = {}
    for key in _INCREMENTAL_SLICES:
        value = context.get(key)
        digest = _slice_digest(value)
        slices[key] = digest
        if value is not None and prev_slices.get(key) != digest:
            changes[key] = value
    incremental = {"summary": build_compact_summary(context)}
    if changes:
        incremental["changes"] = changes
    request_states = dict((prev_state or {}).get("requestStates") or {})
    decision = context.get("decision")
    request_id = decision.get("requestId") if isinstance(decision, dict) else None
    if request_id:
        terminal = {
            "selected": bool(decision.get("selected")),
            "finished": bool(decision.get("finished")),
            "taskId": decision.get("taskId"),
        }
        request_states[str(request_id)] = _slice_digest(terminal)
    new_state = {
        "slices": slices,
        "requestStates": request_states,
        "goalDigest": _slice_digest(context.get("goals")),
        "contextDigest": _slice_digest(context),
    }
    return incremental, new_state
