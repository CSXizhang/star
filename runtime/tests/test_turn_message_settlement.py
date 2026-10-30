"""Confirmed tool-boundary inputs must not become another player turn."""
import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.turn_messages import exchange
from stardew_ai_runtime.work_state import WorkStore


@pytest.mark.parametrize("success", [True, False])
def test_settlement_consumes_only_delivered_messages_even_after_turn_failure(tmp_path, success):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._send_reply = AsyncMock()
    path = tmp_path / "data/turn-messages.json"
    for request_id in ("delivered", "pending"):
        bridge._work_store.record_request("farm", request_id, request_id, origin="player-midflight")
        bridge._life_queue.append({"request_id": request_id, "save_id": "farm"})
    exchange(path, "turn", "farm", publish=[{"requestId": "delivered", "text": "stop selling"}])
    exchange(path, "turn", "farm", take=True)
    exchange(path, "turn", "farm", publish=[{"requestId": "pending", "text": "water plants"}])
    result = {"success": success, "response": ""}
    assert asyncio.run(bridge._settle_turn_messages(ActiveChatTask("turn", "farm"), result)) is result
    assert [item["request_id"] for item in bridge._life_queue] == ["pending"]
    reopened = WorkStore(tmp_path / "data/work-state.json")
    assert [item["id"] for item in reopened.pending_midflight_requests("farm")] == ["pending"]
    bridge._send_reply.assert_awaited_once()
    asyncio.run(bridge._settle_turn_messages(ActiveChatTask("turn", "farm"), result))
    bridge._send_reply.assert_awaited_once()


@pytest.mark.parametrize("interruption", ["error", "cancel"])
def test_delivered_messages_settle_when_provider_does_not_return(tmp_path, interruption):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._send_reply = AsyncMock()
    bridge._apply_life_intent = AsyncMock()
    bridge._work_store.record_request("farm", "message", "message", origin="player-midflight")
    bridge._life_queue.append({"request_id": "message", "save_id": "farm"})
    path = tmp_path / "data/turn-messages.json"
    exchange(path, "turn", "farm", publish=[{"requestId": "message", "text": "stop selling"}])
    delivered = threading.Event()
    release = threading.Event()

    @asynccontextmanager
    async def no_pause(*args):
        yield

    def provider(*args):
        exchange(path, "turn", "farm", take=True)
        delivered.set()
        if interruption == "error":
            raise RuntimeError("provider disconnected after tool result")
        assert release.wait(5)
        return {"success": False}

    bridge._thinking_pause = no_pause
    bridge._execute_turn = provider
    bridge._execute_life_turn = provider

    async def run():
        active = ActiveChatTask("turn", "farm")
        if interruption == "error":
            with pytest.raises(RuntimeError, match="provider disconnected"):
                await bridge._await_work_provider(active, None, "prompt")
        else:
            task = asyncio.create_task(bridge._await_life_provider(active, None, "prompt"))
            try:
                async with asyncio.timeout(5):
                    while not delivered.is_set():
                        await asyncio.sleep(0.01)
                task.cancel()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not bridge._work_store.pending_midflight_requests("farm")
        assert not bridge._life_queue

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["mcode", "codex"])
def test_cancel_entire_life_turn_records_once_closes_ui_and_reuses_session(tmp_path, backend):
    """F8 must settle the original life request, not merely its provider await."""
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, backend=backend, enable_plan_worker=False)
        bridge._life_chat.record_session_id("farm", "original-session")
        bridge._interrupt_short_job = AsyncMock(return_value=True)
        bridge._apply_life_intent = AsyncMock()
        started, killed, exited = threading.Event(), threading.Event(), threading.Event()
        bridge._backend = SimpleNamespace(terminate=lambda proc: killed.set())
        calls, replies = [], []

        @asynccontextmanager
        async def no_pause(*args):
            yield

        async def send(ws, reply):
            replies.append(reply)
            return True

        def model(active, session, prompt, mode):
            calls.append((active.request_id, session))
            if active.request_id == "rest":
                active.process = SimpleNamespace(poll=lambda: None)
                started.set()
                assert killed.wait(5)
                assert active.cancelled
                exited.set()
                return {"success": False, "conversation_id": session,
                        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
            assert exited.is_set()
            return {"success": True, "conversation_id": session, "response": "浇水完成。"}

        bridge._thinking_pause = no_pause
        bridge._send_reply = send
        bridge._execute_life_turn = model
        task = asyncio.create_task(bridge._run_life_chat_turn(None, {
            "request_id": "rest", "save_id": "farm", "text": "休息到80"}))
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(.01)
            assert bridge._busy_lock.locked()
            assert await bridge.handle_chat_cancel(None, None, "F8 cancel", "farm")
            with pytest.raises(asyncio.CancelledError):
                await task
            assert exited.is_set()
            assert not bridge._busy_lock.locked() and not bridge._execution_lock.locked()
            assert bridge._active_task is None
            terminals = [r.payload for r in replies if r.message_type == "life.chat.reply"
                         and r.payload.get("requestId") == "rest"
                         and r.payload.get("status") in {"completed", "failed"}]
            assert len(terminals) == 1
            assert terminals[0]["status"] == "completed"
            assert "取消" in terminals[0]["replyText"] and not terminals[0].get("error")
            await bridge._run_life_chat_turn(None, {
                "request_id": "water", "save_id": "farm", "text": "现在浇水"})
            assert calls == [("rest", "original-session"), ("water", "original-session")]
            records = [json.loads(line) for line in bridge._commands_file.read_text(encoding="utf-8").splitlines()]
            assert [(r["requestId"], r["status"]) for r in records] == [("rest", "cancelled"), ("water", "completed")]
            assert records[0]["error"] == "PLAYER_CANCELLED"
            assert records[0]["conversationId"] == "original-session"
            assert records[0]["usage"]["total_tokens"] == 12
        finally:
            killed.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
