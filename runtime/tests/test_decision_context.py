"""Tests for the compact real-time decision context (unknown-safe, bounded).

The fixture ``fixtures/world-snapshot-real-schema.json`` mirrors the *actual*
captured Mod envelope (``WorldSnapshotPayload`` / ``WorldStateSnapshot`` /
``CompanionSnapshot`` / ``CompanionInventorySnapshot`` field names), so these
assertions test the real schema instead of an invented one.
"""

from __future__ import annotations

import json
from pathlib import Path

from stardew_ai_runtime.decision_context import (
    UNKNOWN,
    build_decision_context,
    render_decision_context,
)

FIXTURE = Path(__file__).parent / "fixtures" / "world-snapshot-real-schema.json"


def _real_snapshot() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _snapshot() -> dict:
    return _real_snapshot()


def test_coop_empty_farm_work_is_unobserved_but_farm_true_zero_is_preserved():
    counts = {key: 0 for key in ("tilledUnwateredCount", "cropUnwateredCount", "matureCropCount", "deadCropCount")}
    for location in ("Coopc24c873f-3070-4dfa-9268-7e9b5ca8df58", "Farm", None):
        snapshot = {"payload": {"companion": {"locationId": location},
                                "world": {"currentLocation": "Farm"}, "farmWork": counts}}
        result = build_decision_context(snapshot, origin="test")["farmWork"]
        assert result["locationId"] == "Farm"
        assert result["observationStatus"] == ("observed" if location == "Farm" else "not-observed")
        assert all(result[key] == (0 if location == "Farm" else UNKNOWN) for key in counts)


def test_context_matches_real_captured_snapshot_schema() -> None:
    snapshot = _real_snapshot()
    context = build_decision_context(
        snapshot,
        work={
            "goals": [{"text": "把农场种满胡萝卜", "source": "user"}],
            "nextStep": {"taskId": "t1", "operation": "water_auto"},
            "anomalies": [{"taskId": "t9", "reasonCode": "X"}],
            "tasks": [{"id": "t2", "title": "等种子", "status": "waiting"}],
            "waitingConditions": [
                {
                    "taskId": "t2",
                    "stepId": "s2",
                    "operation": "plant_seeds",
                    "waitCondition": {"type": "inventory", "params": {"itemId": "(O)472", "minCount": 3}},
                    "waitDescription": "wait:inventory:(O)472>=3 (WAIT_FOR_SEEDS)",
                    "reasonCode": "WAIT_FOR_SEEDS",
                }
            ],
            "lastSettledDay": "1:spring:5",
        },
    )
    assert context["provenance"] == "smapi_native_snapshot"
    assert context["worldRevision"] == 42
    assert context["date"] == {"year": 1, "season": "spring", "day": 6}
    # Real native weather icon, not a rain-flag guess.
    assert context["weather"] == {"icon": "0", "isRaining": False}
    assert context["time"] == 610
    assert context["location"]["name"] == "Farm"
    assert context["location"]["tileX"] == 61
    assert "playerLocation" in context
    assert context["playerLocation"] == "Farm"
    assert context["stamina"] == {"current": 210.5, "max": 270}
    # Funds come from the companion wallet in the real payload.
    assert context["funds"] == 1250
    assert context["inventory"]["freeSlots"] == 5
    assert context["inventory"]["items"] == [{"itemId": "(O)472", "name": "Parsnip Seeds", "count": 7, "quality": 0}]
    assert context["inventory"]["tools"] == [{"itemId": "(T)Hoe", "name": "Hoe"}, {"itemId": "(T)WateringCan", "name": "Watering Can"}]
    assert context["inventory"]["toolResources"]["waterCan"] == {"level": 33, "max": 40}
    assert context["goals"] == [{"text": "把农场种满胡萝卜", "source": "user"}]
    assert context["currentTask"]["nextStep"]["operation"] == "water_auto"
    # The explicit wait condition travels with the waiting task.
    waiting = context["currentTask"]["waitingFor"][0]
    assert waiting["taskId"] == "t2"
    assert waiting["waitingOn"][0]["condition"]["type"] == "inventory"
    assert "wait:inventory" in waiting["waitingOn"][0]["description"]
    assert context["currentTask"]["anomalies"][0]["taskId"] == "t9"
    assert context["lastSettledDay"] == "1:spring:5"

    # No farm/chest bulk payload leaked in, and rendering stays one compact line.
    rendered = render_decision_context(context)
    assert "\n" not in rendered
    assert "tilledUnwateredTiles" not in rendered
    assert "contents" not in rendered


def test_rain_false_is_never_reported_as_clear() -> None:
    # A snapshot with only the legacy rain flag must not invent "clear".
    context = build_decision_context(
        {"payload": {"world": {"dayOfMonth": 3, "season": "winter", "year": 1, "isRaining": False}}}
    )
    assert context["weather"]["isRaining"] is False
    assert "icon" not in context["weather"]
    assert context["weather"] != "clear"

    raining = build_decision_context({"payload": {"world": {"isRaining": True}}})
    assert raining["weather"]["isRaining"] is True


def test_missing_stack_is_unknown_not_one() -> None:
    context = build_decision_context(
        {
            "payload": {
                "inventory": {
                    "freeSlots": 4,
                    "slots": [
                        {"itemId": "(O)472", "name": "Parsnip Seeds"},
                        {"itemId": "(O)24", "name": "Parsnip", "stack": 3},
                    ],
                }
            }
        }
    )
    assert context["inventory"]["items"] == [
        {"itemId": "(O)472", "name": "Parsnip Seeds", "count": UNKNOWN, "quality": UNKNOWN},
        {"itemId": "(O)24", "name": "Parsnip", "count": 3, "quality": UNKNOWN},
    ]


