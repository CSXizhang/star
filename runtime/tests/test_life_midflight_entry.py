"""Production life-submit entry preserves B before the single-model queue is drained."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge


@pytest.mark.parametrize("paused", [False, True])
def test_life_submit_persists_midflight_request_before_queued_turn(tmp_path, paused):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        store = bridge._work_store
        store.record_request("farm", "A", "Harvest first")
        store.begin_decision("farm", "token-A", request_id="A")
        store.submit_plan("farm", decision_token="token-A", goal_text="Harvest", tasks=[{
            "id": "job-A", "title": "Harvest", "steps": [{"id": "step-A", "operation": "harvest_auto"}],
        }])
        store.set_paused("farm", paused)
        bridge._active_player_requests["farm"] = "A"
        bridge._await_life_provider = AsyncMock(return_value={"success": True, "response": "After A, store two."})
        ws = AsyncMock()
        text = "Continue A. After A settles, put two parsnips in the same chest."
        await bridge._busy_lock.acquire()
        await bridge._handle_life_message(ws, "life.chat.submit", {
            "payload": {"requestId": "B", "saveId": "farm", "mode": "chat", "text": text},
        }, "farm")
        pending = store.pending_midflight_requests("farm")
        assert [(r["id"], r["origin"], r["text"]) for r in pending] == [("B", "player-midflight", text)]
        assert len(bridge._life_queue) == 1
        bridge._await_life_provider.assert_not_called()
        bridge._busy_lock.release()
        await bridge._drain_life_queue(ws)
        active = bridge._await_life_provider.call_args.args[0]
        assert active.read_only is True
        assert store.pending_midflight_requests("farm")[0]["text"] == text
        assert bridge._active_player_requests["farm"] == "A"
        state = store.state("farm")
        assert state.decision["taskId"] == "job-A"
        assert state.tasks[0].status == "pending"
        assert not bridge._execution_lock.locked()
    asyncio.run(run())
