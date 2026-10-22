"""Profile and memory changes update current facts without discarding the agent session."""

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



def test_legacy_session_without_fingerprint_is_preserved(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge.record_conversation_id("save-1", "conv-legacy")
    assert bridge._ensure_session_matches_profile("save-1", "conv-legacy") == "conv-legacy"
    assert bridge.get_conversation_id("save-1") == "conv-legacy"
    assert not (tmp_path / "chat_session_history.json").exists()
    assert bridge.consume_profile_rotation_note() is None



def test_changed_agent_profile_updates_metadata_without_reset(tmp_path: Path) -> None:
    agent_file = tmp_path / "game-agent.md"
    agent_file.write_text("game tools v1", encoding="utf-8")
    bridge = ChatBridge(run_dir=tmp_path)
    bridge.agent_file = agent_file
    bridge.record_conversation_id("save-1", "conv-profile-1")
    before = bridge.profile_fingerprint()
    bridge._ensure_session_matches_profile("save-1", "conv-profile-1")
    agent_file.write_text("game tools v2", encoding="utf-8")
    assert bridge.profile_fingerprint() != before
    assert bridge._ensure_session_matches_profile("save-1", "conv-profile-1") == "conv-profile-1"
    recorded = json.loads((tmp_path / "chat_profile_fingerprints.json").read_text(encoding="utf-8"))
    assert recorded[f"{bridge.backend_name}:save-1"] == bridge.profile_fingerprint()
    assert bridge.consume_profile_rotation_note() is None


def test_missing_agent_file_does_not_break_fingerprint(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge.agent_file = tmp_path / "does-not-exist.md"
    assert bridge.profile_fingerprint()


# Current preferences replace stale facts without replacing conversation history.
def test_deleted_agreement_refreshes_facts_in_same_work_session(tmp_path: Path) -> None:
    """The next snapshot removes deleted preferences while keeping the session."""
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

    # Continue the same conversation with updated current facts.
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-1") == "conv-work-1"
    assert bridge.get_conversation_id("save-1") is not None
    assert not (tmp_path / "chat_session_history.json").exists()
    assert bridge.consume_profile_rotation_note() is None

    # The fresh prompt/decision context must not contain the deleted agreement.
    prompt = bridge._format_agent_prompt("该干活了", "save-1")
    assert "每天给南瓜浇水" not in prompt
    context = bridge._decision_context("save-1")
    assert "每天给南瓜浇水" not in json.dumps(context, ensure_ascii=False)


def test_profile_edit_preserves_work_session(tmp_path: Path) -> None:
    """Editing the companion profile retains its conversation."""
    bridge = ChatBridge(run_dir=tmp_path)
    assert bridge._ensure_session_matches_profile("save-1", None) is None
    bridge.record_conversation_id("save-1", "conv-work-2")
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-2") == "conv-work-2"

    status, _ = bridge._profile_store.set(
        "save-1", {"onboarded": True, "personality": "calm"}, expected_revision=0
    )
    assert status == "confirmed"

    assert bridge._ensure_session_matches_profile("save-1", "conv-work-2") == "conv-work-2"
    assert bridge.get_conversation_id("save-1") is not None
    assert not (tmp_path / "chat_session_history.json").exists()


def test_uncorrected_agreement_keeps_work_session(tmp_path: Path) -> None:
    """Neither an added preference nor an unchanged one replaces the session."""
    bridge = ChatBridge(run_dir=tmp_path)
    assert bridge._ensure_session_matches_profile("save-1", None) is None
    bridge.record_conversation_id("save-1", "conv-work-3")
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-3") == "conv-work-3"

    # No revision change -> still resumes.
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-3") == "conv-work-3"

    # A new revision is delivered as current context.
    status, _ = bridge._memory_store.add(
        "save-1", kind="preference", text="喜欢安静",
        source="player", game_date="1:spring:1", expected_revision=0,
    )
    assert status == "confirmed"
    assert bridge._ensure_session_matches_profile("save-1", "conv-work-3") == "conv-work-3"
    assert bridge.get_conversation_id("save-1") is not None


def test_completion_events_preserve_work_session_after_store_reload(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._ensure_session_matches_profile("save-1", None)
    bridge.record_conversation_id("save-1", "conv-work-events")
    status, _ = bridge._memory_store.add(
        "save-1", kind="event", text="完成了浇水", source="system",
        game_date="1:spring:1", expected_revision=0, command_id="water-1",
    )
    assert status == "confirmed"
    assert bridge._memory_revision("save-1") == 1
    reloaded = ChatBridge(run_dir=tmp_path)
    assert reloaded._ensure_session_matches_profile("save-1", "conv-work-events") == "conv-work-events"
    assert "完成了浇水" in reloaded._format_agent_prompt("继续", "save-1")
    status, _ = reloaded._memory_store.add(
        "save-1", kind="preference", text="空闲先砍树", source="player",
        game_date="1:spring:1", expected_revision=1,
    )
    assert status == "confirmed"
    assert reloaded._ensure_session_matches_profile("save-1", "conv-work-events") == "conv-work-events"
