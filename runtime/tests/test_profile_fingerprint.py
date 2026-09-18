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
