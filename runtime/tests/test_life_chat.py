"""Life-chat service and ChatBridge life.* wiring tests (contract §1/§2/§4)."""
import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.life_chat import (
    LifeChatService,
    _life_session_key,
    make_life_env,
)
from stardew_ai_runtime.mcp_server import LIFE_TOOLS, create_mcp_server
from stardew_ai_runtime.plan_executor import (
    StepExecution,
    classify_step_outcome,
    normalise_native_result,
)


def _bridge(tmp_path) -> ChatBridge:
    return ChatBridge(run_dir=tmp_path, enable_plan_worker=False)


def _sent_payloads(ws) -> list[dict]:
    return [json.loads(call[0][0]) for call in ws.send_text.call_args_list]


# ---------------------------------------------------------------- life_chat unit
def test_life_session_key_namespaced() -> None:
    assert _life_session_key("kimi", "Save1") == "life:kimi:Save1"
    assert not _life_session_key("kimi", "Save1").startswith("kimi:Save1")


def test_make_life_env_injects_surface_and_strips_token(monkeypatch) -> None:
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "work-token")
    monkeypatch.setenv("STARDEW_MCP_SURFACE", "internal")
    env = make_life_env({})
    assert env["STARDEW_MCP_SURFACE"] == "life"
    assert "STARDEW_DECISION_TOKEN" not in env


def test_life_session_rotation_on_revision_change(tmp_path) -> None:
    sessions: dict[str, str] = {}
    fingerprints: dict[str, str] = {}
    svc = LifeChatService(
        backend_name="kimi",
        sessions=sessions,
        sessions_file=tmp_path / "chat_sessions.json",
        fingerprints=fingerprints,
        fingerprints_file=tmp_path / "life_fp.json",
    )
    assert svc.rotate_if_needed("Save1", 0, 0) is None
    svc.record_session_id("Save1", "cid-1")
    assert svc.rotate_if_needed("Save1", 0, 0) == "cid-1"
    # Any profile/memory revision change rotates the life session (§2/§4).
    assert svc.rotate_if_needed("Save1", 1, 0) is None
    assert svc.get_session_id("Save1") is None


def test_life_system_prompt_contains_profile_memory_and_work() -> None:
    profile = {"companionName": "小星", "personality": "tsundere", "playStyle": "earn",
               "careFrequency": "moderate", "onboarded": True, "skipped": False}
    memory = {"agreements": [{"text": "每天浇水", "gameDate": "1:spring:1"}],
              "preferences": [], "recentEvents": [{"text": "完成了「浇水」", "gameDate": "1:spring:2"}]}
    work = {"mode": "free", "goal": "优先赚钱"}
    prompt = LifeChatService.build_system_prompt(profile, memory, work, mode="chat")
    assert "小星" in prompt
    assert "嘴硬心软" in prompt
    assert "每天浇水" in prompt
    assert "完成了「浇水」" in prompt
    assert "优先赚钱" in prompt
    assert "不能派工、取消或暂停工作" in prompt
    assert "绝不能说任务已执行、已取消、已安排" in prompt
    assert "帮我做件事" in prompt
    assert "查看记忆" in prompt
    assert "当前工作状态仅供聊天时核对事实" in prompt
    assert "没有显示的状态就是未知" in prompt
    plan_prompt = LifeChatService.build_system_prompt(profile, memory, work, mode="plan")
    assert "不要代为派发" in plan_prompt


# ---------------------------------------------------------------- mcp life surface
def test_mcp_life_surface_is_readonly() -> None:
    async def run() -> None:
        server = create_mcp_server(scheduler=MagicMock(), surface="life")
        tools = {t.name for t in await server.list_tools()}
        assert tools == LIFE_TOOLS
        for forbidden in (
            "submit_plan", "remember_intent", "set_autonomy", "autonomy_status",
            "pause_task", "resume_task", "cancel_task", "call_capability",
            "discover_capabilities", "run_next_step", "dispatch_plan_operation",
            "water_auto", "harvest_auto", "purchase_items", "plant_crop_workflow",
        ):
            assert forbidden not in tools, forbidden
        for expected in (
            "get_work_overview", "get_status", "observe_machines",
            "query_farm_work", "query_wiki",
        ):
            assert expected in tools, expected

    asyncio.run(run())


