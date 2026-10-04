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
    goal_context,
    objective_scope,
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


def test_current_goal_scope_survives_repeated_context_projection_and_is_bounded():
    goal = {"id": "g", "text": "种菜", "source": "user", "epoch": 7,
            "constraints": {"objectiveScope": {"summary": "种" * 1500, "targetDate": "1:spring:16",
                                              "plannedCount": None, "termsNote": None, "preparation": ["plant"]}}}
    first = goal_context(goal)
    assert len(first["scope"]["summary"]) == 1200
    assert first["scope"]["truncated"] is True
    assert first["scope"]["revision"] == 7
    assert goal_context(first) == first
    context = build_decision_context(_snapshot(), work={"goals": [first]})
    assert context["goals"][0] == first
    # Wake material uses the unabridged store goal, never the display truncation.
    assert len(objective_scope(goal)["summary"]) == 1500
    assert len(goal["constraints"]["objectiveScope"]["summary"]) == 1500


def test_explicit_candidate_scope_survives_new_session_context_and_double_projection():
    tiles = [{"x": 62 + index, "y": 27} for index in range(19)]
    goal = {"id": "g", "text": "同一批候选", "epoch": 4, "constraints": {
        "milestoneId": "custom-candidates", "objectiveScope": {"summary": "候选19格，已种跳过",
        "plannedCount": 19, "preparation": ["plant", "water"],
        "executionScope": {"locationId": "Farm", "tiles": tiles}}}}
    projected = goal_context(goal)
    execution = projected["scope"]["executionScope"]
    assert execution == {"locationId": "Farm", "tiles": tiles, "tileCount": 19, "truncated": False}
    assert goal_context(projected) == projected
    assert build_decision_context(_snapshot(), work={"goals": [projected]})["goals"][0] == projected
    assert "tileCount" not in objective_scope(goal)["executionScope"]


def test_large_candidate_scope_is_display_bounded_but_raw_wake_and_detail_are_exact():
    tiles = [{"x": index, "y": 27} for index in range(180)]
    goal = {"id": "g", "text": "大批候选", "epoch": 9, "constraints": {
        "milestoneId": "custom-large", "objectiveScope": {"preparation": ["plant"],
        "executionScope": {"locationId": "Farm", "tiles": tiles}}}}
    projected = goal_context(goal)
    execution = projected["scope"]["executionScope"]
    assert len(execution["tiles"]) == 128 and execution["tileCount"] == 180
    assert execution["truncated"] is True and projected["scope"]["truncated"] is True
    assert execution["detailQuery"] == {"tool": "manage_milestones", "params": {
        "action": "list", "node_id": "custom-large"}}
    assert len(objective_scope(goal)["executionScope"]["tiles"]) == 180
    assert goal_context(projected) == projected
    original = json.loads(json.dumps(objective_scope(goal)))
    goal["epoch"] += 1
    goal["project"] = {"summary": "路线备注", "phase": "observe"}
    assert objective_scope(goal) == original
    goal["constraints"]["objectiveScope"]["executionScope"]["tiles"][-1]["y"] = 28
    assert objective_scope(goal) != original
    assert goal_context(goal)["scope"]["executionScope"]["tiles"] == execution["tiles"]


def test_legacy_goal_does_not_invent_candidate_scope_from_seed_count():
    goal = {"text": "种19颗", "constraints": {"plannedCount": 19,
        "objectiveScope": {"plannedCount": 19, "preparation": ["plant"]}}}
    assert "executionScope" not in objective_scope(goal)
    assert "executionScope" not in goal_context(goal)["scope"]


def test_legacy_milestone_scope_excludes_notes_epochs_but_tracks_actual_range():
    goal = {"id": "g", "text": "菜地", "epoch": 1, "project": {"summary": "观察路线"},
            "constraints": {"milestoneSpec": {"todos": [
                {"key": "plant", "intent": "只种包内", "trigger": {"type": "calendar", "day": 20}, "expiry": None},
            ]}}}
    signal = objective_scope(goal)
    projected = goal_context(goal)
    assert projected["scope"]["workItems"][0]["intent"] == "只种包内"
    assert goal_context(projected) == projected
    goal["epoch"] += 1
    goal["project"]["summary"] = "路线备注更新"
    assert objective_scope(goal) == signal
    goal["constraints"]["milestoneSpec"]["todos"][0]["intent"] = "包内和箱内全部种"
    assert objective_scope(goal) != signal


