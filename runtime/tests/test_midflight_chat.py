"""Discriminating tests for player chat while a native job owns the connection.

While a selected job is still in flight, the execution connection stays with
the native executor: a player message is recorded verbatim
(origin ``player-midflight``) and answered by a read-only life-surface turn
that never claims the execution connection and cannot dispatch work.  Whole
work-control utterances (the same vocabulary as the life surface) reach the
in-flight command through the autonomy-control bypass immediately.  Only
``preparation-`` turns (bridge-scheduled accepted preparations) keep the
regular claiming path.

Fake clients/WebSockets and isolated ``tmp_path`` run dirs only; no real
provider process, save file, or game is touched.
"""

import asyncio
import json
import os
from unittest.mock import MagicMock

from stardew_ai_runtime.chat_bridge import ChatBridge


class FakeClient:
    """Internal plan client double; records closes and native dispatches."""

    def __init__(self, save_id="Save1"):
        self.save_id = save_id
        self.dispatched = []
        self.closed = 0

    async def current_save_id(self):
        return self.save_id

    async def call_tool(self, name, params):
        if name == "dispatch_plan_operation":
            self.dispatched.append(params.get("operation"))
            return {"status": "completed", "effects": [{"kind": "done"}]}
        return {"status": "completed"}

    async def close(self):
        self.closed += 1


class FakeWs:
    """Chat-channel double recording every wire payload."""

    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)

    def replies(self):
        return [json.loads(m)["payload"] for m in self.sent
                if json.loads(m).get("messageType") == "chat.reply"]


def bridge_with_inflight_job(tmp_path, monkeypatch):
    """A bridge whose save has a selected, unfinished short job (in flight)."""
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    client = FakeClient()
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False,
                        internal_plan_client=client)
    store = bridge._work_store
    store.record_request("Save1", "req-1", "照料农田", origin="player")
    goal = store.add_goal("Save1", "照料农田", source="user")
    store.begin_decision("Save1", "tok-1")
    store.submit_plan("Save1", goal_id=goal.id, decision_token="tok-1", tasks=[{
        "id": "t1", "title": "锄地",
        "steps": [{"id": "s1", "operation": "hoe_tiles", "params": {}}],
    }])
    store.bind_request_task("Save1", "req-1", "t1", goal.id)
    bridge._active_player_requests["Save1"] = "req-1"
    assert bridge._job_in_flight("Save1")
    return bridge, client


def test_midflight_player_chat_is_recorded_and_answered_read_only(tmp_path, monkeypatch):
    bridge, client = bridge_with_inflight_job(tmp_path, monkeypatch)
    ws = FakeWs()
    bridge._chat_ws = ws

    life_turns = []
    original_life = bridge._execute_turn

    def spy_life(active_task, conversation_id, prompt):
        life_turns.append((active_task, prompt))
        return original_life(active_task, conversation_id, prompt)

    bridge._execute_turn = spy_life

    seen = []

    class FakeBackend:
        def run(self, active_task, cid, prompt):
            seen.append({"surface": os.environ.get("STARDEW_MCP_SURFACE"),
                         "token": os.environ.get("STARDEW_DECISION_TOKEN"),
                         "mode": os.environ.get("STARDEW_LIFE_MODE")})
            return {"success": True, "response": "地里正在锄草，稍后汇报。", "conversation_id": "life-1"}

    bridge._backend = FakeBackend()

    async def run():
        await bridge.handle_chat_submit(ws, "req-mid-1", "顺便看看鸡", "Save1")

        # The turn ran on the cached life surface with no decision token.
        assert len(life_turns) == 1
        assert seen == [{"surface": "life", "token": None, "mode": None}]
        active, prompt = life_turns[0]
        assert active.read_only is True
        assert '"workDispatchAvailable": false' in prompt
        assert '"taskId": "t1"' in prompt

        # The original text is persisted as a midflight request.
        recorded = next(r for r in bridge._work_store.state("Save1").requests if r["id"] == "req-mid-1")
        assert recorded["origin"] == "player-midflight"
        assert recorded["text"] == "顺便看看鸡"
        assert [m["id"] for m in bridge._work_store.pending_midflight_requests("Save1")] == ["req-mid-1"]

        # The execution connection was never claimed.
        assert client.closed == 0
        assert client.dispatched == []
        assert bridge._execution_lock.locked() is False
        assert bridge._plan_worker is None or bridge._plan_worker._provider_active is False
        # A midflight chat never replaces the root player request.
        assert bridge._active_player_requests["Save1"] == "req-1"

        final = ws.replies()[-1]
        assert final["status"] == "completed"
        assert final["readOnly"] is True
        assert final["commandId"] == "req-mid-1"

    asyncio.run(run())


