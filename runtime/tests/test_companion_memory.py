"""Companion memory store tests (contract §1.5/§1.6, §2, §4)."""
import json
from pathlib import Path

from stardew_ai_runtime.companion_memory import CompanionMemoryStore


def _store(tmp_path: Path) -> CompanionMemoryStore:
    return CompanionMemoryStore(tmp_path / "data" / "companion-memory.json")


def test_empty_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.list("Save1")
    assert state == {"memoryRevision": 0, "entries": []}


def test_player_add_increments_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    status, result = store.add(
        "Save1", kind="agreement", text="每天浇水", source="player",
        game_date="1:spring:5", expected_revision=0,
    )
    assert status == "confirmed"
    assert result["memoryRevision"] == 1
    entry = result["entry"]
    assert entry["kind"] == "agreement"
    assert entry["source"] == "player"
    assert entry["gameDate"] == "1:spring:5"
    assert entry["createdAt"].endswith("Z")
    # Stale follow-up is rejected.
    status2, result2 = store.add(
        "Save1", kind="preference", text="早睡", source="player",
        game_date="1:spring:5", expected_revision=0,
    )
    assert status2 == "rejected"
    assert result2["reason"] == "STALE_REVISION"


def test_agreement_limit_rejects_with_memory_full(tmp_path: Path) -> None:
    store = _store(tmp_path)
    revision = 0
    for i in range(15):
        status, result = store.add(
            "Save1", kind="agreement", text=f"约定{i}", source="player",
            game_date="1:spring:1", expected_revision=revision,
        )
        assert status == "confirmed"
        revision = result["memoryRevision"]
    status, result = store.add(
        "Save1", kind="agreement", text="超限", source="player",
        game_date="1:spring:1", expected_revision=revision,
    )
    assert status == "rejected"
    assert result["reason"] == "MEMORY_FULL"


def test_event_add_dedupes_by_command_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    status1, r1 = store.add(
        "Save1", kind="event", text="完成了「浇水」", source="system",
        game_date="1:spring:2", expected_revision=0, command_id="cmd-1",
    )
    assert status1 == "confirmed"
    status2, r2 = store.add(
        "Save1", kind="event", text="完成了「浇水」", source="system",
        game_date="1:spring:2", expected_revision=0, command_id="cmd-1",
    )
    # Duplicate terminal: no second entry, no revision bump.
    assert r2["memoryRevision"] == r1["memoryRevision"]
    assert len(store.list("Save1")["entries"]) == 1


def test_event_limit_evicts_oldest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(21):
        status, result = store.add(
            "Save1", kind="event", text=f"事件{i}", source="system",
            game_date="1:spring:1", expected_revision=0, command_id=f"cmd-{i}",
        )
        assert status == "confirmed"
    entries = store.list("Save1")["entries"]
    events = [e for e in entries if e["kind"] == "event"]
    assert len(events) == 20
    texts = {e["text"] for e in events}
    assert "事件0" not in texts  # oldest evicted first
    assert "事件20" in texts


def test_correct_and_delete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, r1 = store.add("Save1", kind="preference", text="喜欢草莓", source="player",
                      game_date="1:spring:1", expected_revision=0)
    entry_id = r1["entry"]["id"]
    status, r2 = store.correct("Save1", entry_id, "喜欢蓝莓", r1["memoryRevision"])
    assert status == "confirmed"
    assert r2["entry"]["text"] == "喜欢蓝莓"
    status, _ = store.delete("Save1", entry_id, r2["memoryRevision"])
    assert status == "confirmed"
    assert store.list("Save1")["entries"] == []
    # Deleting again fails cleanly.
    status, result = store.delete("Save1", entry_id, expected_revision=3)
    assert status == "rejected"
    assert result["reason"] == "NOT_FOUND"


def test_events_cannot_be_corrected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, r1 = store.add("Save1", kind="event", text="完成了「收获」", source="system",
                      game_date="1:spring:1", expected_revision=0, command_id="c")
    status, result = store.correct("Save1", r1["entry"]["id"], "改", r1["memoryRevision"])
    assert status == "rejected"
    assert result["reason"] == "CANNOT_EDIT_EVENT"


def test_render_for_context_limits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    revision = 0
    for i in range(10):
        _, r = store.add("Save1", kind="event", text=f"事件{i}", source="system",
                         game_date="1:spring:1", expected_revision=revision,
                         command_id=f"c-{i}")
        revision = r["memoryRevision"]
    render = store.render_for_context("Save1")
    assert len(render["recentEvents"]) == 8  # latest ≤8
    assert render["recentEvents"][-1]["text"] == "事件9"
    assert render["recentEvents"][0]["text"] == "事件2"


def test_render_budget_truncates_agreements_and_preferences(tmp_path: Path) -> None:
    store = _store(tmp_path)
    revision = 0
    for _ in range(15):
        _, r = store.add("Save1", kind="agreement", text="长" * 90, source="player",
                         game_date="1:spring:1", expected_revision=revision)
        revision = r["memoryRevision"]
    render = store.render_for_context("Save1")
    total = sum(len(e["text"]) for e in render["agreements"])
    assert total <= 1200
    assert 0 < len(render["agreements"]) < 15


def test_deleted_entry_leaves_context(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, r1 = store.add("Save1", kind="agreement", text="旧的约定", source="player",
                      game_date="1:spring:1", expected_revision=0)
    entry_id = r1["entry"]["id"]
    assert any(e["text"] == "旧的约定" for e in store.render_for_context("Save1")["agreements"])
    store.delete("Save1", entry_id, r1["memoryRevision"])
    render = store.render_for_context("Save1")
    assert all(e["text"] != "旧的约定" for e in render["agreements"])


def test_memory_survives_restart_and_day_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add("Save1", kind="agreement", text="跨天约定", source="player",
              game_date="1:spring:1", expected_revision=0)
    store.add("Save1", kind="event", text="完成了「浇水」", source="system",
              game_date="1:spring:2", expected_revision=1, command_id="cmd-x")
    # Restart mid-run; entries and revision must persist (§4 cross-day loop).
    reopened = _store(tmp_path)
    state = reopened.list("Save1")
    assert state["memoryRevision"] == 2
    assert {e["kind"] for e in state["entries"]} == {"agreement", "event"}
    raw = json.loads((tmp_path / "data" / "companion-memory.json").read_text(encoding="utf-8"))
    assert "memoryRevision" in raw["Save1"]
    assert raw["Save1"]["entries"][0]["gameDate"] in {"1:spring:1", "1:spring:2"}
