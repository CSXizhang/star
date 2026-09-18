from pathlib import Path

from stardew_ai_runtime.autonomy import AutonomyController


def test_autonomy_is_disabled_until_player_enables_it(tmp_path: Path):
    ctl = AutonomyController(tmp_path / "autonomy.json")
    snap = {"farmWork": {"cropUnwateredTiles": [{"x": 1, "y": 2}]}}
    assert ctl.next_candidate("save-a", snap) is None
    ctl.set_enabled("save-a", True)
    assert ctl.next_candidate("save-a", snap)["kind"] == "water"


def test_autonomy_state_is_partitioned_by_save_and_day(tmp_path: Path):
    path = tmp_path / "autonomy.json"
    ctl = AutonomyController(path)
    ctl.set_enabled("save-a", True)
    ctl.set_preferences("save-a", goal="种植", budget_limit=100)
    ctl.on_day_started("save-a", 2)
    ctl.record_completion("save-a", "water")
    reloaded = AutonomyController(path)
    assert reloaded.state("save-a").day == 2
    assert reloaded.state("save-a").completed == ["water"]
    assert reloaded.state("save-b").enabled is False
    reloaded.on_day_started("save-a", 3)
    assert reloaded.state("save-a").completed == []


def test_autonomy_waits_when_no_native_crop_work(tmp_path: Path):
    ctl = AutonomyController(tmp_path / "autonomy.json")
    ctl.set_enabled("save-a", True)
    assert ctl.next_candidate("save-a", {"farmWork": {"tilledUnwateredTiles": [{"x": 1, "y": 2}]}})["kind"] == "decision"


def test_autonomy_reloads_shared_state_and_deduplicates_events(tmp_path: Path):
    path = tmp_path / "shared" / "data" / "autonomy-state.json"
    first = AutonomyController(path)
    second = AutonomyController(path)
    first.set_enabled("save-a", True)
    assert second.state("save-a").enabled is True
    assert second.record_event("save-a", "world:7", "world.snapshot") is True
    assert first.record_event("save-a", "world:7", "world.snapshot") is False
    first.record_usage("save-a", "2026-09-15", 12)
    assert second.state("save-a").daily_tokens["2026-09-15"]["total_tokens"] == 12


def test_autonomy_world_fingerprint_and_game_day_usage(tmp_path: Path):
    ctl = AutonomyController(tmp_path / "autonomy.json")
    ctl.set_enabled("save-a", True)
    snapshot = {"world": {"season": "spring", "dayOfMonth": 4}, "farmWork": {}}
    candidate = ctl.next_candidate("save-a", snapshot)
    assert candidate and candidate["kind"] == "decision"
    assert ctl.record_world_event("save-a", "save-a:spring:4:decision:x:0", 10) is True
    assert ctl.record_world_event("save-a", "save-a:spring:4:decision:x:0", 11) is False
    ctl.record_usage("save-a", "spring:4", {"input_tokens": 3, "output_tokens": 4, "cache_read_tokens": 2})
    usage = ctl.state("save-a").daily_tokens["spring:4"]
    assert usage == {"input_tokens": 3, "cache_read_tokens": 2, "output_tokens": 4, "total_tokens": None}


def test_free_mode_controls_and_new_day_epoch_are_persisted(tmp_path: Path):
    path = tmp_path / "autonomy.json"
    ctl = AutonomyController(path)
    ctl.control("save-a", "set_mode", mode="free")
    ctl.control("save-a", "set_preferences", budget_limit=25, box_preference="Chest A")
    ctl.control("save-a", "pause")
    assert ctl.state("save-a").paused is True
    epoch = ctl.state("save-a").decision_epoch
    ctl.control("save-a", "resume")
    ctl.on_day_started("save-a", 2)
    state = AutonomyController(path).state("save-a")
    assert state.mode == "free" and state.paused is False
    assert state.budget_limit == 25 and state.box_preference == "Chest A"
    assert state.decision_epoch > epoch and state.daily_spend == 0


def test_spend_reservation_is_idempotent_and_unknown_stays_reserved(tmp_path: Path):
    ctl = AutonomyController(tmp_path / "autonomy.json")
    ctl.set_enabled("save-a", True)
    ctl.set_preferences("save-a", budget_limit=10)
    assert ctl.reserve_spend("save-a", "cmd-1", 7) == 7
    assert ctl.reserve_spend("save-a", "cmd-1", 7) == 7
    ctl.settle_spend("save-a", "cmd-1", None, unknown=True)
    assert ctl.state("save-a").daily_spend == 0
    assert ctl.state("save-a").spend_reservations == {"cmd-1": 7}
    ctl.settle_spend("save-a", "cmd-1", 5)
    assert ctl.state("save-a").daily_spend == 5
