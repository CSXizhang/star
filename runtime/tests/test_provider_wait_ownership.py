"""A cancelled await cannot release a provider still restoring turn state."""
import asyncio
import threading
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "mailbox_error"])
def test_provider_wait_settles_after_executor_exits(tmp_path, outcome):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        bridge._apply_life_intent = AsyncMock()
        if outcome == "mailbox_error":
            bridge._apply_life_intent.side_effect = OSError("mailbox unavailable")
        bridge._settle_turn_messages = AsyncMock()
        started, release, exited = threading.Event(), threading.Event(), threading.Event()

        @asynccontextmanager
        async def pause(*args):
            yield

        def provider(*args):
            started.set()
            try:
                assert release.wait(5)
                if outcome == "error":
                    raise RuntimeError("provider failed")
                return {"success": True}
            finally:
                exited.set()

        bridge._thinking_pause = pause
        bridge._execute_turn = provider
        bridge._execute_life_turn = provider
        active = ActiveChatTask("turn", "farm")
        wait = bridge._await_life_provider if outcome == "cancel" else bridge._await_work_provider
        task = asyncio.create_task(wait(active, "original-session", "prompt"))
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            if outcome == "cancel":
                task.cancel()
                await asyncio.sleep(0.05)
                assert not task.done()
                bridge._settle_turn_messages.assert_not_awaited()
            if outcome == "mailbox_error":
                await asyncio.sleep(.15)
                assert active.cancelled and not task.done()
                bridge._settle_turn_messages.assert_not_awaited()
            release.set()
            if outcome == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "error":
                with pytest.raises(RuntimeError, match="provider failed"):
                    await task
            elif outcome == "mailbox_error":
                with pytest.raises(OSError, match="mailbox unavailable"):
                    await task
            else:
                assert await task == {"success": True}
            assert exited.is_set()
            bridge._settle_turn_messages.assert_awaited_once()
        finally:
            release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