# ---------------------------------------------------------------- env / no dispatch
def test_life_turn_injects_life_surface_and_never_decision_token(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        captured: dict[str, object] = {}

        class _FakeBackend:
            def run(self, active_task, session_id, prompt):
                captured["surface"] = os.environ.get("STARDEW_MCP_SURFACE")
                captured["token"] = os.environ.get("STARDEW_DECISION_TOKEN")
                captured["session_id"] = session_id
                captured["prompt"] = prompt
                return {"success": True, "response": "早上好呀", "conversation_id": "life-cid-1"}

        monkeypatch.setattr(bridge, "_get_backend", lambda: _FakeBackend())
        begin_decision = MagicMock()
        monkeypatch.setattr(bridge._work_store, "begin_decision", begin_decision)
        monkeypatch.setenv("STARDEW_DECISION_TOKEN", "work-token")
        with patch("stardew_ai_runtime.compatibility.assert_native_compatible"):
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                bridge._execute_life_turn,
                ActiveChatTask(request_id="life-1", save_id="Save1"),
                None,
                "prompt",
            )
        assert result["success"] is True
        assert captured["surface"] == "life"
        assert captured["token"] is None
        assert begin_decision.call_count == 0
        # The surrounding work-token env is restored after the life turn.
        assert os.environ.get("STARDEW_DECISION_TOKEN") == "work-token"
        # Scheduler/autonomy state untouched: no autonomy state file was created.
        assert not (tmp_path / "data" / "autonomy-state.json").exists()

    asyncio.run(run())


