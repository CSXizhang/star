"""Cross-day closed-loop integration for the companion-day stores (contract §4).

Minimal chain: profile set -> memory add -> day settle -> store restart ->
agreement still present -> delete -> decision context no longer carries it.
Also locks the §1 wire shape of the production life projections that the C#
DTOs consume. No provider subprocess, socket, or game is involved.
"""
import json

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.companion_memory import CompanionMemoryStore
from stardew_ai_runtime.companion_profile import CompanionProfileStore
from stardew_ai_runtime.decision_context import build_decision_context

SAVE = "Save1"


def _profile_store(run_dir) -> CompanionProfileStore:
    return CompanionProfileStore(run_dir / "data" / "companion-profile.json")


def _memory_store(run_dir) -> CompanionMemoryStore:
    return CompanionMemoryStore(run_dir / "data" / "companion-memory.json")


def _snapshot() -> dict:
    return {
        "world": {"year": 1, "season": "spring", "dayOfMonth": 2, "timeOfDay": 600},
        "companion": {"stamina": 270, "availableMoney": 5000},
    }


def test_cross_day_loop_agreement_survives_restart_and_delete_reaches_decisions(tmp_path) -> None:
    # 1) Player picks preferences/personality and the pair agrees on a plan.
    profile_store = _profile_store(tmp_path)
    status, result = profile_store.set(
        SAVE,
        {"onboarded": True, "skipped": False, "playStyle": "earn", "personality": "gentle",
         "careFrequency": "moderate", "companionName": "阿星"},
        expected_revision=0,
    )
    assert status == "confirmed"
    assert result["profileRevision"] == 1

    memory_store = _memory_store(tmp_path)
    status, result = memory_store.add(
        SAVE, kind="agreement", text="每天给鸡喂食", source="player",
        game_date="1:spring:1", expected_revision=0,
    )
    assert status == "confirmed"
    entry_id = result["entry"]["id"]
    assert result["memoryRevision"] == 1

    # 2) The agreement reaches the work decision context while valid.
    profile = profile_store.get(SAVE)["profile"]
    render = memory_store.render_for_context(SAVE)
    ctx = build_decision_context(_snapshot(), companion=profile, memory=render)
    assert any(a["text"] == "每天给鸡喂食" for a in ctx["agreements"])

    # 3) Day settle: stores reload from disk on every mutate and stay intact.
    profile_store2 = _profile_store(tmp_path)
    memory_store2 = _memory_store(tmp_path)
    assert profile_store2.get(SAVE)["profileRevision"] == 1
    assert profile_store2.get(SAVE)["profile"]["companionName"] == "阿星"
    assert memory_store2.list(SAVE)["memoryRevision"] == 1

    # 4) Full restart (fresh process view): the agreement is still there...
    reloaded = _memory_store(tmp_path)
    entries = reloaded.list(SAVE)["entries"]
    assert any(e["id"] == entry_id and e["text"] == "每天给鸡喂食" for e in entries)
    ctx_after_restart = build_decision_context(
        _snapshot(), companion=profile, memory=reloaded.render_for_context(SAVE))
    assert any(a["text"] == "每天给鸡喂食" for a in ctx_after_restart["agreements"])

    # 5) ...until the player deletes it; then neither render nor the decision
    #    context may carry the stale authorization (contract §4 rotation).
    status, result = reloaded.delete(SAVE, entry_id, expected_revision=1)
    assert status == "confirmed"
    render = reloaded.render_for_context(SAVE)
    assert all(a["text"] != "每天给鸡喂食" for a in render["agreements"])
    ctx = build_decision_context(_snapshot(), companion=profile, memory=render)
    assert all(a["text"] != "每天给鸡喂食" for a in ctx.get("agreements", []))


def test_memory_wire_entries_match_contract_field_set(tmp_path) -> None:
    """§1.5: life.memory.state entries must carry exactly the documented fields."""
    store = _memory_store(tmp_path)
    store.add(SAVE, kind="agreement", text="每天浇水", source="player",
              game_date="1:spring:1", expected_revision=0)
    store.add(SAVE, kind="event", text="完成了「浇水」", source="system",
              game_date="1:spring:2", expected_revision=0, command_id="cmd-7")
    entries = store.list(SAVE)["entries"]
    assert len(entries) == 2
    expected_keys = {"id", "kind", "text", "source", "gameDate", "createdAt"}
    for entry in entries:
        assert set(entry.keys()) == expected_keys, (
            f"wire entry carries non-contract fields: {sorted(set(entry.keys()) - expected_keys)}"
        )
        json.dumps(entry)  # must be plain JSON-serializable wire data