def test_rest_and_local_planting_facts_have_revision_limits_and_seed_sources():
    snapshot = _snapshot()
    payload = snapshot["payload"]
    payload["companion"]["restState"] = "resting"
    payload["planting"] = {"seeds": [{"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 17,
                                       "canPlantCurrentSeason": True, "seasons": ["spring"]}],
                           "candidateTiles": {"tilledEmptyCount": 20,
                                              "tilledEmptyTiles": [{"x": 68 + x, "y": 28} for x in range(20)],
                                              "tilledEmptyTruncated": False,
                                              "tillableCount": 0, "tillableTiles": [], "tillableTruncated": False},
                           "searchBounds": {"center": {"x": 61, "y": 17}, "radius": 15}}
    result = {"taskId": "done", "actualSummary": {"plantedCount": 2, "skippedCount": 17}}
    context = build_decision_context(snapshot, work={"lastJob": result})
    assert context["restState"] == "resting"
    facts = context["farmActionFacts"]
    assert facts["worldRevision"] == 42
    assert facts["planting"]["locationId"] == "Farm"
    assert len(facts["planting"]["tiles"]["tilledEmpty"]["coordinates"]) == 16
    assert facts["planting"]["tiles"]["tilledEmpty"]["truncated"] is True
    assert facts["planting"]["tiles"]["tillable"] == {"count": 0, "coordinates": [], "truncated": False}
    assert facts["seedSources"]["inventory"]["seeds"][0]["count"] == 17
    assert facts["seedSources"]["chests"]["sources"][0]["tile"] == {"x": 66, "y": 20}
    assert facts["seedSources"]["chests"]["sources"][0]["seeds"][0]["count"] == 12
    assert context["lastResult"] == result
    from stardew_ai_runtime.life_chat import LifeChatService
    live = LifeChatService.compact_live_context(context)
    assert live["restState"] == "resting" and live["farmActionFacts"] == facts
    assert live["lastResult"] == result
    assert "contents" not in render_decision_context(context)


def test_unobserved_chests_and_seed_classification_never_invent_stock():
    snapshot = {"worldRevision": 5, "payload": {"companion": {"locationId": "FarmHouse"},
                 "inventory": {"slots": [{"itemId": "(O)472", "name": "Seeds", "stack": 17}]}}}
    context = build_decision_context(snapshot)
    sources = context["farmActionFacts"]["seedSources"]
    assert context["restState"] == UNKNOWN
    assert sources["inventory"]["observationStatus"] == "unknown"
    assert sources["inventory"]["seeds"] == UNKNOWN
    assert sources["chests"]["observationStatus"] == "unknown"
    assert sources["chests"]["sources"] == UNKNOWN
    assert sources["chests"]["queryNeeded"] is True
    # A suggestive display name is not authoritative native seed classification.
    snapshot["payload"]["chests"] = {"items": [{"tile": {"x": 1, "y": 2}, "contents": [
        {"name": "种子", "itemId": "custom", "stack": 99},
    ]}], "truncated": False}
    chest = build_decision_context(snapshot)["farmActionFacts"]["seedSources"]["chests"]
    assert chest["sources"] == [] and chest["queryNeeded"] is True


def test_seed_chest_rows_are_bounded_across_sources_and_mark_incomplete():
    snapshot = {"worldRevision": 8, "payload": {"companion": {"locationId": "Farm"}, "chests": {
        "truncated": True, "items": [{"tile": {"x": x, "y": 1}, "contents": [
            {"itemId": f"seed-{x}-{i}", "name": "Seed", "stack": 1, "isSeed": True} for i in range(3)
        ]} for x in range(12)],
    }}}
    chest = build_decision_context(snapshot)["farmActionFacts"]["seedSources"]["chests"]
    assert len(chest["sources"]) == 6
    assert sum(len(row["seeds"]) for row in chest["sources"]) == 16
    assert chest["truncated"] is True and chest["queryNeeded"] is True


def test_relevant_farm_tiles_use_real_native_fields_and_keep_off_map_scope():
    snapshot = {"worldRevision": 81, "payload": {"companion": {"locationId": "FarmHouse"}, "farmWork": {
        "locationId": "Farm", "observationStatus": "observed", "cropUnwateredCount": 2,
        "cropUnwateredTiles": [{"x": 68, "y": 28}, {"x": 72, "y": 19}], "cropUnwateredTruncated": False,
        "matureCropCount": 1, "matureCrops": [{"x": 72, "y": 24, "cropId": "24"}], "matureCropsTruncated": False,
    }}}
    work = {"goals": [{"text": "持续经营", "constraints": {"objectiveScope": {"preparation": ["production"]}}}]}
    facts = build_decision_context(snapshot, work=work)["farmActionFacts"]["farm"]
    assert facts["locationId"] == "Farm"
    assert facts["currentCompanionLocationId"] == "FarmHouse" and facts["needsNavigation"] is True
    assert facts["unwatered"] == {"coordinates": [{"x": 68, "y": 28}, {"x": 72, "y": 19}], "count": 2, "truncated": False}
    assert facts["harvestable"] == {"coordinates": [{"x": 72, "y": 24}], "count": 1, "truncated": False}
    snapshot["payload"]["farmWork"]["observationStatus"] = "not-observed"
    unobserved = build_decision_context(snapshot, work=work)["farmActionFacts"]["farm"]
    assert unobserved["unwatered"]["coordinates"] == UNKNOWN
    assert unobserved["harvestable"]["count"] == UNKNOWN


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
