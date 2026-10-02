import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.agent_backends import DshBackend
from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.life_chat import LifeChatService
from stardew_ai_runtime.protocol import LifeChatSubmitPayload, ProtocolError
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.usage_display import UsageDisplay
from stardew_ai_runtime.usage_meter import dsh_response_usage, recover_dsh_receipts
from stardew_ai_runtime.work_state import WorkStore


def test_notice_delete_restore_is_persistent_and_never_calls_model(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        store = bridge._work_store
        goal = store.add_goal("farm", "照料农场")
        notice = store.request_player_decision("farm", goal.id, "留着树？")
        bridge._await_life_provider = AsyncMock(side_effect=AssertionError("visibility is not a model turn"))
        bridge._send_reply = AsyncMock(return_value=True)
        for action in ("dismiss", "dismiss", "restore"):
            await bridge._handle_life_chat_submit(None, {"requestId": action, "saveId": "farm",
                "mode": "chat", "text": "隐藏或恢复", "noticeAction": action, "noticeIds": [notice["id"]]}, "farm")
            restored = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
            assert bool(restored._work_store.unresolved_player_notices("farm")) == (action == "restore")
            row = next(n for n in restored._life_work_projection("farm")["playerDecisions"] if n["id"] == notice["id"])
            assert row["status"] == ("pending" if action == "restore" else "dismissed")
            assert not store.state("farm").notices[0].get("answeredAt")
        bridge._await_life_provider.assert_not_called()
        assert store.unresolved_player_notices("other") == []
    asyncio.run(run())


def test_mcode_aggregate_usage_does_not_rotate_as_one_request():
    from stardew_ai_runtime.agent_backends import McodeBackend
    usage = McodeBackend._usage({"inputTokens": 120000, "outputTokens": 300, "cacheReadTokens": 0})
    assert usage["input_tokens"] == 120000
    assert ChatBridge._request_input_context(usage) is None


def test_successful_life_context_survives_restart_and_reload_is_delta_once(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    svc = bridge._life_chat
    profile = {"personality": "gentle"}
    live = {"reloadSummary": {"gameSessionId": "loaded-1", "instruction": "重新核实当前世界",
                              "goals": ["historical-goal-marker"]}}
    args = dict(mode="chat", milestones=[], live_context=live)
    first = svc.build_turn_prompt("farm", None, profile, {}, {}, **args)
    assert "historical-goal-marker" in first
    svc.record_context("farm", "cid", None)
    svc.mark_prompt_delivered("farm", "cid", "chat")
    svc = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)._life_chat
    again = svc.build_turn_prompt("farm", "cid", profile, {}, {}, **args)
    assert "温柔体贴" not in again and "reloadSummary" not in again
    live["reloadSummary"]["gameSessionId"] = "loaded-2"
    delta = svc.build_turn_prompt("farm", "cid", profile, {}, {}, **args)
    assert "loaded-2" in delta and "historical-goal-marker" not in delta
    assert "loaded-2" in svc.build_turn_prompt("farm", "cid", profile, {}, {}, **args)  # retry until success
    svc.mark_prompt_delivered("farm", "cid", "chat")
    assert "reloadSummary" not in svc.build_turn_prompt("farm", "cid", profile, {}, {}, **args)
    assert "historical-goal-marker" in svc.build_turn_prompt("farm", None, profile, {}, {}, **args)


def test_work_reload_delivery_survives_restart(tmp_path):
    path = tmp_path / "data/reload-context.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"farm": {"gameSessionId": "load1", "instruction": "revalidate", "archiveMarker": "full"}}))
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    assert "archiveMarker" in bridge._work_reload_context("farm", None)
    assert "archiveMarker" not in bridge._work_reload_context("farm", "cid")
    bridge._work_reload_context("farm", "cid", delivered=True)
    restored = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    assert restored._work_reload_context("farm", "cid") is None
    assert restored._work_reload_context("farm", "other") is not None


def test_resumed_dsh_response_usage_survives_handoff_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("STARDEW_MCP_SURFACE", "light")
    backend = DshBackend(tmp_path, timeout_seconds=.1)
    monkeypatch.setattr(backend, "_prepare", lambda _: (tmp_path, {}))
    monkeypatch.setattr(backend, "_start", lambda *_: None)
    monkeypatch.setattr(backend, "_send", lambda *_: 42)
    monkeypatch.setattr(backend, "close", lambda: None)
    frames = [
        {"method": "session.event", "params": {"sessionId": "old", "event": {"type": "turn/start"}}},
        {"method": "session.event", "params": {"sessionId": "old", "event": {"type": "assistant/message", "seq": 9,
            "data": {"message": {"id": "response-one"}, "usage": {"inputTokens": 20, "cacheReadTokens": 100,
                                                                        "outputTokens": 5, "totalTokens": 125}}}}},
    ]
    for frame in [*frames, frames[-1]]:
        backend._events.put(frame)
    # No terminal: timeout/job handoff must keep the measured response.
    result = backend.run(SimpleNamespace(cancelled=False, process=None, request_id="autonomy-r", save_id="farm"), "old", "work")
    assert not result["success"]
    assert result["usage"]["total_tokens"] == 125
    assert result["usage"]["input_tokens"] == 120
    assert result["usage"]["generations_count"] == 1
    assert ChatBridge._request_input_context(result["usage"]) == 120
    assert len((tmp_path / "data/model-usage.jsonl").read_text().splitlines()) == 1


def test_dsh_unknown_and_reasoning_are_not_fake_zero_or_double_counted():
    assert dsh_response_usage({"inputTokens": True, "outputTokens": -1}) is None
    measured = dsh_response_usage({"inputTokens": 4, "cacheReadTokens": 16, "outputTokens": 8, "reasoningTokens": 7})
    assert measured["total_tokens"] == 28
    assert measured["thinking_tokens"] == 7
    assert ChatBridge._request_input_context({"input_tokens": 4, "cached_input_tokens": 16}) == 20


def test_usage_today_save_total_and_partial_response_without_double_count(tmp_path):
    commands = tmp_path / "chat_commands.jsonl"
    measured = dsh_response_usage({"inputTokens": 20, "cacheReadTokens": 100, "outputTokens": 5, "totalTokens": 125})
    rows = [{"provider": "dsh", "requestId": "same", "saveId": "farm", "timestamp": "2026-10-02T01:00:00Z", "usage": measured},
            {"provider": "dsh", "requestId": "same", "saveId": "other", "timestamp": "2026-10-02T01:00:00Z", "usage": measured},
            {"provider": "dsh", "requestId": "yesterday", "saveId": "farm", "timestamp": "2026-10-01T01:00:00Z", "usage": measured}]
    commands.write_text("".join(json.dumps(r) + "\n" for r in rows))
    responses = tmp_path / "responses.jsonl"
    response = {**rows[0], "responseId": "one"}
    responses.write_text(json.dumps(response) + "\n" + json.dumps(response) + "\n")
    display = UsageDisplay(commands, responses=responses)
    now = datetime.fromisoformat("2026-10-02T12:00:00+08:00")
    assert "250 token" in display.today_text(now)
    assert "125 token" in display.today_text(now, save_id="farm")
    assert "250 token" in display.summary_text(now, save_id="farm", today=False)
    # A real response whose turn terminal was lost becomes a lower bound.
    with responses.open("a") as stream:
        stream.write(json.dumps({**response, "requestId": "lost-terminal", "responseId": "two"}) + "\n")
    assert "250+未记录 token" in display.today_text(now, save_id="farm")


def test_historical_recovery_is_save_scoped_idempotent_and_read_only(tmp_path):
    commands = tmp_path / "chat_commands.jsonl"
    row = {"provider": "dsh", "conversationId": "stardew-own", "saveId": "farm", "requestId": "autonomy-r",
           "timestamp": "2026-10-02T01:00:10Z", "durationSeconds": 10, "usage": None}
    commands.write_text(json.dumps(row) + "\n")
    path = tmp_path / "data/dsh-home/sessions/game/stardew-own/session.v3.jsonl"
    path.parent.mkdir(parents=True)
    when = int(datetime.fromisoformat("2026-10-02T01:00:00+00:00").timestamp() * 1000)
    events = [{"type": "turn/start", "time": when}, {"type": "assistant/message", "time": when + 5000, "seq": 5,
              "data": {"message": {"id": "raw-response"}, "usage": {"inputTokens": 2, "cacheReadTokens": 10, "outputTokens": 3}}}]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    original = (path.read_bytes(), commands.read_bytes())
    assert recover_dsh_receipts(tmp_path, commands) == 1
    assert recover_dsh_receipts(tmp_path, commands) == 0
    assert original == (path.read_bytes(), commands.read_bytes())


def test_decisions_survive_delivery_cap_restart_and_save_isolation(tmp_path):
    path = tmp_path / "work.json"
    store = WorkStore(path)
    goal = store.add_goal("farm", "农场装修")
    notice = store.request_player_decision("farm", goal.id, "保留树还是扩大田区？")
    store.acknowledge_player_notice("farm", notice["id"])
    for i in range(25):
        store.request_player_decision("farm", goal.id, f"另一件事{i}")
    restored = WorkStore(path)
    assert any(n["id"] == notice["id"] for n in restored.unresolved_player_notices("farm"))
    assert restored.unresolved_player_notices("other") == []
    restored.answer_player_notice("farm", notice["id"], "保留树", "reply-one")
    assert all(n["id"] != notice["id"] for n in WorkStore(path).unresolved_player_notices("farm"))


def test_lost_confirmation_remains_in_reconnect_projection_after_new_notices(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    store = bridge._work_store
    goal = store.add_goal("farm", "test")
    notice = store.request_player_decision("farm", goal.id, "keep the tree?")
    store.acknowledge_player_notice("farm", notice["id"])
    # The game never receives this answer's terminal reply.
    store.answer_player_notice("farm", notice["id"], "keep it", "lost-reply")
    for i in range(30):
        store.request_player_decision("farm", goal.id, f"another question {i}")
    restored = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    decisions = restored._life_work_projection("farm")["playerDecisions"]
    assert next(n for n in decisions if n["id"] == notice["id"])["status"] == "answered"
    saved = next(n for n in restored._work_store.state("farm").notices if n["id"] == notice["id"])
    assert saved["answer"] == "keep it" and saved["answerRequestId"] == "lost-reply"
    assert not restored._life_work_projection("other")["playerDecisions"]


@pytest.mark.parametrize("success,resolve", [(True, True), (False, True), (True, False)])
def test_free_text_decision_reply_keeps_question_and_only_success_answers(tmp_path, success, resolve):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        goal = bridge._work_store.add_goal("farm", "装修")
        notice = bridge._work_store.request_player_decision("farm", goal.id, "树要保留吗？")
        captured = []
        async def provider(task, cid, prompt, mode):
            captured.append(prompt)
            return {"success": success, "response": "记下了。" if success else "连接失败", "usage": None}
        bridge._await_life_provider = provider
        bridge._send_reply = AsyncMock(return_value=True)
        bridge._start_accepted_preparation = AsyncMock(return_value=None)
        await bridge._handle_life_chat_submit(None, {"requestId": "reply", "saveId": "farm", "mode": "chat",
                                                   "text": "保留它", "replyToNoticeId": notice["id"], "resolveNotice": resolve}, "farm")
        assert "树要保留吗？" in captured[0] and "保留它" in captured[0]
        pending = bridge._work_store.unresolved_player_notices("farm")
        assert bool(pending) is not (success and resolve)
        terminals = [call.args[1] for call in bridge._send_reply.call_args_list if call.args[1].message_type == "life.chat.reply"]
        assert terminals[-1].payload.get("answeredNoticeId") == (notice["id"] if success and resolve else None)
    asyncio.run(scenario())


def test_invalid_decision_reference_is_rejected():
    with pytest.raises(ProtocolError):
        LifeChatSubmitPayload.from_mapping({"requestId": "r", "saveId": "f", "text": "好", "replyToNoticeId": 9})


@pytest.mark.parametrize("reason", ["missing", "answered", "other-save"])
@pytest.mark.parametrize("text", ["保留树", "暂停"])
def test_stale_decision_submit_rejects_before_provider_or_control(tmp_path, reason, text):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        store = bridge._work_store
        goal = store.add_goal("farm", "装修")
        notice = store.request_player_decision("farm", goal.id, "树要保留吗？")
        reference, save_id = notice["id"], "farm"
        if reason == "missing":
            reference = "no-such-notice"
        elif reason == "answered":
            store.answer_player_notice("farm", reference, "保留树", "previous-reply")
        else:
            save_id = "other"
        before = store.state_path.read_bytes()
        bridge._await_life_provider = AsyncMock()
        bridge._handle_autonomy_control = AsyncMock()
        bridge._send_reply = AsyncMock(return_value=True)
        await bridge._handle_life_chat_submit(None, {"payload": {
            "requestId": "stale-reply", "saveId": save_id, "text": text,
            "replyToNoticeId": reference, "resolveNotice": True,
        }}, "farm")
        bridge._await_life_provider.assert_not_awaited()
        bridge._handle_autonomy_control.assert_not_awaited()
        assert not bridge._life_queue
        assert store.state_path.read_bytes() == before
        reply = bridge._send_reply.call_args.args[1]
        assert reply.message_type == "life.chat.reply"
        assert reply.payload["requestId"] == "stale-reply"
        assert reply.payload["saveId"] == save_id
        if reason == "answered":
            assert reply.payload["status"] == "completed"
            assert reply.payload["answeredNoticeId"] == reference
            assert "保留树" in reply.payload["replyText"]
        else:
            assert reply.payload["status"] == "failed"
            assert reply.payload["error"] == ("SAVE_MISMATCH" if reason == "other-save" else "DECISION_NO_LONGER_PENDING")
            assert not reply.payload.get("answeredNoticeId")
    asyncio.run(scenario())


@pytest.mark.parametrize("resolve", [False, True])
def test_queued_decision_answered_before_drain_rejects_without_provider(tmp_path, resolve):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        store = bridge._work_store
        goal = store.add_goal("farm", "装修")
        notice = store.request_player_decision("farm", goal.id, "树要保留吗？")
        bridge._await_life_provider = AsyncMock()
        bridge._start_accepted_preparation = AsyncMock()
        bridge._send_reply = AsyncMock(return_value=True)
        async with bridge._busy_lock:
            await bridge._handle_life_chat_submit(None, {
                "requestId": "queued-reply", "saveId": "farm", "text": "保留树",
                "replyToNoticeId": notice["id"], "resolveNotice": resolve,
            }, "farm")
            assert len(bridge._life_queue) == 1
            assert bridge._send_reply.call_args.args[1].payload["status"] == "queued"
            store.answer_player_notice("farm", notice["id"], "扩大田区", "other-reply")
            before = store.state_path.read_bytes()
        await bridge._drain_life_queue(None)
        bridge._await_life_provider.assert_not_awaited()
        bridge._start_accepted_preparation.assert_not_awaited()
        assert not bridge._life_queue
        assert store.state_path.read_bytes() == before
        reply = bridge._send_reply.call_args.args[1]
        assert reply.payload["requestId"] == "queued-reply"
        assert reply.payload["saveId"] == "farm"
        if resolve:
            assert reply.payload["status"] == "completed"
            assert reply.payload["answeredNoticeId"] == notice["id"]
            assert "扩大田区" in reply.payload["replyText"]
        else:
            assert reply.payload["status"] == "failed"
            assert reply.payload["error"] == "DECISION_NO_LONGER_PENDING"
            assert not reply.payload.get("answeredNoticeId")
    asyncio.run(scenario())


def test_life_context_rotation_counts_cached_input_and_survives_restart(tmp_path):
    sessions, fingerprints = {}, {}
    def service():
        return LifeChatService("dsh", sessions, tmp_path / "sessions.json", fingerprints, tmp_path / "fingerprints.json")
    life = service()
    life.rotate_if_needed("farm", 1, 1)
    life.record_session_id("farm", "old")
    life.record_context("farm", "old", ChatBridge._request_input_context({"input_tokens": 1000, "cached_input_tokens": 110000}))
    assert service().rotate_if_needed("farm", 1, 1) is None
    assert life.get_session_id("farm") is None


def test_map_response_bounds_details_but_keeps_geometry_and_completeness():
    async def scenario():
        scheduler = CompanionScheduler()
        original = {"rows": ["OOO"], "mapWidth": 3, "mapHeight": 1,
                    "occupants": [{"kind": "tree", "tile": {"x": i, "y": 1}} for i in range(1000)]}
        scheduler._execute_native_action = AsyncMock(return_value={"details": {"farmSpace": original}})
        result = await scheduler.query_farm_space()
        assert result["rows"] == original["rows"]
        assert result["occupantCount"] == 1000 and result["occupantsTruncated"]
        assert len(result["occupants"]) == 128 and result["occupantCounts"]["tree"] == 1000
        assert len(original["occupants"]) == 1000
    asyncio.run(scenario())


@pytest.mark.parametrize("truncated", [False, True])
def test_farming_tool_projects_native_refill_scope_even_when_list_is_empty(truncated):
    async def scenario():
        farming = {"location": "Farm", "refillWaterTiles": [], "refillWaterScope": "near-companion",
                   "refillWaterCenter": {"x": 20, "y": 20}, "refillWaterRadius": 12,
                   "refillWaterTilesTruncated": truncated, "refillWaterMapComplete": False}
        scheduler = CompanionScheduler()
        scheduler._latest_snapshot_data = {"payload": {"farming": farming}}
        scheduler.ensure_connected = AsyncMock(return_value=SimpleNamespace(world_revision=1, save_id="farm"))
        scheduler._refresh_snapshot = AsyncMock()
        result = await scheduler.query_farming_helpers()
        for field, value in farming.items():
            assert result[field] == value
        assert result["refillWaterMapComplete"] is False
    asyncio.run(scenario())