def test_same_name_ingredients_keep_distinct_native_ids_and_available_counts() -> None:
    snapshot = {"inventory": {"slots": [
        {"itemId": "(O)176", "name": "Egg", "stack": 1},
        {"itemId": "(O)180", "name": "Egg", "stack": 15},
        {"name": "unknown ingredient"},
    ]}}
    items = build_decision_context(snapshot)["inventory"]["items"]
    assert {item["itemId"]: item["count"] for item in items if "itemId" in item} == {
        "(O)176": 1, "(O)180": 15,
    }
    assert items[2] == {"name": "unknown ingredient", "count": UNKNOWN, "quality": UNKNOWN}


def test_same_native_item_keeps_distinct_stack_qualities_and_missing_is_unknown() -> None:
    slots = [
        {"itemId": "(O)176", "name": "Egg", "stack": 16, "quality": 0},
        {"itemId": "(O)176", "name": "Egg", "stack": 2, "quality": 2},
        {"itemId": "(O)176", "name": "Egg", "stack": 1},
    ]
    context = build_decision_context({"inventory": {"slots": slots, "freeSlots": 0}})
    assert context["inventory"]["freeSlots"] == 0
    assert [(item["count"], item["quality"]) for item in context["inventory"]["items"]] == [
        (16, 0), (2, 2), (1, UNKNOWN),
    ]


def test_missing_native_fields_render_as_unknown_not_zero() -> None:
    context = build_decision_context({"payload": {"world": {"dayOfMonth": 3}}})
    assert context["date"]["year"] == UNKNOWN
    assert context["date"]["season"] == UNKNOWN
    assert context["date"]["day"] == 3
    assert context["weather"] == UNKNOWN
    assert context["time"] == UNKNOWN
    assert context["stamina"] == UNKNOWN
    assert context["funds"] == UNKNOWN
    assert context["inventory"]["freeSlots"] == UNKNOWN
    assert context["inventory"]["items"] == []


def test_no_snapshot_reports_unavailable_without_guessing() -> None:
    context = build_decision_context(None)
    assert context["provenance"] == UNKNOWN
    assert context["date"] == UNKNOWN
    assert context["location"] == UNKNOWN
    assert "playerLocation" in context
    assert context["playerLocation"] == UNKNOWN
    assert context["funds"] == UNKNOWN
    assert context["inventory"]["items"] == []
    assert context["currentTask"]["nextStep"] is None


def test_location_name_comes_from_companion_not_player() -> None:
    snapshot = {
        "payload": {
            "world": {
                "currentLocation": "FarmHouse",
            },
            "companion": {
                "locationId": "Farm",
                "tileX": 61,
                "tileY": 17,
            },
        }
    }
    context = build_decision_context(snapshot)
    assert context["location"]["name"] == "Farm"
    assert context["location"]["tileX"] == 61
    assert context["location"]["tileY"] == 17
    assert "playerLocation" in context
    assert context["playerLocation"] == "FarmHouse"


def test_context_rebuild_is_bounded_not_append_only() -> None:
    first = build_decision_context(_snapshot())
    second = build_decision_context(_snapshot())
    assert first == second
    # A newer snapshot replaces, never appends to, the previous *formatter*
    # payload. (This does not erase the provider's own earlier prompt history;
    # only provider-session rotation bounds that.)
    newer = _snapshot()
    newer["payload"]["world"]["dayOfMonth"] = 7
    rebuilt = build_decision_context(newer)
    assert rebuilt["date"]["day"] == 7
    assert len(render_decision_context(rebuilt)) <= len(render_decision_context(first)) + 8


def test_companion_and_memory_blocks_are_optional() -> None:
    # Default behaviour is unchanged: no companion/memory keys at all.
    base = build_decision_context(_snapshot())
    assert "companion" not in base
    assert "agreements" not in base
    assert "recentSharedEvents" not in base
    empty = build_decision_context({})
    assert "companion" not in empty
    assert "agreements" not in empty


def test_companion_and_memory_blocks_attach_when_given() -> None:
    companion = {"name": "阿星", "personality": "lively", "playStyle": "earn",
                 "careFrequency": "chatty"}
    memory = {
        "agreements": [{"text": "每天浇水", "gameDate": "1:spring:1"}],
        "preferences": [{"text": "喜欢草莓", "gameDate": "1:spring:2"}],
        "recentEvents": [{"text": "完成了「浇水」", "gameDate": "1:spring:3"}],
    }
    context = build_decision_context(_snapshot(), companion=companion, memory=memory)
    assert context["companion"] == companion
    assert context["agreements"] == [{"text": "每天浇水", "gameDate": "1:spring:1"}]
    assert context["recentSharedEvents"] == [
        {"text": "完成了「浇水」", "gameDate": "1:spring:3"}
    ]
    # Rendered prompt carries the effective agreements for the work turn (§4).
    rendered = render_decision_context(context)
    assert "每天浇水" in rendered
    assert "lively" in rendered


def test_snapshot_companion_section_does_not_clash_with_profile() -> None:
    # The native snapshot's own "companion" section (stamina/location) must keep
    # working while the profile projection lands under the same output key only
    # when explicitly provided.
    snapshot = _snapshot()
    snapshot["payload"]["companion"] = {"stamina": 100, "locationId": "Farm"}
    context = build_decision_context(snapshot)
    assert context["stamina"]["current"] == 100
    assert "companion" not in context
    context2 = build_decision_context(
        snapshot, companion={"name": "n", "personality": "calm",
                             "playStyle": "earn", "careFrequency": "quiet"},
    )
    assert context2["stamina"]["current"] == 100
    assert context2["companion"]["personality"] == "calm"