def test_life_profile_state_work_projection_matches_contract(tmp_path) -> None:
    """§1.3: work.waitingConditions is a display-string list (≤5) on the wire."""
    bridge = ChatBridge(run_dir=tmp_path, backend="agy", enable_plan_worker=False)
    store = bridge._work_store
    store.begin_decision(SAVE, "decision-1")
    store.submit_plan(
        SAVE,
        goal_text="日常农活",
        decision_token="decision-1",
        tasks=[{"id": "task-1", "title": "等待补水",
                "steps": [{"id": "step-1", "operation": "refill_watering_can"}]}],
    )
    store.mark_task_waiting(
        SAVE, "task-1", step_id="step-1",
        condition={"type": "inventory", "params": {"itemId": "WateringCan"}},
        reason_code="NEEDS_WATER_CAN",
    )
    work = bridge._life_work_projection(SAVE)
    assert len(work["waitingConditions"]) <= 5
    assert all(isinstance(c, str) for c in work["waitingConditions"]), (
        "contract §1.3 declares work.waitingConditions as [str]; "
        "objects break the C# List<string> DTO"
    )
    assert all(set(g.keys()) == {"id", "text", "status"} for g in work["activeGoals"])
    assert all(set(t.keys()) == {"id", "intent", "status"} for t in work["recentTodos"])
    assert set(work.keys()) == {
        "mode", "paused", "goal", "dailySpendLimit", "boxPreference", "dailySpend",
        "hasExecutableWork", "lastPlanAction", "planWaitReason", "lastSettledDay",
        "activeGoals", "recentTodos", "waitingConditions", "activity",
    }


def test_life_milestones_state_wire_node_field_set(tmp_path) -> None:
    """§2.2: life.milestones.state nodes must carry exactly the documented fields."""
    bridge = ChatBridge(run_dir=tmp_path, backend="agy", enable_plan_worker=False)
    bridge._profile_store.set(
        SAVE,
        {"onboarded": True, "skipped": False, "playStyle": "earn", "personality": "gentle",
         "careFrequency": "moderate", "companionName": "阿星"},
        expected_revision=0,
    )
    bridge._milestone_store.adopt(
        SAVE,
        "spring-egg-festival-strawberry:y1",
        reserved_funds=1000,
        planned_count=10,
        terms_note="预留1000g",
        game_date={"year": 1, "season": "spring", "day": 11},
    )
    # A live date is required for the relative countdown; settlement alone is not
    # a current game snapshot. Evidence must cover all ten planned seeds.
    bridge._latest_snapshot_payload = {
        "world": {"year": 1, "season": "spring", "dayOfMonth": 14}
    }
    bridge._milestone_store.on_day_settled(
        SAVE, year=1, season="spring", day=14,
        player_items=[{"name": "Strawberry Seeds", "quantity": 10}],
    )
    envelope = bridge._build_milestones_state(SAVE, "lmg-it-1")
    payload = envelope.to_mapping()["payload"]
    assert payload["requestId"] == "lmg-it-1"
    assert payload["status"] == "ok"
    assert len(payload["nodes"]) == 1
    node = payload["nodes"][0]
    expected_keys = {
        "id", "title", "status", "verification", "targetDate", "daysUntil",
        "summary", "sourceUrl", "prepItems", "reservedFunds", "plannedCount",
        "termsNote", "updatedAt",
    }
    assert set(node.keys()) == expected_keys, (
        f"wire node carries non-contract fields: {sorted(set(node.keys()) - expected_keys)}"
    )
    assert node["status"] == "completed"
    assert node["verification"] == "verified"
    assert node["daysUntil"] == -1
    # prepItems serialize null `note` as omitted; the buy item carries one.
    noteless = next(p for p in node["prepItems"] if p["key"] == "reserve-funds")
    assert set(noteless.keys()) == {"key", "label", "support", "status"}
    noted = next(p for p in node["prepItems"] if p["key"] == "buy-at-festival")
    assert set(noted.keys()) == {"key", "label", "support", "status", "note"}
    statuses = {p["key"]: p["status"] for p in node["prepItems"]}
    assert statuses["buy-at-festival"] == "done"
    assert statuses["plant-after"] == "pending"  # Seeds in the bag do not prove planting.
    json.dumps(payload)  # must be plain JSON-serializable wire data
    bridge._latest_snapshot_payload = {}
    undated = bridge._build_milestones_state(SAVE, "lmg-it-undated").to_mapping()["payload"]
    assert "daysUntil" not in undated["nodes"][0]  # Unknown current date is not a stale countdown.
    assert undated["nodes"][0]["status"] == "completed"


def test_life_milestones_state_payload_roundtrips_through_protocol(tmp_path) -> None:
    """§2.2: the wire payload must parse back through LifeMilestonesStatePayload."""
    from stardew_ai_runtime.protocol import LifeMilestonesStatePayload

    bridge = ChatBridge(run_dir=tmp_path, backend="agy", enable_plan_worker=False)
    bridge._profile_store.set(
        SAVE, {"onboarded": True, "playStyle": "earn"}, expected_revision=0
    )
    bridge._latest_snapshot_payload = {
        "world": {"year": 1, "season": "spring", "dayOfMonth": 11}
    }
    envelope = bridge._build_milestones_state(SAVE, "lmg-it-2")
    payload = LifeMilestonesStatePayload.from_mapping(envelope.to_mapping()["payload"])
    assert payload.status == "ok"
    assert payload.game_date == "1:spring:11"
    assert len(payload.nodes) == 1
    node = payload.nodes[0]
    assert node.id == "spring-egg-festival-strawberry:y1"
    assert node.status == "suggested"
    assert node.days_until == 2
    assert node.source_url == "https://stardewvalleywiki.com/Egg_Festival"
    # verification is null on a fresh suggestion: omitted on the wire.
    assert node.verification is None
    assert "verification" not in envelope.to_mapping()["payload"]["nodes"][0]
    assert node.prep_items[0].support == "manual"
