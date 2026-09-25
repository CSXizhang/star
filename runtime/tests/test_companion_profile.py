"""Companion profile store tests (contract §1.3/§1.4, §2)."""
import json
from pathlib import Path

from stardew_ai_runtime.companion_profile import CompanionProfileStore


def _store(tmp_path: Path) -> CompanionProfileStore:
    return CompanionProfileStore(tmp_path / "data" / "companion-profile.json")


def test_missing_file_is_not_onboarded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get("Save1") == {"profile": None, "profileRevision": 0}


def test_set_confirms_and_increments_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    status, result = store.set(
        "Save1",
        {"onboarded": True, "playStyle": "earn", "personality": "lively",
         "careFrequency": "chatty", "companionName": "小星"},
        expected_revision=0,
    )
    assert status == "confirmed"
    assert result["profileRevision"] == 1
    got = store.get("Save1")
    assert got["profileRevision"] == 1
    profile = got["profile"]
    assert profile is not None
    assert profile["onboarded"] is True
    assert profile["playStyle"] == "earn"
    assert profile["personality"] == "lively"
    assert profile["careFrequency"] == "chatty"
    assert profile["companionName"] == "小星"


def test_set_rejects_stale_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set("Save1", {"onboarded": True}, expected_revision=0)
    status, result = store.set("Save1", {"companionName": "别人"}, expected_revision=0)
    assert status == "rejected"
    assert result["reason"] == "STALE_REVISION"
    assert result["profileRevision"] == 1
    # The conflicting write must not land.
    assert store.get("Save1")["profile"]["companionName"] == "阿星"


def test_set_rejects_invalid_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for patch, reason_prefix in (
        ({"playStyle": "bogus"}, "INVALID_PLAY_STYLE"),
        ({"personality": "bogus"}, "INVALID_PERSONALITY"),
        ({"careFrequency": "bogus"}, "INVALID_CARE_FREQUENCY"),
        ({"companionName": ""}, "INVALID_COMPANION_NAME"),
        ({"companionName": "x" * 13}, "INVALID_COMPANION_NAME"),
    ):
        status, result = store.set("Save1", patch, expected_revision=0)
        assert status == "rejected", patch
        assert result["reason"].startswith(reason_prefix)
    assert store.get("Save1") == {"profile": None, "profileRevision": 0}


def test_skipped_save_exposes_profile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    status, _ = store.set("Save1", {"skipped": True}, expected_revision=0)
    assert status == "confirmed"
    got = store.get("Save1")
    assert got["profile"] is not None
    assert got["profile"]["skipped"] is True


def test_wire_format_camel_case_multi_save(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set("Save1", {"onboarded": True, "careFrequency": "quiet"}, expected_revision=0)
    store.set("Save2", {"onboarded": True}, expected_revision=0)
    raw = json.loads((tmp_path / "data" / "companion-profile.json").read_text(encoding="utf-8"))
    assert set(raw) == {"Save1", "Save2"}
    record = raw["Save1"]
    assert "careFrequency" in record
    assert "profileRevision" in record
    assert "care_frequency" not in record


def test_profile_survives_restart_and_save_switch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set("Save1", {"onboarded": True, "personality": "tsundere"}, expected_revision=0)
    # Simulate a restart: a brand new store instance over the same file.
    reopened = _store(tmp_path)
    got = reopened.get("Save1")
    assert got["profileRevision"] == 1
    assert got["profile"]["personality"] == "tsundere"
    # A different save is untouched (no cross-save leakage).
    assert reopened.get("Save2") == {"profile": None, "profileRevision": 0}
