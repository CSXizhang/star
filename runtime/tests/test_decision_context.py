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
    assert context["inventory"]["items"] == [{"name": "Parsnip Seeds", "count": 7}]
    assert context["inventory"]["tools"] == [{"name": "Hoe"}, {"name": "Watering Can"}]
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
        {"name": "Parsnip Seeds", "count": UNKNOWN},
        {"name": "Parsnip", "count": 3},
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