# ---------------------------------------------------------------- life chat turn
def test_life_chat_turn_replies_completed_without_token_fields(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        bridge._profile_store.set(
            "Save1", {"onboarded": True, "personality": "gentle"}, expected_revision=0
        )
        ws = AsyncMock()
        with patch.object(
            bridge, "_execute_life_turn",
            return_value={"success": True, "response": "今天过得怎么样？", "conversation_id": "life-cid-9"},
        ) as turn:
            await bridge._handle_life_chat_submit(ws, {
                "payload": {"requestId": "life-req-1", "saveId": "Save1",
                            "mode": "chat", "text": "在吗", "source": "life-menu"},
            }, "Save1")
        assert turn.call_count == 1
        prompt = turn.call_args[0][2]
        assert "玩家说：在吗" in prompt
        replies = _sent_payloads(ws)
        assert [r["payload"]["status"] for r in replies] == ["processing", "completed"]
        final = replies[-1]["payload"]
        assert final["requestId"] == "life-req-1"
        assert final["replyText"] == "今天过得怎么样？"
        assert "tokensUsed" not in final
        assert "conversationId" not in final
        # The life session id is persisted under the life: namespaced key.
        assert bridge._life_chat.get_session_id("Save1") == "life-cid-9"

    asyncio.run(run())


def test_life_chat_turn_failed_model_still_reaches_terminal(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        ws = AsyncMock()
        with patch.object(
            bridge, "_execute_life_turn",
            return_value={"success": False, "response": "", "error": "RESOURCE_EXHAUSTED"},
        ):
            await bridge._handle_life_chat_submit(ws, {
                "payload": {"requestId": "life-req-2", "saveId": "Save1",
                            "mode": "chat", "text": "嗨"},
            }, "Save1")
        replies = _sent_payloads(ws)
        assert replies[-1]["payload"]["status"] == "failed"
        assert replies[-1]["payload"]["error"] == "RESOURCE_EXHAUSTED"

    asyncio.run(run())


def test_life_session_rotates_when_memory_changes(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        ws = AsyncMock()
        seen_cids: list[object] = []

        def _fake_turn(task, cid, prompt):
            seen_cids.append(cid)
            return {"success": True, "response": "好", "conversation_id": f"cid-{len(seen_cids)}"}

        with patch.object(bridge, "_execute_life_turn", side_effect=_fake_turn):
            await bridge._handle_life_chat_submit(ws, {
                "payload": {"requestId": "r1", "saveId": "Save1", "mode": "chat", "text": "你好"},
            }, "Save1")
            assert seen_cids == [None]
            # A memory revision bump (e.g. a new agreement) must rotate the session.
            bridge._memory_store.add("Save1", kind="agreement", text="晚上八点上线",
                                     source="player", game_date="1:spring:1",
                                     expected_revision=0)
            await bridge._handle_life_chat_submit(ws, {
                "payload": {"requestId": "r2", "saveId": "Save1", "mode": "chat", "text": "记住啦"},
            }, "Save1")
            assert seen_cids[1] is None, "life session must rotate on memoryRevision change"

    asyncio.run(run())


# ---------------------------------------------------------------- queueing
def test_life_submit_while_busy_queues_then_drains_to_terminal(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        ws = AsyncMock()
        await bridge._busy_lock.acquire()
        try:
            for i, req in enumerate(["q-1", "q-2"]):
                await bridge._handle_life_chat_submit(ws, {
                    "payload": {"requestId": req, "saveId": "Save1",
                                "mode": "chat", "text": f"消息{i}"},
                }, "Save1")
                queued = _sent_payloads(ws)[-1]["payload"]
                assert queued["status"] == "queued"
                assert queued["queuePosition"] == i + 1
            assert len(bridge._life_queue) == 2
        finally:
            bridge._busy_lock.release()
        with patch.object(
            bridge, "_execute_life_turn",
            side_effect=lambda task, cid, prompt: {
                "success": True, "response": f"回复{task.request_id}", "conversation_id": None,
            },
        ):
            await bridge._drain_life_queue(ws)
        # FIFO: both queued submits reached a terminal state, in order.
        terminals = [
            r["payload"] for r in _sent_payloads(ws)
            if r["payload"]["status"] in {"completed", "failed"}
        ]
        assert [t["requestId"] for t in terminals] == ["q-1", "q-2"]
        assert bridge._life_queue == []

    asyncio.run(run())


def test_queued_life_submit_drains_after_work_turn_finishes(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        ws = AsyncMock()
        # A life message arrives while the single model slot is taken.
        await bridge._busy_lock.acquire()
        try:
            await bridge._handle_life_chat_submit(ws, {
                "payload": {"requestId": "life-q", "saveId": "Save1",
                            "mode": "chat", "text": "排队消息"},
            }, "Save1")
            assert _sent_payloads(ws)[-1]["payload"]["status"] == "queued"
        finally:
            bridge._busy_lock.release()

        with patch.object(
            bridge, "_execute_turn",
            return_value={"success": True, "response": "作业完成",
                          "conversation_id": None, "usage": None},
        ), patch.object(
            bridge, "_execute_life_turn",
            return_value={"success": True, "response": "排到你啦", "conversation_id": None},
        ):
            await bridge.handle_chat_submit(ws, "work-req", "把南瓜收一下", "Save1")
            # The work turn's finally schedules the drain; give it a few loop ticks.
            for _ in range(20):
                await asyncio.sleep(0.01)
        statuses = [
            (r["payload"].get("requestId"), r["payload"]["status"])
            for r in _sent_payloads(ws)
        ]
        assert ("life-q", "queued") in statuses
        assert ("life-q", "completed") in statuses
        assert bridge._life_queue == []

    asyncio.run(run())


# ---------------------------------------------------------------- profile / memory messages
def test_life_profile_get_set_roundtrip(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        ws = AsyncMock()
        await bridge._handle_life_message(ws, "life.profile.get", {
            "payload": {"requestId": "g1", "saveId": "Save1"},
        }, "Save1")
        state = _sent_payloads(ws)[-1]
        assert state["messageType"] == "life.profile.state"
        assert state["payload"]["status"] == "confirmed"
        assert state["payload"]["profile"] is None
        work = state["payload"]["work"]
        assert work["mode"] in {"free", "command"}
        assert "activeGoals" in work and "recentTodos" in work and "waitingConditions" in work

        await bridge._handle_life_message(ws, "life.profile.set", {
            "payload": {"requestId": "s1", "saveId": "Save1", "expectedRevision": 0,
                        "patch": {"onboarded": True, "companionName": "阿星"}},
        }, "Save1")
        state = _sent_payloads(ws)[-1]["payload"]
        assert state["status"] == "confirmed"
        assert state["profileRevision"] == 1
        assert state["profile"]["onboarded"] is True

        # Stale revision is rejected without overwriting.
        await bridge._handle_life_message(ws, "life.profile.set", {
            "payload": {"requestId": "s2", "saveId": "Save1", "expectedRevision": 0,
                        "patch": {"companionName": "篡改"}},
        }, "Save1")
        state = _sent_payloads(ws)[-1]["payload"]
        assert state["status"] == "rejected"
        assert state["reason"] == "STALE_REVISION"
        assert bridge._profile_store.get("Save1")["profile"]["companionName"] == "阿星"

    asyncio.run(run())


def test_life_memory_list_and_edit(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        ws = AsyncMock()
        await bridge._handle_life_message(ws, "life.memory.list", {
            "payload": {"requestId": "m1", "saveId": "Save1"},
        }, "Save1")
        state = _sent_payloads(ws)[-1]
        assert state["messageType"] == "life.memory.state"
        assert state["payload"]["entries"] == []
        assert state["payload"]["memoryRevision"] == 0

        await bridge._handle_life_message(ws, "life.memory.edit", {
            "payload": {"requestId": "m2", "saveId": "Save1", "expectedRevision": 0,
                        "op": "add", "kind": "agreement", "text": "每天给鸡喂食"},
        }, "Save1")
        state = _sent_payloads(ws)[-1]["payload"]
        assert state["status"] == "confirmed"
        assert state["memoryRevision"] == 1
        assert state["entries"][0]["source"] == "player"

        # Invalid edits are rejected with a reason on the wire, not an exception.
        await bridge._handle_life_message(ws, "life.memory.edit", {
            "payload": {"requestId": "m4", "saveId": "Save1", "expectedRevision": 99,
                        "op": "delete", "id": "不存在"},
        }, "Save1")
        state = _sent_payloads(ws)[-1]["payload"]
        assert state["status"] == "rejected"
        assert state["reason"] in {"STALE_REVISION", "NOT_FOUND"}

    asyncio.run(run())


def test_life_chat_submit_invalid_payload_gets_failed_terminal(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        ws = AsyncMock()
        await bridge._handle_life_chat_submit(ws, {
            "payload": {"requestId": "bad-1", "saveId": "Save1",
                        "mode": "chat", "text": ""},
        }, "Save1")
        reply = _sent_payloads(ws)[-1]
        assert reply["messageType"] == "life.chat.reply"
        assert reply["payload"]["status"] == "failed"
        assert "error" in reply["payload"]

    asyncio.run(run())


# ---------------------------------------------------------------- memory event hook
def _make_task(bridge: ChatBridge, save_id: str = "Save1") -> str:
    token = "decision-token-1"
    bridge._work_store.begin_decision(save_id, token)
    result = bridge._work_store.submit_plan(
        save_id,
        goal_text="日常农活",
        tasks=[{"title": "给胡萝卜浇水",
                "steps": [{"operation": "water_auto", "params": {"max_tiles": 5}}]}],
        decision_token=token,
    )
    return result["tasks"][0]["id"]


def test_job_success_terminal_writes_memory_event_once(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        task_id = _make_task(bridge)
        execution = StepExecution(
            status="executed", task_id=task_id, command_id="cmd-42",
            operation="water_auto", outcome="completed", task_status="completed",
        )
        await bridge._on_job_terminal("Save1", execution, "settled")
        entries = bridge._memory_store.list("Save1")["entries"]
        events = [e for e in entries if e["kind"] == "event"]
        assert len(events) == 1
        assert "给胡萝卜浇水" in events[0]["text"]
        assert events[0]["source"] == "system"
        # list() is the wire exit (§1.5): commandId is stripped there but must
        # stay persisted for dedup.
        assert "commandId" not in events[0]
        raw = json.loads(
            (tmp_path / "data" / "companion-memory.json").read_text(encoding="utf-8")
        )
        persisted = [e for e in raw["Save1"]["entries"] if e["kind"] == "event"]
        assert persisted[0]["commandId"] == "cmd-42"
        # A duplicated terminal for the same commandId must not write twice.
        await bridge._on_job_terminal("Save1", execution, "settled-again")
        events = [e for e in bridge._memory_store.list("Save1")["entries"] if e["kind"] == "event"]
        assert len(events) == 1

    asyncio.run(run())


def test_failed_job_terminal_writes_no_memory_event(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        task_id = _make_task(bridge)
        execution = StepExecution(
            status="executed", task_id=task_id, command_id="cmd-43",
            operation="water_auto", outcome="partial", task_status="partial",
            reason_code="NATIVE_STEP_FAILED",
        )
        await bridge._on_job_terminal("Save1", execution, "settled")
        assert bridge._memory_store.list("Save1")["entries"] == []

    asyncio.run(run())


def test_partial_native_terminal_with_successful_prose_records_no_completion(tmp_path) -> None:
    """D6/contract顶部: the completion memory event comes from the native terminal only.

    ``_on_job_terminal`` derives job_success from ``StepExecution.outcome``, which
    the plan worker fills via ``normalise_native_result``/``classify_step_outcome``
    over the real native tool result (``terminalState``) — never from model text
    and never from ``commandComplete=true``. A partial native terminal whose
    prose says "全部完成" must not be recorded as a completed shared experience.
    """
    async def run() -> None:
        bridge = _bridge(tmp_path)
        task_id = _make_task(bridge)

        # Reconcile path: the runtime verdict is partial even though the text
        # claims full completion.
        outcome, reason, effects, _rev = normalise_native_result(
            {"terminalState": "partially-succeeded", "message": "全部完成啦！", "commandId": "cmd-44"}
        )
        assert outcome == "partial"
        execution = StepExecution(
            status="executed", task_id=task_id, command_id="cmd-44",
            operation="water_auto", outcome=outcome, task_status="partial", reason_code=reason,
        )
        await bridge._on_job_terminal("Save1", execution, "settled")
        assert bridge._memory_store.list("Save1")["entries"] == []

        # Dispatch path: classification only trusts terminal/status/outcome
        # fields of the real tool result, not success prose.
        assert classify_step_outcome({"terminalState": "partially-succeeded", "message": "完成！"})[0] == "partial"
        assert classify_step_outcome({"terminalState": "succeeded", "message": "done"})[0] == "completed"
        # Model-style chat with commandComplete alone is not even a tool result.
        assert classify_step_outcome({"commandComplete": True, "message": "都干完了"})[0] == "unknown"

        # A task whose steps did not all complete is not a completion either,
        # even if one step's outcome claims completed.
        mixed = StepExecution(
            status="executed", task_id=task_id, command_id="cmd-45",
            operation="water_auto", outcome="completed", task_status="partial",
        )
        await bridge._on_job_terminal("Save1", mixed, "settled")
        assert bridge._memory_store.list("Save1")["entries"] == []

        # Only a real succeeded terminal records the completion event.
        ok_exec = StepExecution(
            status="executed", task_id=task_id, command_id="cmd-46",
            operation="water_auto", outcome="completed", task_status="completed",
        )
        await bridge._on_job_terminal("Save1", ok_exec, "settled")
        events = [e for e in bridge._memory_store.list("Save1")["entries"] if e["kind"] == "event"]
        assert len(events) == 1
        assert "给胡萝卜浇水" in events[0]["text"]

    asyncio.run(run())


# ---------------------------------------------------------------- care hooks
def test_work_done_care_fires_once_and_quiet_blocks(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        bridge._profile_store.set(
            "Save1", {"onboarded": True, "careFrequency": "chatty"}, expected_revision=0
        )
        bridge._current_game_day_key = "1:spring:2"
        bridge._latest_snapshot_payload = {
            "world": {"year": 1, "season": "spring", "dayOfMonth": 2, "timeOfDay": 1200}
        }
        ws = AsyncMock()
        bridge._chat_ws = ws
        with patch.object(
            bridge, "_generate_care_text", new=AsyncMock(return_value="辛苦啦，喝口水吧")
        ) as generate:
            await bridge._maybe_fire_care("Save1", "work-done", "cmd-1",
                                          {"timeOfDay": 1200}, fact="完成了「收获成熟作物」")
            assert generate.await_args.args[4] == "完成了「收获成熟作物」"
        cares = [r["payload"] for r in _sent_payloads(ws) if r["messageType"] == "life.care"]
        assert len(cares) == 1
        assert cares[0]["kind"] == "work-done"
        assert cares[0]["text"] == "辛苦啦，喝口水吧"
        assert cares[0]["gameDate"] == "1:spring:2"
        assert cares[0]["eventKey"] == "work-done:1:spring:2:cmd-1"
        # Same eventKey never re-fires (restart-safe).
        with patch.object(
            bridge, "_generate_care_text", new=AsyncMock(return_value="又来？")
        ):
            await bridge._maybe_fire_care("Save1", "work-done", "cmd-1",
                                          {"timeOfDay": 1300})
        cares = [r["payload"] for r in _sent_payloads(ws) if r["messageType"] == "life.care"]
        assert len(cares) == 1

        # quiet frequency disables proactive care entirely.
        bridge._profile_store.set(
            "Save1", {"careFrequency": "quiet"}, expected_revision=1
        )
        with patch.object(
            bridge, "_generate_care_text", new=AsyncMock(return_value="x")
        ):
            await bridge._maybe_fire_care("Save1", "evening", "1:spring:2",
                                          {"timeOfDay": 1900})
        cares = [r["payload"] for r in _sent_payloads(ws) if r["messageType"] == "life.care"]
        assert len(cares) == 1

    asyncio.run(run())


def test_care_model_failure_skips_without_template(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        bridge._profile_store.set(
            "Save1", {"onboarded": True, "careFrequency": "chatty"}, expected_revision=0
        )
        bridge._current_game_day_key = "1:spring:2"
        ws = AsyncMock()
        bridge._chat_ws = ws
        with patch.object(
            bridge, "_generate_care_text", new=AsyncMock(return_value=None)
        ):
            await bridge._maybe_fire_care("Save1", "morning", "1:spring:2",
                                          {"timeOfDay": 600})
        assert [r for r in _sent_payloads(ws) if r["messageType"] == "life.care"] == []
        # The consumed eventKey must not re-fire (no catch-up spam).
        assert bridge._care_service.is_fired("Save1", "morning:1:spring:2:1:spring:2")

    asyncio.run(run())


def test_busy_work_defers_care_until_model_slot_is_free(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        bridge._profile_store.set(
            "Save1", {"onboarded": True, "careFrequency": "moderate"}, expected_revision=0
        )
        bridge._current_game_day_key = "1:spring:2"
        ws = AsyncMock()
        bridge._chat_ws = ws
        await bridge._busy_lock.acquire()
        try:
            with patch.object(
                bridge, "_generate_care_text", new=AsyncMock(return_value="今天下雨，作物不用浇水。")
            ) as generate:
                await bridge._maybe_fire_care(
                    "Save1", "morning", "1:spring:2",
                    {"timeOfDay": 600, "weather": {"isRaining": True}},
                )
                assert bridge._care_service.is_fired("Save1", "morning:1:spring:2:1:spring:2")
                assert not [r for r in _sent_payloads(ws) if r["messageType"] == "life.care"]
                bridge._busy_lock.release()
                await asyncio.gather(*bridge._deferred_care_tasks)
                cares = [r["payload"] for r in _sent_payloads(ws) if r["messageType"] == "life.care"]
                assert len(cares) == 1
                assert cares[0]["text"] == "今天下雨，作物不用浇水。"
                assert generate.await_args.args[4] is None
                assert generate.await_args.kwargs["world"]["weather"]["isRaining"] is True
        finally:
            if bridge._busy_lock.locked():
                bridge._busy_lock.release()

    asyncio.run(run())


def test_morning_and_evening_care_hooks(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        bridge._profile_store.set(
            "Save1", {"onboarded": True, "careFrequency": "chatty"}, expected_revision=0
        )
        ws = AsyncMock()
        bridge._chat_ws = ws
        fired: list[tuple[str, str]] = []
        with patch.object(
            bridge, "_maybe_fire_care",
            new=lambda save_id, kind, ref, world: fired.append((kind, ref)) or asyncio.sleep(0),
        ):
            day1 = {"year": 1, "season": "spring", "dayOfMonth": 2, "timeOfDay": 600}
            await bridge._snapshot_care_hooks("Save1", day1, "1:spring:2", None)
            assert ("morning", "1:spring:2") in fired
            bridge._current_game_day_key = "1:spring:2"  # what the receive loop tracks
            # Same day, later snapshot below 1900: nothing new.
            await bridge._snapshot_care_hooks("Save1", dict(day1, timeOfDay=1200), "1:spring:2", "1:spring:2")
            assert len(fired) == 1
            # First snapshot of the day reaching 1900 fires evening exactly once.
            evening_world = dict(day1, timeOfDay=1905)
            await bridge._snapshot_care_hooks("Save1", evening_world, "1:spring:2", "1:spring:2")
            await bridge._snapshot_care_hooks("Save1", dict(day1, timeOfDay=1930), "1:spring:2", "1:spring:2")
            assert fired.count(("evening", "1:spring:2")) == 1
            # Next day everything may fire again.
            day2 = {"year": 1, "season": "spring", "dayOfMonth": 3, "timeOfDay": 610}
            await bridge._snapshot_care_hooks("Save1", day2, "1:spring:3", "1:spring:2")
            assert ("morning", "1:spring:3") in fired

    asyncio.run(run())


def test_deleted_agreement_disappears_from_decision_context(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        _, r = bridge._memory_store.add(
            "Save1", kind="agreement", text="每天去矿洞", source="player",
            game_date="1:spring:1", expected_revision=0,
        )
        ctx = bridge._decision_context("Save1")
        assert any(a["text"] == "每天去矿洞" for a in ctx["agreements"])
        bridge._memory_store.delete("Save1", r["entry"]["id"], r["memoryRevision"])
        ctx = bridge._decision_context("Save1")
        assert "agreements" not in ctx or all(
            a["text"] != "每天去矿洞" for a in ctx["agreements"]
        )

    asyncio.run(run())


def test_decision_context_carries_companion_profile(tmp_path) -> None:
    async def run() -> None:
        bridge = _bridge(tmp_path)
        bridge._profile_store.set(
            "Save1", {"onboarded": True, "personality": "calm", "playStyle": "workhorse",
                      "careFrequency": "moderate", "companionName": "稳稳"},
            expected_revision=0,
        )
        ctx = bridge._decision_context("Save1")
        assert ctx["companion"] == {
            "name": "稳稳", "personality": "calm",
            "playStyle": "workhorse", "careFrequency": "moderate",
        }

    asyncio.run(run())
