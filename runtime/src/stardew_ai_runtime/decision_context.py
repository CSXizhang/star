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
* No full farm tile list, chest dump or shop inventory is included by default.
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


def build_decision_context(
    snapshot: Any,
    *,
    work: dict[str, Any] | None = None,
    origin: str = "chat",
) -> dict[str, Any]:
    """Build the compact, unknown-safe context block for one decision."""
    payload = _payload_of(snapshot)
    world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
    companion = payload.get("companion") if isinstance(payload.get("companion"), dict) else {}
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
            "stamina": UNKNOWN,
            "funds": UNKNOWN,
            "inventory": {"items": [], "freeSlots": UNKNOWN, "tools": []},
            "goals": [],
            "currentTask": {"nextStep": None, "waitingFor": [], "anomalies": []},
            "note": "native snapshot unavailable; fields unknown",
        }
        if isinstance(work, dict):
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

    stamina = _value(companion, "stamina")
    stamina_block: Any = stamina
    if stamina != UNKNOWN:
        stamina_block = {"current": stamina, "max": _value(companion, "maxStamina")}

    location = _value(world, "currentLocation")
    if location == UNKNOWN:
        location = _value(companion, "locationId")
    location_block: Any = location
    tile_x = companion.get("tileX")
    tile_y = companion.get("tileY")
    if location != UNKNOWN or tile_x is not None or tile_y is not None:
        location_block = {
            "name": location,
            "tileX": tile_x if tile_x is not None else UNKNOWN,
            "tileY": tile_y if tile_y is not None else UNKNOWN,
        }

    # Funds: the companion wallet published in the native snapshot. The older
    # shop-section reading is only a fallback for pre-upgrade payloads.
    money = companion.get("availableMoney")
    money_status = companion.get("moneyStatus")
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
        if slot.get("isTool"):
            tools.append({"name": name})
        else:
            items.append({"name": name, "count": count})

    inventory_block: dict[str, Any] = {
        "items": items[:24],
        "freeSlots": _value(inventory, "freeSlots"),
        "tools": tools,
    }
    if len(items) > 24:
        inventory_block["itemsTruncated"] = True

    water_can = companion.get("waterCanLevel")
    tool_resources: dict[str, Any] = {}
    if water_can is not None:
        tool_resources["waterCan"] = {
            "level": water_can,
            "max": _value(companion, "maxWaterCanLevel"),
        }
    inventory_block["toolResources"] = tool_resources

    work = work if isinstance(work, dict) else {}
    overview_goals = work.get("goals") if isinstance(work.get("goals"), list) else []
    goals = [
        {"text": g.get("text"), "source": g.get("source")}
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
        "stamina": stamina_block,
        "funds": funds,
        "inventory": inventory_block,
        "goals": goals,
        "currentTask": current_task,
    }
    if "paused" in work:
        context["paused"] = bool(work.get("paused"))
    if "decision" in work:
        context["decision"] = dict(work.get("decision") or {})
    if money_status is not None:
        context["fundsStatus"] = money_status
    if work.get("lastJob"):
        context["lastResult"] = work["lastJob"]
    last_settled = work.get("lastSettledDay")
    if last_settled is not None:
        context["lastSettledDay"] = last_settled
    return context


def render_decision_context(context: dict[str, Any]) -> str:
    """Render the context as one compact JSON line for prompt injection."""
    import json

    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))
