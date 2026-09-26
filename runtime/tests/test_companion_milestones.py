"""Companion milestone store tests (contract §3.1): catalogue suggestions,
state machine, persistence reload, day-settle verification, reminder candidates,
WorkStore goal/todo wiring, and reserved-funds semantics.
"""
import json
from pathlib import Path

import pytest

from stardew_ai_runtime.companion_milestones import (
    CompanionMilestoneStore,
    MilestoneError,
    wire_node,
)
from stardew_ai_runtime.work_state import WorkStore

SAVE = "Save1"
DATE_11 = {"year": 1, "season": "spring", "day": 11}  # 2 days before Egg Festival
STRAWBERRY_ID = "spring-egg-festival-strawberry:y1"
BUNDLE_ID = "spring-crops-bundle-retention:y1"


def _store(tmp_path: Path) -> CompanionMilestoneStore:
    return CompanionMilestoneStore(tmp_path / "data" / "companion-milestones.json")


def _work_store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path / "data" / "work-state.json")


# ---------------------------------------------------------------- catalogue
def test_earn_playstyle_suggests_strawberry_node(tmp_path: Path) -> None:
    store = _store(tmp_path)
    nodes = store.suggest(DATE_11, "earn")
    assert [n["id"] for n in nodes] == [STRAWBERRY_ID]
    node = nodes[0]
    assert node["status"] == "suggested"
    assert node["targetDate"] == "1:spring:13"
    assert node["daysUntil"] == 2
    assert node["sourceUrl"] == "https://stardewvalleywiki.com/Egg_Festival"
    assert "100g/个" in node["summary"] and "120g" in node["summary"]
    keys = [p["key"] for p in node["prepItems"]]
    assert keys == ["reserve-funds", "buy-at-festival", "pre-till", "plant-after"]
    supports = {p["key"]: p["support"] for p in node["prepItems"]}
    assert supports == {
        "reserve-funds": "manual",
        "buy-at-festival": "manual",
        "pre-till": "capability",
        "plant-after": "capability",
    }
    buy = next(p for p in node["prepItems"] if p["key"] == "buy-at-festival")
    assert "无法代劳" in buy["label"]


def test_community_playstyle_suggests_bundle_node(tmp_path: Path) -> None:
    store = _store(tmp_path)
    day_20 = {"year": 1, "season": "spring", "day": 20}
    nodes = store.suggest(day_20, "community")
    assert [n["id"] for n in nodes] == [BUNDLE_ID]
    node = nodes[0]
    assert node["targetDate"] == "1:spring:28"
    assert node["daysUntil"] == 8
    assert node["sourceUrl"] == "https://stardewvalleywiki.com/Bundles"
    assert "未知" in node["summary"]
    assert len(node["prepItems"]) == 4
    assert all(p["support"] == "manual" for p in node["prepItems"])
    assert {p["verifyItem"] for p in node["prepItems"]} == {
        "Parsnip",
        "Green Bean",
        "Cauliflower",
        "Potato",
    }
    # Outside the second-half-of-spring window there is no bundle suggestion.
    assert store.suggest(DATE_11, "community") == []


@pytest.mark.parametrize("play_style", ["workhorse", "decor", None])
def test_other_playstyles_get_no_template_suggestions(tmp_path: Path, play_style) -> None:
    store = _store(tmp_path)
    assert store.suggest(DATE_11, play_style) == []


