"""Companion care service tests (contract §1.7, §2)."""
from pathlib import Path

from stardew_ai_runtime.companion_care import CompanionCareService


def _svc(tmp_path: Path) -> CompanionCareService:
    return CompanionCareService(tmp_path / "data" / "companion-care.json")


def test_quiet_never_fires(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.maybe_fire("Save1", "morning", "1:spring:2", "1:spring:2", "quiet", 600) is None
    assert svc.maybe_fire("Save1", "work-done", "1:spring:2", "cmd-1", "quiet", 1200) is None


def test_first_fire_then_event_key_dedup(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    key = svc.maybe_fire("Save1", "morning", "1:spring:2", "1:spring:2", "moderate", 600)
    assert key == "morning:1:spring:2:1:spring:2"
    assert svc.is_fired("Save1", key)
    # Same event again (retry/restart): no re-fire.
    assert svc.maybe_fire("Save1", "morning", "1:spring:2", "1:spring:2", "moderate", 700) is None


def test_daily_frequency_limit(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.maybe_fire("Save1", "morning", "1:spring:2", "ref-a", "moderate", 600) is not None
    assert svc.maybe_fire("Save1", "evening", "1:spring:2", "ref-b", "moderate", 1900) is not None
    # moderate ≤ 2/day: third distinct event same day is refused.
    assert svc.maybe_fire("Save1", "work-done", "1:spring:2", "ref-c", "moderate", 2000) is None
    # Next day the counter resets.
    assert svc.maybe_fire("Save1", "morning", "1:spring:3", "ref-d", "moderate", 610) is not None


def test_chatty_allows_four(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    tods = [600, 900, 1300, 1900]
    for i, tod in enumerate(tods):
        key = svc.maybe_fire("Save1", "work-done", "1:spring:2", f"ref-{i}", "chatty", tod)
        assert key is not None, f"fire {i} refused"
    assert svc.maybe_fire("Save1", "work-done", "1:spring:2", "ref-4", "chatty", 2100) is None


def test_min_two_game_hour_gap(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.maybe_fire("Save1", "morning", "1:spring:2", "ref-a", "chatty", 600) is not None
    # Only 100 game-minutes later: refused even though the daily count allows it.
    assert svc.maybe_fire("Save1", "work-done", "1:spring:2", "ref-b", "chatty", 700) is None
    # 2 game hours (120 minutes) later: allowed.
    assert svc.maybe_fire("Save1", "work-done", "1:spring:2", "ref-b", "chatty", 720) is not None


def test_fired_keys_survive_restart(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    key = svc.maybe_fire("Save1", "evening", "1:spring:2", "ref-a", "chatty", 1900)
    assert key is not None
    reopened = _svc(tmp_path)
    assert reopened.is_fired("Save1", key)
    assert reopened.maybe_fire("Save1", "evening", "1:spring:2", "ref-a", "chatty", 1910) is None


def test_expired_other_day_key_does_not_block(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    # Same ref on a different day is a distinct event (eventKey embeds gameDate).
    assert svc.maybe_fire("Save1", "morning", "1:spring:2", "ref-a", "moderate", 600) is not None
    assert svc.maybe_fire("Save1", "morning", "1:spring:3", "ref-a", "moderate", 600) is not None
