"""Profile/tool-surface fingerprint session safety.

A Kimi provider session keeps the profile and tool surface it was created with,
so reconfiguring the agent file (or the game tool surface) must start a fresh
session instead of silently resuming the old one. Old session ids stay in history
and durable work memory lives in WorkStore, so nothing is deleted or lost.
"""

from __future__ import annotations

import json
from pathlib import Path

from stardew_ai_runtime.chat_bridge import ChatBridge


def test_first_turn_records_fingerprint_and_stays_new(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    assert bridge.profile_fingerprint()
    assert bridge._ensure_session_matches_profile("save-1", None) is None
    recorded = json.loads(
        (tmp_path / "chat_profile_fingerprints.json").read_text(encoding="utf-8")
    )
    assert recorded[f"{bridge.backend_name}:save-1"] == bridge.profile_fingerprint()


def test_matching_fingerprint_resumes_the_session(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge.record_conversation_id("save-1", "conv-1")
    bridge._save_profile_fingerprints(
        {f"{bridge.backend_name}:save-1": bridge.profile_fingerprint()}
    )
    assert bridge._ensure_session_matches_profile("save-1", "conv-1") == "conv-1"
    assert bridge.consume_profile_rotation_note() is None


def test_legacy_session_without_fingerprint_starts_a_new_session(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge.record_conversation_id("save-1", "conv-legacy")

    result = bridge._ensure_session_matches_profile("save-1", "conv-legacy")

    assert result is None
    # The old id is preserved in history (never deleted) and the session entry is
    # cleared so the next turn starts fresh.
    history = json.loads(
        (tmp_path / "chat_session_history.json").read_text(encoding="utf-8")
    )
    assert history[f"{bridge.backend_name}:save-1"] == ["conv-legacy"]
    assert bridge.get_conversation_id("save-1") is None
    note = bridge.consume_profile_rotation_note()
    assert note is not None and "missing-fingerprint" in note


def test_changed_agent_profile_starts_a_new_session(tmp_path: Path) -> None:
    agent_file = tmp_path / "game-agent.md"
    agent_file.write_text("game tools v1", encoding="utf-8")
    bridge = ChatBridge(run_dir=tmp_path)
    bridge.agent_file = agent_file
    bridge.record_conversation_id("save-1", "conv-profile-1")
    bridge._save_profile_fingerprints(
        {f"{bridge.backend_name}:save-1": bridge.profile_fingerprint()}
    )

    # Same config still resumes.
    assert bridge._ensure_session_matches_profile("save-1", "conv-profile-1") == "conv-profile-1"

    # Reconfiguring the agent file changes the fingerprint -> fresh session.
    agent_file.write_text("game tools v2 (new surface)", encoding="utf-8")
    assert bridge._ensure_session_matches_profile("save-1", "conv-profile-1") is None
    assert bridge.get_conversation_id("save-1") is None

    history = json.loads(
        (tmp_path / "chat_session_history.json").read_text(encoding="utf-8")
    )
    assert history[f"{bridge.backend_name}:save-1"] == ["conv-profile-1"]

    recorded = json.loads(
        (tmp_path / "chat_profile_fingerprints.json").read_text(encoding="utf-8")
    )
    assert recorded[f"{bridge.backend_name}:save-1"] == bridge.profile_fingerprint()
    assert "profile-changed" in (bridge.consume_profile_rotation_note() or "")


def test_missing_agent_file_does_not_break_fingerprint(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge.agent_file = tmp_path / "does-not-exist.md"
    assert bridge.profile_fingerprint()


# ---------------------------------------------------------------- D5: revision-aware rotation
def test_deleted_agreement_rotates_work_session(tmp_path: Path) -> None:
    """Deleting an agreement must invalidate the current work session (contract 顶部/§4).

    The old provider session still carries the deleted agreement in its visible
    history, so the next work turn must rotate instead of resuming it; the old
    session id stays in history and WorkStore data is untouched.
    """
    bridge = ChatBridge(run_dir=tmp_path)
    # Existing work session bound to the current fingerprint.
    assert bridge._ensure_session_matches_profile("save-1", None) is None
    bridge.record_conversation_id("save-1", "conv-work-1")
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-1") == "conv-work-1"

    # Player records an agreement and later deletes it (memoryRevision bumps).
    status, result = bridge._memory_store.add(
        "save-1", kind="agreement", text="每天给南瓜浇水",
        source="player", game_date="1:spring:1", expected_revision=0,
    )
    assert status == "confirmed"
    entry_id = result["entry"]["id"]
    rev = bridge._memory_revision("save-1")
    status, _ = bridge._memory_store.delete("save-1", entry_id, expected_revision=rev)
    assert status == "confirmed"

    # Next work turn rotates: the stale session must not be resumed.
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-1") is None
    assert bridge.get_conversation_id("save-1") is None
    history = json.loads(
        (tmp_path / "chat_session_history.json").read_text(encoding="utf-8")
    )
    assert history[f"{bridge.backend_name}:save-1"] == ["conv-work-1"]
    note = bridge.consume_profile_rotation_note() or ""
    assert "profile-changed" in note

    # The fresh prompt/decision context must not contain the deleted agreement.
    prompt = bridge._format_agent_prompt("该干活了", "save-1")
    assert "每天给南瓜浇水" not in prompt
    context = bridge._decision_context("save-1")
    assert "每天给南瓜浇水" not in json.dumps(context, ensure_ascii=False)


def test_profile_edit_rotates_work_session(tmp_path: Path) -> None:
    """Editing the companion profile (profileRevision bump) rotates the work session."""
    bridge = ChatBridge(run_dir=tmp_path)
    assert bridge._ensure_session_matches_profile("save-1", None) is None
    bridge.record_conversation_id("save-1", "conv-work-2")
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-2") == "conv-work-2"

    status, _ = bridge._profile_store.set(
        "save-1", {"onboarded": True, "personality": "calm"}, expected_revision=0
    )
    assert status == "confirmed"

    assert bridge._ensure_session_matches_profile("save-1", "conv-work-2") is None
    assert bridge.get_conversation_id("save-1") is None
    history = json.loads(
        (tmp_path / "chat_session_history.json").read_text(encoding="utf-8")
    )
    assert history[f"{bridge.backend_name}:save-1"] == ["conv-work-2"]


def test_uncorrected_agreement_keeps_work_session(tmp_path: Path) -> None:
    """Adding (but not deleting) an agreement also rotates, but a no-op does not."""
    bridge = ChatBridge(run_dir=tmp_path)
    assert bridge._ensure_session_matches_profile("save-1", None) is None
    bridge.record_conversation_id("save-1", "conv-work-3")
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-3") == "conv-work-3"

    # No revision change -> still resumes.
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-3") == "conv-work-3"

    # Any successful memory mutate bumps the revision -> rotation.
    status, _ = bridge._memory_store.add(
        "save-1", kind="preference", text="喜欢安静",
        source="player", game_date="1:spring:1", expected_revision=0,
    )
    assert status == "confirmed"
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-3") is None
    assert bridge.get_conversation_id("save-1") is None