def test_strawberry_suggestion_window_is_spring_6_to_13(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Day 5 is outside the window; day 6 is the first suggested day.
    assert store.suggest({"year": 1, "season": "spring", "day": 5}, "earn") == []
    assert [n["id"] for n in store.suggest({"year": 1, "season": "spring", "day": 6}, "earn")] == [
        STRAWBERRY_ID
    ]
    # After spring 13 the target rolls to the next year.
    nodes = store.suggest({"year": 1, "season": "spring", "day": 14}, "earn")
    assert nodes == []
    summer = store.suggest({"year": 1, "season": "summer", "day": 1}, "earn")
    assert summer == []
    assert store.suggest({"year": 2, "season": "spring", "day": 10}, "earn")[0]["id"] == (
        "spring-egg-festival-strawberry:y2"
    )


def test_suggest_without_game_date_returns_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.suggest(None, "earn") == []


# ---------------------------------------------------------------- state machine
def test_propose_requires_valid_target_date(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(MilestoneError):
        store.propose(SAVE, title="自定义节点", target_date="not-a-date")
    with pytest.raises(MilestoneError):
        store.propose(SAVE, title="自定义节点", target_date="1:spring:33")
    node = store.propose(
        SAVE,
        title="夏季闪电棒",
        target_date="1:summer:20",
        summary="雷雨天准备",
        source_url="https://stardewvalleywiki.com/Lightning_Rod",
        game_date=DATE_11,
    )
    assert node["status"] == "suggested"
    assert node["daysUntil"] == 37
    assert node["id"].startswith("custom-")


def test_adopt_persists_template_suggestion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    node = store.adopt(
        SAVE,
        STRAWBERRY_ID,
        reserved_funds=1000,
        planned_count=10,
        terms_note="预留1000g只用于买种子",
        game_date=DATE_11,
    )
    assert node["status"] == "adopted"
    assert node["reservedFunds"] == 1000
    assert node["plannedCount"] == 10
    assert store.revision(SAVE) >= 1
    # list_nodes only sees persisted nodes now.
    assert [n["id"] for n in store.list_nodes(SAVE)] == [STRAWBERRY_ID]


def test_state_machine_guards(tmp_path: Path) -> None:
    store = _store(tmp_path)
    node = store.adopt(SAVE, STRAWBERRY_ID, game_date=DATE_11)
    assert node["status"] == "adopted"
    with pytest.raises(MilestoneError, match="cannot adopt"):
        store.adopt(SAVE, STRAWBERRY_ID, game_date=DATE_11)
    with pytest.raises(MilestoneError, match="unknown node"):
        store.defer(SAVE, "custom-nope", reason="x")
    node = store.defer(SAVE, STRAWBERRY_ID, reason="先攒钱", game_date=DATE_11)
    assert node["status"] == "deferred"
    with pytest.raises(MilestoneError, match="cannot defer"):
        store.defer(SAVE, STRAWBERRY_ID, reason="x", game_date=DATE_11)
    with pytest.raises(MilestoneError, match="cannot revise"):
        store.revise(SAVE, STRAWBERRY_ID, title="x", game_date=DATE_11)
    # defer -> adopt is allowed again.
    node = store.adopt(SAVE, STRAWBERRY_ID, game_date=DATE_11)
    assert node["status"] == "adopted"
    # reopen from deferred returns to suggested; suggested cannot reopen.
    store.defer(SAVE, STRAWBERRY_ID, reason="x", game_date=DATE_11)
    node = store.reopen(SAVE, STRAWBERRY_ID, reason="重新考虑", game_date=DATE_11)
    assert node["status"] == "suggested"
    with pytest.raises(MilestoneError, match="cannot reopen"):
        store.reopen(SAVE, STRAWBERRY_ID, reason="x", game_date=DATE_11)
    # Terminal states are model-locked: settlement owns completed/missed.
    store.adopt(SAVE, STRAWBERRY_ID, game_date=DATE_11)
    store.on_day_settled(SAVE, year=1, season="spring", day=14,
                         player_items=[{"name": "Strawberry Seeds", "quantity": 10}])
    assert store.list_nodes(SAVE)[0]["status"] == "completed"
    with pytest.raises(MilestoneError, match="cannot adopt"):
        store.adopt(SAVE, STRAWBERRY_ID, game_date=DATE_11)
    with pytest.raises(MilestoneError, match="cannot defer"):
        store.defer(SAVE, STRAWBERRY_ID, reason="x", game_date=DATE_11)
    with pytest.raises(MilestoneError, match="cannot revise"):
        store.revise(SAVE, STRAWBERRY_ID, title="x", game_date=DATE_11)


def test_revise_updates_terms(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.adopt(SAVE, STRAWBERRY_ID, reserved_funds=500, game_date=DATE_11)
    node = store.revise(
        SAVE, STRAWBERRY_ID, reserved_funds=800, planned_count=8, terms_note="改预留800g",
        game_date=DATE_11,
    )
    assert node["reservedFunds"] == 800
    assert node["plannedCount"] == 8
    assert node["termsNote"] == "改预留800g"


# ---------------------------------------------------------------- persistence
def test_nodes_survive_store_reload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.adopt(SAVE, STRAWBERRY_ID, reserved_funds=1000, game_date=DATE_11)
    store.defer(SAVE, STRAWBERRY_ID, reason="缓缓", game_date=DATE_11)

    reloaded = _store(tmp_path)
    nodes = reloaded.list_nodes(SAVE)
    assert [n["id"] for n in nodes] == [STRAWBERRY_ID]
    assert nodes[0]["status"] == "deferred"
    assert nodes[0]["reservedFunds"] == 1000
    raw = json.loads((tmp_path / "data" / "companion-milestones.json").read_text(encoding="utf-8"))
    record = raw[SAVE]
    assert record["revision"] >= 2
    actions = [h["action"] for h in record["history"]]
    assert "adopt" in actions and "defer" in actions


# ---------------------------------------------------------------- day settle
def _adopted_store(tmp_path: Path, work_store=None) -> CompanionMilestoneStore:
    store = _store(tmp_path)
    store.adopt(
        SAVE,
        STRAWBERRY_ID,
        reserved_funds=1000,
        work_store=work_store,
        game_date=DATE_11,
    )
    return store


def test_settle_completes_node_when_items_verified(tmp_path: Path) -> None:
    store = _adopted_store(tmp_path)
    changed, candidates = store.on_day_settled(
        SAVE, year=1, season="spring", day=14,
        player_items=[{"name": "Strawberry Seeds", "quantity": 10}],
    )
    assert changed is True
    node = store.list_nodes(SAVE)[0]
    assert node["status"] == "completed"
    assert node["verification"] == "verified"
    statuses = {p["key"]: p["status"] for p in node["prepItems"]}
    assert statuses["buy-at-festival"] == "done"
    assert statuses["plant-after"] == "pending"
    assert statuses["pre-till"] == "pending"
    # Target day passed: no reminder candidates.
    assert candidates == []


def test_settle_marks_missed_when_evidence_insufficient(tmp_path: Path) -> None:
    store = _adopted_store(tmp_path)
    changed, _ = store.on_day_settled(
        SAVE, year=1, season="spring", day=14, player_items=[],
    )
    assert changed is True
    node = store.list_nodes(SAVE)[0]
    assert node["status"] == "adopted"
    assert node["verification"] == "unverified"
    # prepItem verification degrades to pending with empty-but-present data
    statuses = {p["key"]: p["status"] for p in node["prepItems"]}
    assert statuses["plant-after"] == "pending"


def test_settle_marks_unverified_when_no_item_data(tmp_path: Path) -> None:
    store = _adopted_store(tmp_path)
    changed, _ = store.on_day_settled(SAVE, year=1, season="spring", day=14)
    node = store.list_nodes(SAVE)[0]
    assert node["status"] == "adopted"
    assert node["verification"] == "unverified"
    statuses = {p["key"]: p["status"] for p in node["prepItems"]}
    assert statuses["buy-at-festival"] == "unknown"


def test_settle_leaves_deferred_nodes_alone(tmp_path: Path) -> None:
    store = _adopted_store(tmp_path)
    store.defer(SAVE, STRAWBERRY_ID, reason="x", game_date=DATE_11)
    changed, _ = store.on_day_settled(
        SAVE, year=1, season="spring", day=14,
        player_items=[{"name": "Strawberry Seeds", "quantity": 10}],
    )
    # Prep-item facts may still refresh, but a deferred node never transitions.
    assert store.list_nodes(SAVE)[0]["status"] == "deferred"
    assert store.list_nodes(SAVE)[0]["verification"] is None


def test_reminder_candidates_for_nodes_within_two_days(tmp_path: Path) -> None:
    store = _adopted_store(tmp_path)
    # daysUntil == 2 -> reminder with the first pending gap. The first settle
    # may flip prep items to "unknown" (no item data); a repeated same-day
    # settle is then a no-op.
    _, candidates = store.on_day_settled(SAVE, year=1, season="spring", day=11)
    assert [c["id"] for c in candidates] == [STRAWBERRY_ID]
    assert candidates[0]["daysUntil"] == 2
    assert "不冻结" in (candidates[0]["firstGap"] or "")
    changed, candidates = store.on_day_settled(SAVE, year=1, season="spring", day=11)
    assert changed is False
    assert [c["id"] for c in candidates] == [STRAWBERRY_ID]
    # daysUntil == 3 -> no reminder.
    _, candidates = store.on_day_settled(SAVE, year=1, season="spring", day=10)
    assert candidates == []
    # Target day itself (daysUntil == 0) reminds once more.
    _, candidates = store.on_day_settled(SAVE, year=1, season="spring", day=13)
    assert [c["id"] for c in candidates] == [STRAWBERRY_ID]
    assert candidates[0]["daysUntil"] == 0


def test_bundle_node_completion_requires_all_four_items(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.adopt(SAVE, BUNDLE_ID, game_date={"year": 1, "season": "spring", "day": 20})
    changed, _ = store.on_day_settled(
        SAVE, year=1, season="summer", day=1,
        player_items=[
            {"name": "Parsnip", "quantity": 1},
            {"name": "Green Bean", "quantity": 1},
            {"name": "Cauliflower", "quantity": 1},
        ],
    )
    assert changed is True
    node = store.list_nodes(SAVE)[0]
    assert node["status"] == "adopted"  # Potato missing
    _, _ = store.on_day_settled(
        SAVE, year=1, season="summer", day=1,
        player_items=[
            {"name": "Parsnip", "quantity": 1},
            {"name": "Green Bean", "quantity": 1},
            {"name": "Cauliflower", "quantity": 1},
            {"name": "Potato", "quantity": 2},
        ],
    )
    # Previously insufficient evidence can be re-evaluated.
    assert store.list_nodes(SAVE)[0]["status"] == "completed"


# ---------------------------------------------------------------- WorkStore wiring
def test_adopt_creates_goal_and_calendar_todos(tmp_path: Path) -> None:
    store = _adopted_store(tmp_path, work_store=_work_store(tmp_path))
    node = store.list_nodes(SAVE)[0]
    assert node["goalId"]
    todo_ids = node["todoIds"]
    assert set(todo_ids) == {"pre-till", "plant-after"}

    work = _work_store(tmp_path)
    goals = work.list_goals(SAVE)
    goal = next(g for g in goals if g["id"] == node["goalId"])
    assert goal["source"] == "user"
    assert goal["constraints"]["reservedFunds"] == 1000
    assert "不冻结资金" in goal["constraints"]["reservedFundsNote"]

    todos = {t["id"]: t for t in work.list_todos(SAVE)}
    pre_till = todos[todo_ids["pre-till"]]
    assert pre_till["trigger"] == {"type": "calendar", "year": 1, "season": "spring", "day": 12}
    assert pre_till["expiry"] == {"year": 1, "season": "spring", "day": 13}
    plant_after = todos[todo_ids["plant-after"]]
    assert plant_after["trigger"] == {"type": "calendar", "year": 1, "season": "spring", "day": 13}


def test_defer_cancels_todos_and_pauses_goal(tmp_path: Path) -> None:
    work = _work_store(tmp_path)
    store = _adopted_store(tmp_path, work_store=work)
    store.defer(SAVE, STRAWBERRY_ID, reason="缓缓", work_store=work, game_date=DATE_11)
    node = store.list_nodes(SAVE)[0]
    todos = {t["id"]: t for t in work.list_todos(SAVE)}
    assert all(todos[tid]["status"] == "cancelled" for tid in node["todoIds"].values())
    goal = next(g for g in work.list_goals(SAVE) if g["id"] == node["goalId"])
    assert goal["status"] == "paused"


def test_reopen_does_not_resume_goal_without_adoption(tmp_path: Path) -> None:
    work = _work_store(tmp_path)
    store = _adopted_store(tmp_path, work_store=work)
    store.defer(SAVE, STRAWBERRY_ID, reason="x", work_store=work, game_date=DATE_11)
    store.reopen(SAVE, STRAWBERRY_ID, reason="继续", work_store=work, game_date=DATE_11)
    node = store.list_nodes(SAVE)[0]
    assert node["status"] == "suggested"
    goal = next(g for g in work.list_goals(SAVE) if g["id"] == node["goalId"])
    assert goal["status"] == "paused"
    # Cancelled todos are not resurrected.
    todos = {t["id"]: t for t in work.list_todos(SAVE)}
    assert all(todos[tid]["status"] == "cancelled" for tid in node["todoIds"].values())


def test_custom_node_without_capability_items_creates_no_work(tmp_path: Path) -> None:
    work = _work_store(tmp_path)
    store = _store(tmp_path)
    node = store.propose(
        SAVE, title="夏季闪电棒", target_date="1:summer:20", game_date=DATE_11
    )
    store.adopt(SAVE, node["id"], work_store=work, game_date=DATE_11)
    assert work.list_goals(SAVE) == []
    assert work.list_todos(SAVE) == []


# ---------------------------------------------------------------- ordering / wire
def test_merged_nodes_ordering_and_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    far = store.propose(SAVE, title="远一点的节点", target_date="1:summer:10", game_date=DATE_11)
    store.adopt(SAVE, far["id"], game_date=DATE_11)
    near = store.propose(SAVE, title="近一点的节点", target_date="1:spring:12", game_date=DATE_11)
    # suggested nodes sort by daysUntil ascending after adopted ones
    merged = store.merged_nodes(SAVE, DATE_11, "earn")
    assert merged[0]["id"] == far["id"]  # adopted first
    assert merged[1]["id"] == near["id"]  # suggested, daysUntil 1
    assert merged[2]["id"] == STRAWBERRY_ID  # suggested, daysUntil 2
    # completed/missed sort by updatedAt desc at the end
    store.adopt(SAVE, STRAWBERRY_ID, game_date=DATE_11)
    store.on_day_settled(SAVE, year=1, season="spring", day=14,
                         player_items=[{"name": "Strawberry Seeds", "quantity": 10}])
    merged = store.merged_nodes(SAVE, DATE_11, "earn")
    assert merged[-1]["status"] in {"completed", "missed"}


def test_merged_nodes_cap_twelve(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(15):
        node = store.propose(SAVE, title=f"节点{i}", target_date=f"1:summer:{i + 1}", game_date=DATE_11)
        store.adopt(SAVE, node["id"], game_date=DATE_11)
    merged = store.merged_nodes(SAVE, DATE_11, None)
    assert len(merged) == 12


def test_wire_node_shape_omits_internal_fields(tmp_path: Path) -> None:
    store = _adopted_store(tmp_path)
    node = store.list_nodes(SAVE)[0]
    wire = wire_node(node)
    assert set(wire.keys()) == {
        "id",
        "title",
        "status",
        "verification",
        "targetDate",
        "daysUntil",
        "summary",
        "sourceUrl",
        "prepItems",
        "reservedFunds",
        "plannedCount",
        "termsNote",
        "updatedAt",
    }
    assert "goalId" not in wire and "todoIds" not in wire and "target" not in wire
    assert set(wire["prepItems"][0].keys()) == {"key", "label", "support", "status", "note"}


def test_planned_quantity_and_split_stacks_do_not_claim_planting(tmp_path):
    store = _store(tmp_path)
    store.adopt(SAVE, STRAWBERRY_ID, planned_count=10, game_date=DATE_11)
    store.on_day_settled(SAVE, year=1, season="spring", day=14,
                         player_items=[{"name": "Strawberry Seeds", "quantity": 1}])
    node = store.list_nodes(SAVE)[0]
    assert node["status"] == "adopted" and node["verification"] == "unverified"
    store.on_day_settled(SAVE, year=1, season="spring", day=14,
                         player_items=[{"name": "Strawberry Seeds", "quantity": 4},
                                       {"name": "Strawberry Seeds", "quantity": 6}])
    node = store.list_nodes(SAVE)[0]
    assert node["status"] == "completed"
    assert next(p for p in node["prepItems"] if p["key"] == "plant-after")["status"] != "done"


def test_revise_and_readopt_sync_work_idempotently(tmp_path):
    work = _work_store(tmp_path)
    store = _adopted_store(tmp_path, work)
    original = store.list_nodes(SAVE)[0]
    revised = store.revise(SAVE, STRAWBERRY_ID, planned_count=6, reserved_funds=600,
                           terms_note="只种六颗", work_store=work, game_date=DATE_11)
    assert revised["goalId"] == original["goalId"]
    goal = work.list_goals(SAVE)[0]
    assert goal["constraints"]["plannedCount"] == 6
    assert goal["constraints"]["reservedFunds"] == 600
    todos = work.list_todos(SAVE)
    assert all(t["status"] == "cancelled" for t in todos if t["id"] in original["todoIds"].values())
    assert all("计划数量=6" in t["intent"] for t in todos if t["status"] == "pending")
    again = store.revise(SAVE, STRAWBERRY_ID, planned_count=6, reserved_funds=600,
                         terms_note="只种六颗", work_store=work, game_date=DATE_11)
    assert again["todoIds"] == revised["todoIds"]
    store.defer(SAVE, STRAWBERRY_ID, work_store=work)
    assert all(t["status"] == "cancelled" for t in work.list_todos(SAVE))
    store.reopen(SAVE, STRAWBERRY_ID, work_store=work)
    assert work.list_goals(SAVE)[0]["status"] == "paused"
    adopted = store.adopt(SAVE, STRAWBERRY_ID, work_store=work, game_date=DATE_11)
    assert len(work.list_goals(SAVE)) == 1
    assert len([t for t in work.list_todos(SAVE) if t["status"] == "pending"]) == 2
    assert set(adopted["todoIds"].values()).isdisjoint(revised["todoIds"].values())


def test_work_retry_after_milestone_file_write_failure_does_not_duplicate(tmp_path, monkeypatch):
    work = _work_store(tmp_path)
    store = _store(tmp_path)
    original_write = store._write_unlocked
    monkeypatch.setattr(store, "_write_unlocked", lambda: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        store.adopt(SAVE, STRAWBERRY_ID, work_store=work, game_date=DATE_11)
    monkeypatch.setattr(store, "_write_unlocked", original_write)
    store.adopt(SAVE, STRAWBERRY_ID, work_store=work, game_date=DATE_11)
    assert len(work.list_goals(SAVE)) == 1
    assert len(work.list_todos(SAVE)) == 2
