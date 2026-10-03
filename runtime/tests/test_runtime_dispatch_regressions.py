"""Regressions from the live 2026-10-03 dispatch/session trace; no provider calls."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.agent_backends import McodeBackend
from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.companion_profile import CompanionProfileStore
from stardew_ai_runtime.plan_executor import StepExecution
from stardew_ai_runtime.protocol import Envelope


def snapshot(*, stamina=2, water=0, rest="awake", revision=1):
    return Envelope.from_mapping({
        "protocolVersion": "0.1", "messageType": "world.snapshot", "messageId": f"snapshot-{revision}",
        "senderInstanceId": "game", "sequenceNumber": revision, "worldRevision": revision,
        "sentAt": "2026-10-03T04:00:30Z", "saveId": "save",
        "payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 16, "timeOfDay": 600},
                    "companion": {"stamina": stamina, "waterCanLevel": water, "restState": rest}}})


def bridge_for_work(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._autonomy.set_enabled("save", True)
    bridge._execute_turn = MagicMock(return_value={
        "success": True, "response": "先等等。", "conversation_id": "work-session"})
    return bridge


def wire_payloads(ws):
    return [json.loads(call.args[0])["payload"] for call in ws.send_text.call_args_list]


async def finish_tracked(tracked):
    while tracked:
        await asyncio.gather(*list(tracked))


def test_busy_wakeup_waits_without_consuming_fingerprint_and_resumes_without_snapshot(tmp_path):
    async def run():
        bridge = bridge_for_work(tmp_path)
        ws = MagicMock(send_text=AsyncMock())
        tracked = set()
        envelope = snapshot()
        await bridge._maybe_schedule_autonomy(envelope, "save", tracked, ws, force=True)
        # Reproduce the care race: the created dispatcher has not run yet.
        await bridge._busy_lock.acquire()
        await asyncio.sleep(0)
        for _ in range(10):
            await bridge._maybe_schedule_autonomy(envelope, "save", tracked, ws, force=True)
        assert len(tracked) == 1
        assert bridge._autonomy.state("save").last_decision_fingerprint is None
        bridge._execute_turn.assert_not_called()
        bridge._busy_lock.release()
        await finish_tracked(tracked)
        assert bridge._execute_turn.call_count == 1
        assert bridge._autonomy.state("save").last_decision_fingerprint
        assert all(row.get("error") != "BUSY_CONCURRENT_COMMAND" for row in wire_payloads(ws))
        await bridge._maybe_schedule_autonomy(envelope, "save", tracked, ws, force=True)
        await finish_tracked(tracked)
        assert bridge._execute_turn.call_count == 1
    asyncio.run(run())


def test_waiting_wakeup_rechecks_pause_and_latest_resources(tmp_path):
    async def run():
        bridge = bridge_for_work(tmp_path)
        tracked = set()
        await bridge._busy_lock.acquire()
        await bridge._maybe_schedule_autonomy(snapshot(), "save", tracked, None, force=True)
        await bridge._maybe_schedule_autonomy(snapshot(stamina=15, water=40, revision=2), "save", tracked, None, force=True)
        bridge._autonomy.set_paused("save", True)
        bridge._busy_lock.release()
        await finish_tracked(tracked)
        bridge._execute_turn.assert_not_called()
        assert bridge._autonomy.state("save").last_decision_fingerprint is None
        bridge._autonomy.set_paused("save", False)
        await bridge._busy_lock.acquire()
        await bridge._maybe_schedule_autonomy(snapshot(), "save", tracked, None, force=True)
        await bridge._maybe_schedule_autonomy(snapshot(stamina=15, water=40, revision=2), "save", tracked, None, force=True)
        bridge._busy_lock.release()
        await finish_tracked(tracked)
        prompt = bridge._execute_turn.call_args.args[2]
        assert '"current":15' in prompt and '"level":40' in prompt
        assert bridge._autonomy.state("save").last_attempt_revision == 2
    asyncio.run(run())


def test_morning_care_waits_for_already_scheduled_work(tmp_path):
    async def run():
        bridge = bridge_for_work(tmp_path)
        order = []
        def work(*args):
            order.append("work")
            return {"success": True, "response": "待命。", "conversation_id": "work-session"}
        async def care(*args):
            order.append("care")
            return {"success": True, "response": "早上好。", "conversation_id": "life-session"}
        bridge._execute_turn.side_effect = work
        bridge._await_life_provider = care
        tracked = set()
        # The wire reader creates care first, then schedules the morning work.
        care_task = asyncio.create_task(bridge._generate_care_text(
            "save", "morning", "1:spring:16", {"companionName": "小星"}, None, {}))
        await bridge._maybe_schedule_autonomy(snapshot(), "save", tracked, None, force=True)
        await asyncio.gather(care_task, *list(tracked))
        assert order == ["work", "care"]
        assert bridge._life_chat.get_session_id("save") == "life-session"
    asyncio.run(run())


def test_queued_wakeup_rechecks_native_daytime_rest_before_dispatch(tmp_path):
    async def run():
        bridge = bridge_for_work(tmp_path)
        tracked = set()
        await bridge._busy_lock.acquire()
        await bridge._maybe_schedule_autonomy(snapshot(), "save", tracked, None, force=True)
        await bridge._maybe_schedule_autonomy(snapshot(rest="resting"), "save", tracked, None, force=True)
        bridge._busy_lock.release()
        await finish_tracked(tracked)
        bridge._execute_turn.assert_not_called()
        assert bridge._autonomy.state("save").last_decision_fingerprint is None
        await bridge._maybe_schedule_autonomy(snapshot(stamina=20), "save", tracked, None, force=True)
        await finish_tracked(tracked)
        assert bridge._execute_turn.call_count == 1
    asyncio.run(run())


def test_cancelled_work_keeps_model_slot_until_executor_restores_state(tmp_path):
    async def run():
        bridge = bridge_for_work(tmp_path)
        started, release, restored = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def provider(active, *args):
            calls.append(active.request_id)
            if active.request_id == "chain-interrupted":
                started.set()
                assert release.wait(3)
                restored.set()  # models the backend's environment restore/finally
            else:
                assert restored.is_set(), "new work overlapped the interrupted provider"
            return {"success": True, "response": "待命。", "conversation_id": "work-session"}
        bridge._execute_turn.side_effect = provider
        first = asyncio.create_task(bridge.handle_chat_submit(None, "chain-interrupted", "执行", "save"))
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        tracked = set()
        await bridge._maybe_schedule_autonomy(snapshot(), "save", tracked, None, force=True)
        await asyncio.sleep(0)
        assert bridge._busy_lock.locked()
        assert bridge._autonomy.state("save").last_decision_fingerprint is None
        first.cancel()  # a second channel shutdown must not release ownership either
        await asyncio.sleep(0)
        assert bridge._busy_lock.locked()
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await finish_tracked(tracked)
        assert len(calls) == 2
        assert not bridge._busy_lock.locked()
    asyncio.run(run())


@pytest.mark.parametrize("job_selected", [False, True])
def test_disconnected_autonomy_rearms_only_when_no_native_job_was_selected(tmp_path, job_selected):
    async def run():
        bridge = bridge_for_work(tmp_path)
        bridge._snapshot_care_hooks = AsyncMock()
        started, release = threading.Event(), threading.Event()
        disconnected = asyncio.Event()
        envelope = snapshot()

        def provider(active, *args):
            bridge._work_store.begin_decision("save", "disconnected-choice")
            if job_selected:
                bridge._work_store.submit_plan("save", goal_text="浇地", decision_token="disconnected-choice", tasks=[{
                    "title": "给水壶补水", "steps": [{"operation": "refill_watering_can", "params": {}}]}])
            started.set()
            assert release.wait(3)
            return {"success": True, "response": "待命。", "conversation_id": "work-session"}

        bridge._execute_turn.side_effect = provider
        ws = MagicMock(send_text=AsyncMock())
        reads = 0

        async def receive_text(timeout):
            nonlocal reads
            reads += 1
            if reads == 1:
                return json.dumps(envelope.to_mapping())
            await disconnected.wait()
            raise ConnectionError("chat websocket disconnected")

        ws.receive_text = receive_text
        reader = asyncio.create_task(bridge._receive_loop(ws, "save"))
        assert await asyncio.to_thread(started.wait, 1)
        consumed = bridge._autonomy.state("save").last_decision_fingerprint
        selected_before = dict(bridge._work_store.state("save").decision)
        assert consumed
        disconnected.set()
        await asyncio.sleep(0)
        assert bridge._active_task.cancelled
        release.set()
        with pytest.raises(ConnectionError, match="chat websocket disconnected"):
            await reader
        assert not bridge._busy_lock.locked()
        assert not bridge._autonomy_wakeup_tasks and not bridge._autonomy_requests

        # Reopen the persisted stores as a reconnect/restarted bridge would.
        reconnected = bridge_for_work(tmp_path)
        tracked = set()
        await reconnected._maybe_schedule_autonomy(envelope, "save", tracked, None, force=True)
        await finish_tracked(tracked)
        await reconnected._maybe_schedule_autonomy(envelope, "save", tracked, None, force=True)
        await finish_tracked(tracked)
        if job_selected:
            reconnected._execute_turn.assert_not_called()
            assert reconnected._autonomy.state("save").last_decision_fingerprint == consumed
            state = reconnected._work_store.state("save")
            assert state.decision == selected_before
            assert len(state.tasks) == 1 and state.tasks[0].id == selected_before["taskId"]
            assert state.tasks[0].steps[0].attempts == 0
        else:
            assert reconnected._execute_turn.call_count == 1
            assert reconnected._autonomy.state("save").last_decision_fingerprint != consumed
    asyncio.run(run())


def test_care_and_player_chat_share_persisted_session_and_refresh_corrected_memory(tmp_path):
    async def run():
        bridge = bridge_for_work(tmp_path)
        bridge._profile_store.set("save", {"onboarded": True}, 0)
        bridge._await_life_provider = AsyncMock(return_value={
            "success": True, "response": "早上好。", "conversation_id": "life-session"})
        await bridge._generate_care_text("save", "morning", "1:spring:16", {}, None, {})
        assert bridge._await_life_provider.call_args.args[1] is None
        reloaded = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
        reloaded._await_life_provider = AsyncMock(return_value={
            "success": True, "response": "今天也一起过。", "conversation_id": "life-session"})
        reloaded._memory_store.add("save", kind="event", text="完成了补水", source="system",
                                   game_date="1:spring:16", expected_revision=0, command_id="refill-1")
        await reloaded._generate_care_text("save", "evening", "1:spring:16", {}, None, {})
        assert reloaded._await_life_provider.call_args.args[1] == "life-session"
        assert "完成了补水" in reloaded._await_life_provider.call_args.args[2]
        await reloaded._run_life_chat_turn(None, {"request_id": "player-one", "save_id": "save", "text": "你好"})
        assert reloaded._await_life_provider.call_args.args[1] == "life-session"
        _, agreement = reloaded._memory_store.add("save", kind="agreement", text="保留树木", source="player",
                                                  game_date="1:spring:16", expected_revision=1)
        await reloaded._run_life_chat_turn(None, {"request_id": "player-two", "save_id": "save", "text": "继续聊"})
        assert reloaded._await_life_provider.call_args.args[1] is None
        assert "保留树木" in reloaded._await_life_provider.call_args.args[2]
        reloaded._memory_store.delete("save", agreement["entry"]["id"], expected_revision=2)
        await reloaded._run_life_chat_turn(None, {"request_id": "player-three", "save_id": "save", "text": "继续聊"})
        assert reloaded._await_life_provider.call_args.args[1] is None
        assert "保留树木" not in reloaded._await_life_provider.call_args.args[2]
    asyncio.run(run())


def test_profile_edit_during_life_turn_rotates_next_context(tmp_path):
    async def run():
        bridge = bridge_for_work(tmp_path)
        bridge._profile_store.set("save", {"onboarded": True}, 0)
        async def provider(*args):
            bridge._profile_store.set("save", {"personality": "calm"}, 1)
            return {"success": True, "response": "你好。", "conversation_id": "old-profile-session"}
        bridge._await_life_provider = provider
        await bridge._run_life_chat_turn(None, {"request_id": "chat-one", "save_id": "save", "text": "你好"})
        assert bridge._life_chat.rotate_if_needed("save", 2, 0) is None
    asyncio.run(run())


def test_noop_profile_save_preserves_revision_and_life_session(tmp_path):
    bridge = bridge_for_work(tmp_path)
    store = bridge._profile_store
    store.set("save", {"onboarded": True, "bedtime": 2400}, 0)
    bridge._life_chat.rotate_if_needed("save", 1, 0)
    bridge._life_chat.record_session_id("save", "same-session")
    status, result = store.set("save", {"onboarded": True, "bedtime": 0, "companionName": " 阿星 "}, 1)
    assert status == "confirmed" and result["profileRevision"] == 1
    reopened = CompanionProfileStore(store.state_path)
    assert reopened.get("save")["profileRevision"] == 1
    assert bridge._life_chat.rotate_if_needed("save", bridge._profile_revision("save"), 0) == "same-session"


@pytest.mark.parametrize("provider_success", [True, False])
def test_job_selection_hides_premature_model_result_and_keeps_native_feedback(tmp_path, provider_success):
    async def run():
        bridge = bridge_for_work(tmp_path)
        store = bridge._work_store
        ws = MagicMock(send_text=AsyncMock())
        def select(*args):
            store.begin_decision("save", "refill-choice")
            store.submit_plan("save", goal_text="浇地", decision_token="refill-choice", tasks=[{
                "title": "给水壶补水", "steps": [{"operation": "refill_watering_can", "params": {}}]}])
            return {"success": provider_success, "response": "补水失败，没办法继续。", "conversation_id": "work-session"}
        bridge._execute_turn.side_effect = select
        await bridge.handle_chat_submit(ws, "chain-refill", "补水", "save")
        selected = next(row for row in wire_payloads(ws) if row.get("status") == "selected")
        assert selected["replyText"] == "已选择「给水壶补水」，等待原生执行；结果尚未确认。"
        assert selected["commandComplete"] is False
        assert not store.state("save").last_job
        task_id = store.state("save").decision["taskId"]
        store.finish_job("save", {"status": "completed", "effects": [{"kind": "water-refilled", "waterAfter": 40}]}, task_id=task_id)
        await bridge._publish_job_progress(StepExecution(
            status="executed", task_id=task_id, task_status="completed", operation="refill_watering_can",
            outcome="completed", message="水量已恢复到40。"))
        terminal = next(row for row in wire_payloads(ws) if row.get("status") == "job-completed")
        assert "水量已恢复到40" in terminal["replyText"]
        assert terminal["commandComplete"] is True
        tracked = set()
        await bridge._maybe_schedule_autonomy(snapshot(stamina=15, water=40), "save", tracked, None, force=True)
        await finish_tracked(tracked)
        assert '"lastResult":{"status":"completed"' in bridge._execute_turn.call_args.args[2]
        assert '"waterAfter":40' in bridge._execute_turn.call_args.args[2]
    asyncio.run(run())


def test_resource_bands_wake_once_and_daytime_rest_releases_on_awake(tmp_path):
    async def run():
        bridge = bridge_for_work(tmp_path)
        tracked = set()
        async def tick(envelope):
            await bridge._maybe_schedule_autonomy(envelope, "save", tracked, None, force=True)
            await finish_tracked(tracked)
        await tick(snapshot())
        assert bridge._execute_turn.call_count == 1
        for stamina in (3, 5, 8, 9):
            envelope = snapshot(stamina=stamina)
            envelope.payload["world"]["timeOfDay"] = 610
            envelope.payload["companion"].update(tileX=stamina, tileY=1)
            await tick(envelope)
        assert bridge._execute_turn.call_count == 1
        await tick(snapshot(stamina=15))
        await tick(snapshot(stamina=16))
        assert bridge._execute_turn.call_count == 2
        await tick(snapshot(stamina=16, water=40))
        await tick(snapshot(stamina=16, water=39))
        assert bridge._execute_turn.call_count == 3
        await tick(snapshot(stamina=1, water=39, rest="resting"))
        await tick(snapshot(stamina=20, water=39, rest="resting"))
        assert bridge._execute_turn.call_count == 3
        await tick(snapshot(stamina=20, water=39, rest="awake"))
        # Rest transitions are meaningful even if recovery stays within a band.
        assert bridge._execute_turn.call_count == 4
        await tick(snapshot(stamina=20, water=39, rest="awake"))
        assert bridge._execute_turn.call_count == 4
    asyncio.run(run())


def test_mcode_exec_usage_does_not_guess_model_request_count():
    usage = McodeBackend._usage({"inputTokens": 20, "outputTokens": 3, "cacheReadTokens": 10, "totalTokens": 23})
    assert usage["total_tokens"] == 33
    assert "generations_count" not in usage