def test_midflight_pause_control_bypasses_the_model_turn(tmp_path, monkeypatch):
    bridge, client = bridge_with_inflight_job(tmp_path, monkeypatch)
    ws = FakeWs()
    bridge._chat_ws = ws

    bridge._execute_turn = MagicMock()
    bridge._execute_life_turn = MagicMock()

    controls = []
    original_control = bridge._handle_autonomy_control

    async def spy_control(ws_, envelope, save_id, **kwargs):
        controls.append(envelope.payload.get("action"))
        return await original_control(ws_, envelope, save_id, **kwargs)

    bridge._handle_autonomy_control = spy_control

    async def run():
        await bridge.handle_chat_submit(ws, "req-mid-pause", "暂停", "Save1")

        # The whole control utterance went straight to the control bypass: no
        # model turn at all, and the work state is paused.
        assert controls == ["pause"]
        bridge._execute_turn.assert_not_called()
        bridge._execute_life_turn.assert_not_called()
        assert bridge._work_store.state("Save1").paused is True
        assert bridge._autonomy.state("Save1").paused is True

        # The bypassed request is consumed immediately and never queued as
        # midflight text for a later delivery.
        assert bridge._work_store.pending_midflight_requests("Save1") == []
        recorded = next(r for r in bridge._work_store.state("Save1").requests if r["id"] == "req-mid-pause")
        assert recorded["origin"] == "player-midflight"
        assert recorded["consumedByDelivery"] is True

        assert client.closed == 0
        assert client.dispatched == []
        final = ws.replies()[-1]
        assert final["status"] == "completed"
        assert final["commandComplete"] is True
        assert final["commandId"] == "req-mid-pause"

    asyncio.run(run())


def test_preparation_prefix_keeps_the_claiming_execution_path(tmp_path, monkeypatch):
    bridge, client = bridge_with_inflight_job(tmp_path, monkeypatch)
    ws = FakeWs()
    bridge._chat_ws = ws

    life_turns = MagicMock()
    bridge._execute_life_turn = life_turns
    seen = []

    class FakeBackend:
        def run(self, active_task, cid, prompt):
            seen.append({"surface": os.environ.get("STARDEW_MCP_SURFACE"),
                         "token": os.environ.get("STARDEW_DECISION_TOKEN")})
            return {"success": True, "response": "准备作业已安排。", "conversation_id": "work-1"}

    bridge._backend = FakeBackend()

    async def run():
        await bridge.handle_chat_submit(ws, "preparation-abc123", "玩家认可的准备", "Save1")

        # Accepted-preparation turns never take the midflight read-only path:
        # they run a regular claiming turn with a fresh decision token.
        life_turns.assert_not_called()
        assert len(seen) == 1
        assert seen[0]["surface"] == "light"
        assert seen[0]["token"]
        recorded = next(r for r in bridge._work_store.state("Save1").requests
                        if r["id"] == "preparation-abc123")
        assert recorded["origin"] == "player"
        assert bridge._active_player_requests["Save1"] == "preparation-abc123"
        # The superseded in-flight decision was revoked through the normal
        # preemption path (it had never been dispatched, so no native cancel).
        assert not bridge._work_store.state("Save1").decision.get("selected")

    asyncio.run(run())
