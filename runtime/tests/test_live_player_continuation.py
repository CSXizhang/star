"""Exercise the shared wakeup dispatcher without legacy goal/queue admission."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

from test_transport_recovery import envelope

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.turn_messages import exchange


def test_player_continues_original_session_without_autonomy_or_goals(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge.actor_mode, bridge.control_owner = "player", "agent"
        bridge.record_conversation_id("save", "original-partner")
        # These obsolete queue markers must not veto native immediate execution.
        bridge._work_store._mutate("save", lambda s: s.decision.update(selected=True, finished=False))
        bridge._autonomy.set_goal_scope("save", "removed-goal")
        bridge._autonomy._mutate("save", lambda s: setattr(s, "breaker_cooldown_until", 10**12))
        turns = []

        async def turn(ws, request, prompt, save, *, _slot_owned):
            assert _slot_owned and bridge._busy_lock.locked()
            turns.append((bridge.get_conversation_id(save), prompt))

        bridge.handle_chat_submit = turn
        tracked = set()
        snapshot = envelope(world={"year": 1, "season": "spring", "dayOfMonth": 1, "timeOfDay": 610})
        await bridge._maybe_schedule_autonomy(snapshot, "save", tracked, None, force=True)
        await asyncio.gather(*tracked)
        assert len(turns) == 1
        # Repeated identical snapshots need no extra model turn. A native clock
        # change resumes the same session even after a promise-only response.
        await bridge._maybe_schedule_autonomy(snapshot, "save", tracked, None, force=True)
        await asyncio.gather(*tracked)
        assert len(turns) == 1
        bridge._autonomy.set_goal_scope("save", None)
        changed = replace(snapshot, payload={"world": {**snapshot.payload["world"], "timeOfDay": 620}})
        await bridge._maybe_schedule_autonomy(changed, "save", tracked, None, force=True)
        await asyncio.gather(*tracked)
        assert len(turns) == 2 and {t[0] for t in turns} == {"original-partner"}
        assert not bridge._autonomy.state("save").enabled
        assert not bridge._work_store.state("save").goals
    asyncio.run(run())


def test_world_message_survives_transient_windows_publish_failure(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "turn-messages.json"
    original = Path.replace
    blocked = True

    def replace(source, target):
        nonlocal blocked
        if target == path and blocked:
            blocked = False
            raise PermissionError(5, "brief sharing violation")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    event = {"requestId": "new-day", "kind": "world_changed", "text": "day 2"}
    exchange(path, "turn", "save", publish=[event])
    assert exchange(path, "turn", "save", take=True) == [event]
    assert exchange(path, "turn", "save", pending_world=True) == [event]
    assert exchange(path, "turn", "save", ack_world=True) == [event]
    assert exchange(path, "turn", "save", pending_world=True) == []


def test_f9_and_explicit_pause_stop_continuation_until_handoff(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge.actor_mode, bridge.control_owner = "player", "agent"
        snapshot = envelope(world={"timeOfDay": 610})
        assert bridge._prepare_autonomy_decision(snapshot, "save")
        await bridge._handle_player_authority(None, {"owner": "human", "reason": "human-takeover"}, "save")
        assert bridge._prepare_autonomy_decision(snapshot, "save") is None
        bridge._send_reply = AsyncMock()
        await bridge._maybe_claim_player_control(None, "save")
        bridge._send_reply.assert_not_called()
        await bridge._handle_player_authority(None, {"owner": "agent", "reason": "handoff"}, "save")
        assert bridge._prepare_autonomy_decision(snapshot, "save")
        bridge._work_store.set_paused("save", True)
        assert bridge._prepare_autonomy_decision(snapshot, "save") is None
        bridge._work_store.set_paused("save", False)
        assert bridge._prepare_autonomy_decision(snapshot, "save")
    asyncio.run(run())
