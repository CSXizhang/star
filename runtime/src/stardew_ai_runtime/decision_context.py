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

from typing import Any

from stardew_ai_runtime.decision_policy import project_context

UNKNOWN = "unknown"


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


def goal_context(goal: dict[str, Any]) -> dict[str, Any]:
    """Small model projection of a goal, with its explicit saved scope."""
    result = {**({"id": goal["id"]} if goal.get("id") else {}),
              "text": goal.get("text"), "source": goal.get("source")}
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
            "locationId": location, "observationStatus": "observed",
            "searchBounds": planting.get("searchBounds", UNKNOWN),
            "tiles": {key: {
                "count": _value(candidates, key + "Count"),
                "coordinates": _tiles(candidates.get(key + "Tiles")),
                "truncated": _truncated(candidates.get(key + "Tiles"), candidates.get(key + "Truncated"), 16),
            } for key in ("tilledEmpty", "tillable")},
        }
    else:
        facts["planting"] = {"locationId": location, "observationStatus": "unknown",
                             "tiles": UNKNOWN}
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


def build_decision_context(
    snapshot: Any,
    *,
    work: dict[str, Any] | None = None,
    origin: str = "chat",
    companion: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the compact, unknown-safe context block for one decision.

    Optional keyword-only args (contract §2):
      companion: profile dict with name/personality/playStyle/careFrequency
      memory:    render_for_context() output with agreements/preferences/recentEvents
    When omitted, the default behaviour is unchanged.
    """
    payload = _payload_of(snapshot)
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
    farm_observed = farm_work.get("observationStatus") not in {"not-observed", "unavailable", "unknown"} and (
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
        "tools": tools,
    }
    if len(items) > 24:
        inventory_block["itemsTruncated"] = True

    water_can = companion_snapshot.get("waterCanLevel")
    tool_resources: dict[str, Any] = {}
    if water_can is not None:
        tool_resources["waterCan"] = {
            "level": water_can,
            "max": _value(companion_snapshot, "maxWaterCanLevel"),
        }
    inventory_block["toolResources"] = tool_resources

    work = work if isinstance(work, dict) else {}
    overview_goals = work.get("goals") if isinstance(work.get("goals"), list) else []
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

    context: dict[str, Any] = {
        "origin": origin,
        "provenance": "smapi_native_snapshot",
        "worldRevision": _revision_of(snapshot),
        "date": date,
        "weather": weather,
        "time": _value(world, "timeOfDay"),
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
            "observationStatus": "observed" if farm_observed else "not-observed",
            **{key: farm_work.get(key, UNKNOWN) if farm_observed else UNKNOWN
               for key in ("tilledUnwateredCount", "cropUnwateredCount", "matureCropCount", "deadCropCount")},
        },
        "duePreparation": work.get("duePreparation", []),
        "inventory": inventory_block,
        "goals": goals,
        "currentTask": current_task,
        "farmActionFacts": _action_facts(payload, location, _revision_of(snapshot),
                                         farm_work, farm_observed, work),
    }
    if "paused" in work:
        context["paused"] = bool(work.get("paused"))
    if "plantingEvidence" in work:
        context["plantingEvidence"] = work["plantingEvidence"]
    if "decision" in work:
        context["decision"] = dict(work.get("decision") or {})
    if money_status is not None:
        context["fundsStatus"] = money_status
    if work.get("lastJob"):
        context["lastResult"] = work["lastJob"]
    last_settled = work.get("lastSettledDay")
    if last_settled is not None:
        context["lastSettledDay"] = last_settled
    _attach_companion_memory(context, companion, memory)
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


def render_decision_context(context: dict[str, Any]) -> str:
    """Render the context as one compact JSON line for prompt injection."""
    import json

    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))
