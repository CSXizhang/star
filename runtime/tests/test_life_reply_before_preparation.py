"""Player dialogue must be delivered before a second model turn starts work."""
import asyncio

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge


def test_reply_arrives_while_preparation_is_still_waiting(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        preparing, release = asyncio.Event(), asyncio.Event()
        replies = []

        async def send(ws, envelope):
            if envelope.message_type == "life.chat.reply":
                replies.append(envelope.payload)
            return True

        async def provider(*args):
            return {"success": True, "response": "安排记好了。"}

        async def prepare(*args):
            preparing.set()
            await release.wait()

        bridge._send_reply = send
        bridge._await_life_provider = provider
        bridge._start_accepted_preparation = prepare
        task = asyncio.create_task(bridge._handle_life_chat_submit(None, {
            "requestId": "one", "saveId": "S", "text": "浇这片地", "mode": "chat",
        }, "S"))
        try:
            await asyncio.wait_for(preparing.wait(), 2)
            assert not task.done()
            assert any(r.get("status") == "completed" and r.get("replyText") == "安排记好了。"
                       for r in replies)
        finally:
            release.set()
            await task
    asyncio.run(run())


def test_deferred_preparation_survives_pause_and_snapshot_wakeup(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        bridge._latest_snapshot_payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 2, "timeOfDay": 900}}
        before = bridge._accepted_preparation_state("S")
        node = bridge._milestone_store.propose("S", title="浇水", target_date="1:spring:2", preparation=["water"])
        bridge._milestone_store.adopt("S", node["id"], work_store=bridge._work_store)
        bridge._work_store.set_paused("S", True)
        bridge._pending_life_preparation["S"] = before
        started = []

        async def work(*args, **kwargs):
            started.append(args[1])

        bridge.handle_chat_submit = work
        await asyncio.wait_for(bridge._drain_life_queue(None), 2)
        assert bridge._pending_life_preparation["S"] == before
        assert not bridge._life_draining and not started
        bridge._work_store.set_paused("S", False)
        bridge._schedule_life_drain(None)
        bridge._schedule_life_drain(None)
        await asyncio.gather(*bridge._life_drain_tasks)
        assert len(started) == 1
        assert not bridge._pending_life_preparation and not bridge._life_draining
    asyncio.run(run())


def test_preparation_failure_is_reported_after_acknowledgement(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        replies = []

        async def send(ws, envelope):
            replies.append(envelope)
            return True

        async def provider(*args):
            return {"success": True, "response": "安排记好了。"}

        async def prepare(*args):
            raise RuntimeError("preparation unavailable")

        bridge._send_reply = send
        bridge._await_life_provider = provider
        bridge._start_accepted_preparation = prepare
        await bridge._handle_life_chat_submit(None, {
            "requestId": "one", "saveId": "S", "text": "浇这片地", "mode": "chat",
        }, "S")
        assert any(r.message_type == "life.chat.reply" and r.payload.get("status") == "completed"
                   for r in replies)
        assert replies[-1].message_type == "chat.reply"
        assert replies[-1].payload["status"] == "failed"
        assert not bridge._pending_life_preparation and not bridge._life_draining
    asyncio.run(run())


@pytest.mark.parametrize("defer", [False, True])
def test_queued_player_change_is_read_before_preparing_accepted_work(tmp_path, defer):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        bridge._latest_snapshot_payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 2}}
        events = []
        node_id = None

        async def send(ws, envelope):
            if envelope.message_type == "life.chat.reply" and envelope.payload.get("status") == "completed":
                events.append(envelope.payload["requestId"])
            return True

        async def provider(task, *args):
            nonlocal node_id
            if task.request_id == "one":
                node = bridge._milestone_store.propose("S", title="浇水", target_date="1:spring:2", preparation=["water"])
                node_id = node["id"]
                bridge._milestone_store.adopt("S", node_id, work_store=bridge._work_store)
                await bridge._handle_life_chat_submit(None, {
                    "requestId": "two", "saveId": "S", "text": "换个安排，我们先聊聊", "mode": "chat",
                }, "S")
            elif defer:
                bridge._milestone_store.defer("S", node_id, work_store=bridge._work_store)
            return {"success": True, "response": "好。"}

        async def native_work(*args, **kwargs):
            events.append("work")

        bridge._send_reply = send
        bridge._await_life_provider = provider
        bridge.handle_chat_submit = native_work
        await bridge._handle_life_chat_submit(None, {
            "requestId": "one", "saveId": "S", "text": "浇这片地", "mode": "chat",
        }, "S")
        assert events == (["one", "two"] if defer else ["one", "two", "work"])
        assert not bridge._life_queue and not bridge._pending_life_preparation
    asyncio.run(run())
