"""Tests for the persistent request contract store and terminal-fact outbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def _store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path / "data" / "work-state.json")


def _event(
    command_id: str,
    outcome: str = "completed",
    *,
    reason_code: str | None = None,
    kind: str = "terminal",
    request_id: str | None = None,
) -> dict:
    return {
        "kind": kind,
        "commandId": command_id,
        "taskId": "task-1",
        "goalId": "goal-1",
        "decisionId": "decision-1",
        "operation": "water_auto",
        "outcome": outcome,
        "reasonCode": reason_code,
        "effects": [{"state": "watered", "stack": 3}],
        "feedback": {"text": "浇完了"},
        "snapshotRevision": 7,
        "gameDate": "1:spring:5",
        "requestId": request_id,
    }


def test_append_outbox_event_normalizes_shape_and_persists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = store.append_outbox_event("Save1", _event("cmd-1", "completed"))
    assert stored["id"].startswith("obx-")
    assert stored["delivery"] == {
        "state": "pending", "attempts": 0, "sentAt": None, "consumedAt": None,
    }
    assert stored["notify"] == {
        "key": "cmd-1:completed", "state": "pending", "deliveredAt": None,
    }

    reopened = _store(tmp_path)
    assert [e["id"] for e in reopened.pending_outbox("Save1")] == [stored["id"]]


def test_player_receipt_is_separate_and_saved_reply_replays_without_model(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    from stardew_ai_runtime.chat_bridge import ChatBridge
    from stardew_ai_runtime.protocol import Envelope

    async def check():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        store = bridge._work_store
        event = store.append_outbox_event('Save1', _event('cancel-native', 'cancelled'))
        reply = Envelope.create_chat_reply('bridge', 'delivery-real', 'completed',
                                           '实际取消了，没有补水。', save_id='Save1')
        reply.payload['deliveryOutboxIds'] = [event['id']]
        store.queue_outbox_notification('Save1', [event['id']], reply.to_mapping())
        store.mark_outbox_consumed('Save1', [event['id']])
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._chat_ws = AsyncMock()
        bridge.handle_chat_submit = AsyncMock(side_effect=AssertionError('saved notification cannot run the model'))
        await bridge._run_outbox_delivery('Save1')
        sent = [json.loads(call.args[0]) for call in bridge._chat_ws.send_text.call_args_list]
        assert any(x['payload'].get('replyText') == '实际取消了，没有补水。' for x in sent)
        store = bridge._work_store
        assert store.state('Save1').outbox[0]['notify']['state'] == 'pending'
        store.acknowledge_outbox_notification('Save1', 'wrong-delivery', [event['id']])
        assert store.pending_outbox_notifications('Save1')
        store.acknowledge_outbox_notification('Save1', 'delivery-real', [event['id']])
        assert not WorkStore(store.state_path).pending_outbox_notifications('Save1')
        assert store.state('Save1').outbox[0]['notify']['state'] == 'delivered'
    asyncio.run(check())


def test_player_cancel_closes_original_request_but_keeps_new_requests_available(tmp_path):
    store = _store(tmp_path)
    store.record_request('Save1', 'old', 'Refill and water', origin='player')
    store.stop_request('Save1', 'old', reason='player_cancel')
    store.begin_decision('Save1', 'next', request_id='old')
    job = [{'title': 'retry under a new goal', 'steps': [{'operation': 'refill_watering_can'}]}]
    with pytest.raises(WorkStateError, match='WORK_STOPPED'):
        store.submit_plan('Save1', goal_text='recreated goal', tasks=job, decision_token='next')
    assert not store.state('Save1').tasks
    assert not store.state('Save1').paused
    store.record_request('Save1', 'new', 'Refill now', origin='player')
    store.begin_decision('Save1', 'fresh', request_id='new')
    assert store.submit_plan('Save1', goal_text='new player job', tasks=job, decision_token='fresh')['tasks']


def test_cancelled_request_notification_cannot_grant_another_work_turn(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    from stardew_ai_runtime.chat_bridge import ChatBridge

    async def check():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        store = bridge._work_store
        store.record_request('Save1', 'cancelled-request', 'Refill and water', origin='player')
        store.stop_request('Save1', 'cancelled-request', reason='player_cancel')
        store.append_outbox_event('Save1', _event('native-cancelled', 'cancelled', request_id='cancelled-request'))
        bridge._chat_ws = AsyncMock()
        bridge._execution_occupied = lambda sid: False
        bridge.handle_chat_submit = AsyncMock()
        await bridge._run_outbox_delivery('Save1')
        assert bridge.handle_chat_submit.call_args.kwargs['_delivery']['execution_deferred'] is True
    asyncio.run(check())


def test_player_cancel_handler_stops_the_durable_original_request(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    from stardew_ai_runtime.chat_bridge import ChatBridge

    async def check():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        store = bridge._work_store
        store.record_request('Save1', 'original', 'Refill and water', origin='player')
        store.begin_decision('Save1', 'token', request_id='original')
        store.submit_plan('Save1', goal_text='water goal', decision_token='token',
                          tasks=[{'title': 'water', 'steps': [{'operation': 'refill_watering_can'}]}])
        bridge._active_player_requests['Save1'] = 'original'
        bridge._interrupt_short_job = AsyncMock(return_value=True)
        assert await bridge.handle_chat_cancel(None, None, 'F8 cancel', 'Save1')
        state = store.state('Save1')
        assert state.requests[0]['workStopped'] is True
        assert state.goals[0].status == 'cancelled'
        assert not state.paused
    asyncio.run(check())


def test_outbox_event_requires_command_id_and_known_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(WorkStateError):
        store.append_outbox_event("Save1", {"outcome": "completed"})
    with pytest.raises(WorkStateError):
        store.append_outbox_event("Save1", _event("cmd-x", "completed", kind="bogus"))


def test_terminal_event_updates_in_place_until_consumed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.append_outbox_event("Save1", _event("cmd-1", "completed", reason_code="R1"))
    second = store.append_outbox_event("Save1", _event("cmd-1", "completed", reason_code="R2"))
    assert second["id"] == first["id"]
    outbox = _store(tmp_path).state("Save1").outbox
    assert len(outbox) == 1
    assert outbox[0]["reasonCode"] == "R2"

    store.mark_outbox_sent("Save1", [first["id"]])
    refreshed = store.append_outbox_event("Save1", _event("cmd-1", "completed", reason_code="R3"))
    assert refreshed["reasonCode"] == "R3"
    assert refreshed["delivery"]["state"] == "sent"
    assert refreshed["delivery"]["attempts"] == 1

    store.mark_outbox_consumed("Save1", [first["id"]])
    repeated = store.append_outbox_event("Save1", _event("cmd-1", "completed"))
    assert repeated["id"] == first["id"]
    assert repeated["delivery"]["state"] == "consumed"
    assert len(_store(tmp_path).state("Save1").outbox) == 1


def test_unknown_fact_is_corrected_not_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    unknown = store.append_outbox_event(
        "Save1", _event("cmd-9", "unknown", reason_code="NATIVE_TIMEOUT")
    )
    assert unknown["kind"] == "terminal"

    confirmed = store.append_outbox_event("Save1", _event("cmd-9", "completed"))
    assert confirmed["kind"] == "correction"
    assert confirmed["id"] != unknown["id"]
    assert confirmed["notify"]["key"] == "cmd-9:completed"

    state = _store(tmp_path).state("Save1")
    assert [(e["kind"], e["outcome"]) for e in state.outbox] == [
        ("terminal", "unknown"),
        ("correction", "completed"),
    ]

    repeat_unknown = store.append_outbox_event("Save1", _event("cmd-9", "unknown"))
    assert repeat_unknown["kind"] == "terminal"
    assert len(_store(tmp_path).state("Save1").outbox) == 2

    duplicate = store.append_outbox_event("Save1", _event("cmd-9", "completed"))
    assert duplicate["id"] == confirmed["id"]
    revised = store.append_outbox_event(
        "Save1", _event("cmd-9", "completed", reason_code="REVISED")
    )
    assert revised["kind"] == "correction"
    assert revised["id"] != confirmed["id"]
    assert len(_store(tmp_path).state("Save1").outbox) == 3


def test_sent_event_survives_restart_for_redelivery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = store.append_outbox_event("Save1", _event("cmd-2", "partial"))
    store.mark_outbox_sent("Save1", [stored["id"]])

    reopened = _store(tmp_path)
    pending = reopened.pending_outbox("Save1")
    assert [e["id"] for e in pending] == [stored["id"]]
    assert pending[0]["delivery"]["state"] == "sent"
    assert pending[0]["delivery"]["attempts"] == 1
    assert pending[0]["delivery"]["sentAt"]

    reopened.mark_outbox_sent("Save1", [stored["id"]])
    resent = _store(tmp_path).pending_outbox("Save1")[0]
    assert resent["delivery"]["attempts"] == 2


def test_consumed_event_leaves_pending_outbox(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = store.append_outbox_event(
        "Save1", _event("cmd-3", "failed", reason_code="NATIVE_ERROR")
    )
    store.mark_outbox_sent("Save1", [stored["id"]])
    store.mark_outbox_consumed("Save1", [stored["id"]])
    assert _store(tmp_path).pending_outbox("Save1") == []

    store.mark_outbox_sent("Save1", [stored["id"]])
    event = _store(tmp_path).state("Save1").outbox[0]
    assert event["delivery"]["state"] == "consumed"
    assert event["delivery"]["attempts"] == 1

    again = store.append_outbox_event(
        "Save1", _event("cmd-3", "failed", reason_code="NATIVE_ERROR")
    )
    assert again["delivery"]["state"] == "consumed"
    assert store.pending_outbox("Save1") == []


def test_consumed_outbox_is_bounded_without_dropping_undelivered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    consumed_ids: list[str] = []
    for index in range(105):
        stored = store.append_outbox_event("Save1", _event(f"done-{index:03d}", "completed"))
        consumed_ids.append(stored["id"])
    store.mark_outbox_consumed("Save1", consumed_ids)
    live_ids: list[str] = []
    for index in range(3):
        stored = store.append_outbox_event("Save1", _event(f"live-{index}", "unknown"))
        live_ids.append(stored["id"])

    reopened = _store(tmp_path)
    outbox = reopened.state("Save1").outbox
    assert len(outbox) == 103
    kept_consumed = [e["id"] for e in outbox if e["delivery"]["state"] == "consumed"]
    assert kept_consumed == consumed_ids[5:]
    assert [e["id"] for e in reopened.pending_outbox("Save1")] == live_ids


def test_notify_is_tracked_separately_from_delivery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = store.append_outbox_event(
        "Save1", _event("cmd-n", "failed", reason_code="NATIVE_ERROR")
    )
    event_id = stored["id"]
    store.mark_outbox_sent("Save1", [event_id])
    store.mark_outbox_consumed("Save1", [event_id])

    event = _store(tmp_path).state("Save1").outbox[0]
    assert event["delivery"]["state"] == "consumed"
    assert event["delivery"]["consumedAt"]
    assert event["notify"]["state"] == "pending"

    store.mark_outbox_notified("Save1", [event_id])
    event = _store(tmp_path).state("Save1").outbox[0]
    assert event["notify"]["state"] == "delivered"
    assert event["notify"]["deliveredAt"]
    assert event["notify"]["key"] == "cmd-n:failed"


def test_record_request_is_idempotent_and_bounded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.record_request("Save1", "req-1", "把左边的地浇了", origin="player")
    assert first["taskIds"] == []
    assert first["goalId"] is None
    assert first["createdAt"]

    repeated = store.record_request("Save1", "req-1", "改:先收菜", origin="player-midflight")
    assert repeated["text"] == "把左边的地浇了"
    assert repeated["origin"] == "player"

    for index in range(2, 62):
        store.record_request("Save1", f"req-{index}", f"请求 {index}")
    requests = _store(tmp_path).state("Save1").requests
    assert len(requests) == 50
    assert requests[-1]["id"] == "req-61"
    assert all(r["id"] != "req-1" for r in requests)

    with pytest.raises(WorkStateError):
        store.record_request("Save1", "req-x", "bad origin", origin="system")


def test_pending_request_contract_survives_trimming(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_request("Save1", "root-1", "修栅栏", origin="player")
    store.append_outbox_event("Save1", _event("cmd-a", "unknown", request_id="root-1"))
    for index in range(60):
        store.record_request("Save1", f"flood-{index}", f"中途消息 {index}")

    requests = _store(tmp_path).state("Save1").requests
    assert len(requests) == 50
    assert requests[0]["id"] == "root-1"


def test_bind_request_task_and_find_request_for_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_request("Save1", "req-9", "收土豆", origin="player")
    bound = store.bind_request_task("Save1", "req-9", "task-9", "goal-9")
    assert bound["taskIds"] == ["task-9"]
    assert bound["goalId"] == "goal-9"

    store.bind_request_task("Save1", "req-9", "task-9", "goal-9")
    store.bind_request_task("Save1", "req-9", "task-10", "goal-9")
    reopened = _store(tmp_path)
    assert reopened.find_request_for_task("Save1", "task-9")["id"] == "req-9"
    assert reopened.find_request_for_task("Save1", "task-10")["id"] == "req-9"
    assert reopened.find_request_for_task("Save1", "task-missing") is None
    with pytest.raises(WorkStateError):
        store.bind_request_task("Save1", "req-missing", "task-9", "goal-9")


def test_pending_midflight_requests_reads_without_consuming(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_request("Save1", "m-1", "顺便浇一下后面的地", origin="player-midflight")
    store.record_request("Save1", "m-2", "改:先收菜", origin="player-midflight")
    store.record_request("Save1", "p-1", "最初的请求", origin="player")
    store.record_request("Save1", "a-1", "自治安排", origin="autonomy")

    first = store.pending_midflight_requests("Save1")
    assert [r["id"] for r in first] == ["m-1", "m-2"]
    assert first[0]["text"] == "顺便浇一下后面的地"
    assert first[0]["createdAt"]
    assert [r["id"] for r in store.pending_midflight_requests("Save1")] == ["m-1", "m-2"]

    reopened = _store(tmp_path)
    assert [r["id"] for r in reopened.pending_midflight_requests("Save1")] == ["m-1", "m-2"]


def test_mark_midflight_consumed_is_persistent_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_request("Save1", "m-1", "顺便浇一下后面的地", origin="player-midflight")
    store.record_request("Save1", "m-2", "改:先收菜", origin="player-midflight")

    store.mark_midflight_consumed("Save1", ["m-1"])
    assert [r["id"] for r in store.pending_midflight_requests("Save1")] == ["m-2"]
    store.mark_midflight_consumed("Save1", ["m-1", "missing"])
    store.mark_midflight_consumed("Save1", [])
    assert [r["id"] for r in store.pending_midflight_requests("Save1")] == ["m-2"]

    reopened = _store(tmp_path)
    assert [r["id"] for r in reopened.pending_midflight_requests("Save1")] == ["m-2"]
    record = next(r for r in reopened.state("Save1").requests if r["id"] == "m-1")
    assert record["consumedByDelivery"] is True


def test_unconsumed_midflight_request_survives_request_trimming(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_request("Save1", "m-2", "新的改目标", origin="player-midflight")
    for index in range(60):
        store.record_request("Save1", f"f-{index}", f"请求 {index}")
    requests = _store(tmp_path).state("Save1").requests
    assert len(requests) == 50
    assert "m-2" in {r["id"] for r in requests}


def test_legacy_state_without_request_and_outbox_sections(tmp_path: Path) -> None:
    path = tmp_path / "data" / "work-state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"Save1": {"paused": False, "goals": []}}), encoding="utf-8")

    store = WorkStore(path)
    assert store.pending_outbox("Save1") == []
    assert store.pending_midflight_requests("Save1") == []
    assert store.find_request_for_task("Save1", "task-1") is None
    record = store.record_request("Save1", "req-old", "旧存档新请求")
    assert record["origin"] == "player"
    stored = store.append_outbox_event("Save1", _event("cmd-old", "completed"))
    assert _store(tmp_path).pending_outbox("Save1")[0]["id"] == stored["id"]
