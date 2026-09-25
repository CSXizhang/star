import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.decision_context import build_decision_context
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def test_player_chat_submit_unpauses_work_state(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False, backend="kimi")
    bridge._send_reply = AsyncMock()
    store = bridge._work_store
    store.set_paused("Save1", True)
    assert store.state("Save1").paused is True

    def fake_execute(*args):
        return {"success": True, "response": "done", "conversation_id": None}

    monkeypatch.setattr(bridge, "_execute_turn", fake_execute)
    asyncio.run(bridge.handle_chat_submit(None, "player-req-1", "继续干活", "Save1"))
    assert store.state("Save1").paused is False

    # Autonomy submit should not unpause
    store.set_paused("Save1", True)
    bridge._autonomy_requests["autonomy-req-1"] = "fp"
    asyncio.run(bridge.handle_chat_submit(None, "autonomy-req-1", "自主工作", "Save1"))
    assert store.state("Save1").paused is True


def test_decision_validation_reasons_and_guidance(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    guide = "若无法继续，请直接向玩家说明当前阻塞原因，不要尝试文件系统操作排查。"

    # 1. A provider may select while paused, but native dispatch stays frozen.
    store.begin_decision("save", "tok-1")
    store.set_paused("save", True)
    store.submit_plan("save", goal_text="g", decision_token="tok-1", tasks=[{"title": "t", "steps": [{"operation": "water_auto"}]}])
    assert store.state("save").decision["selected"] is True
    assert store.claim_next_step("save", "worker") is None

    # 2. Token missing / mismatch
    store.set_paused("save", False)
    store.begin_decision("save", "tok-2")
    with pytest.raises(WorkStateError) as exc_missing:
        store.select_direct_job("save", "wrong-tok", "water_auto")
    assert "决策令牌缺失或不匹配" in str(exc_missing.value)
    assert guide in str(exc_missing.value)

    # 3. Already selected
    store.select_direct_job("save", "tok-2", "water_auto")
    with pytest.raises(WorkStateError) as exc_selected:
        store.select_direct_job("save", "tok-2", "ship_items")
    assert "当前决策周期已选择过任务" in str(exc_selected.value)
    assert guide in str(exc_selected.value)

    with pytest.raises(WorkStateError) as exc_selected_plan:
        store.submit_plan("save", goal_text="g", decision_token="tok-2", tasks=[{"title": "t", "steps": [{"operation": "water_auto"}]}])
    assert "当前决策周期已选择过任务" in str(exc_selected_plan.value)
    assert guide in str(exc_selected_plan.value)

    # 4. Expired token
    store.begin_decision("save", "tok-3")
    store._mutate("save", lambda s: s.decision.update(expires=time.time() - 10))
    with pytest.raises(WorkStateError) as exc_expired:
        store.select_direct_job("save", "tok-3", "water_auto")
    assert "决策令牌已过期" in str(exc_expired.value)
    assert guide in str(exc_expired.value)


def test_decision_context_exposes_paused_and_decision(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False, backend="kimi")
    store = bridge._work_store
    store.begin_decision("Save1", "token-xyz")
    store.set_paused("Save1", True)

    work_ctx = bridge._work_context("Save1")
    assert work_ctx["paused"] is True
    assert isinstance(work_ctx["decision"], dict)

    ctx = build_decision_context(None, work=work_ctx, origin="chat")
    assert ctx["paused"] is True
    assert ctx["currentTask"]["paused"] is True
    assert "decision" in ctx
    assert "decision" in ctx["currentTask"]


def test_failed_turn_does_not_put_response_in_error(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False, backend="agy")
    task = ActiveChatTask(request_id="req-fail", save_id="save1", prompt="p")

    # Simulate agy CLI process exit 1, empty stderr, parsed response present
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (
        json.dumps({"status": "FAILED", "response": "这是模型的有效汇报说明", "conversation_id": "cid-1"}),
        "",
    )
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: mock_proc)
    monkeypatch.setattr("stardew_ai_runtime.chat_bridge.get_conversation_db_path", lambda *a: tmp_path / "dummy.db")

    result = bridge._execute_agy_turn(task, "cid-1", "prompt")
    assert result["success"] is False
    assert result["error"] == "AGY_EXIT_1"
    assert result["response"] == "这是模型的有效汇报说明"


def test_interrupted_turn_records_command(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False, backend="agy")
    bridge._send_reply = AsyncMock()

    # Simulate timeout causing interrupted status
    def fake_timeout(*args):
        return {
            "success": False,
            "response": "执行超时 (10分钟)，操作已中止。",
            "error": "TIMEOUT",
            "conversation_id": "cid-timeout",
            "duration": 5.0,
            "status": "interrupted",
        }

    monkeypatch.setattr(bridge, "_execute_turn", fake_timeout)
    asyncio.run(bridge.handle_chat_submit(None, "req-timeout", "超时任务", "save1"))

    # Read chat_commands.jsonl
    commands_file = bridge._commands_file
    assert commands_file.exists()
    records = [json.loads(line) for line in commands_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    interrupted_record = next(r for r in records if r["requestId"] == "req-timeout")
    assert interrupted_record["status"] == "interrupted"
    assert interrupted_record["error"] == "TIMEOUT"
    assert interrupted_record["durationSeconds"] == 5.0


def test_work_plan_overview_exempt_from_protect_job(tmp_path):
    mock_sched = MagicMock()
    mock_sched.get_status = AsyncMock(return_value={"saveId": "Save1"})
    mock_sched.latest_snapshot = {"payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 1}}}
    server = create_mcp_server(run_dir=tmp_path, scheduler=mock_sched, surface="light")

    async def run():
        # Calling work_plan_overview through call_capability should not raise SHORT_JOB_UNSUPPORTED
        name, res = await server.call_tool("call_capability", {"tool": "work_plan_overview", "params": {}})
        assert isinstance(res, dict)
        assert "tasks" in res["result"]

    asyncio.run(run())


def test_dispatch_plan_operation_strips_unknown_params(tmp_path):
    mock_sched = MagicMock()
    mock_sched.run_dir = None
    mock_sched.get_status = AsyncMock(return_value={"saveId": "Save1"})
    captured = {}

    async def _water_auto(max_tiles=None, include_empty_tiles=False, command_id=None):
        captured["max_tiles"] = max_tiles
        captured["command_id"] = command_id
        return {"status": "executed", "terminalState": "succeeded", "effects": []}

    mock_sched.water_auto = _water_auto
    server = create_mcp_server(run_dir=tmp_path, scheduler=mock_sched, surface="internal")
    store = WorkStore(tmp_path / "data" / "work-state.json")

    # Set up running task with extra detail param in work state
    token = "dec-1"
    store.begin_decision("Save1", token)
    store.submit_plan(
        "Save1",
        goal_text="water",
        decision_token=token,
        tasks=[{
            "id": "t1",
            "title": "water",
            "steps": [{"id": "s1", "operation": "water_auto", "params": {"max_tiles": 10, "detail": "redundant_detail"}}],
        }],
    )
    # Claim it
    store.claim_next_step("Save1", "worker1")
    command_id = "plan:Save1:t1:s1:attempt-1"
    store.assign_command_id("Save1", "t1", "s1", command_id)

    async def run():
        with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": token}):
            # dispatch with redundant detail param should succeed because detail is stripped
            _, result = await server.call_tool(
                "dispatch_plan_operation",
                {
                    "operation": "water_auto",
                    "params": {"max_tiles": 10, "detail": "redundant_detail"},
                    "command_id": command_id,
                },
            )
            assert result["terminalState"] == "succeeded"
            assert captured["max_tiles"] == 10
            assert captured["command_id"] == command_id

    asyncio.run(run())


def test_clean_stale_waiting_tasks(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    store.begin_decision("Save1", "token-w")
    store.submit_plan(
        "Save1",
        goal_text="wait test",
        decision_token="token-w",
        tasks=[{
            "id": "task-w",
            "title": "waiting task",
            "steps": [{"id": "step-w", "operation": "water_auto", "params": {}}],
        }],
    )
    store.claim_next_step("Save1", "worker-w")
    # Park behind a wait condition
    store.mark_task_waiting(
        "Save1",
        "task-w",
        condition={"type": "gameDay", "params": {"day": 10}},
        reason_code="WAITING_FOR_DAY",
    )
    assert store.state("Save1").tasks[0].status == "waiting"

    # With timeout_seconds = 0, clean_stale_waiting_tasks immediately fails it
    expired = store.clean_stale_waiting_tasks("Save1", timeout_seconds=0)
    assert len(expired) == 1
    assert expired[0]["reasonCode"] == "WAITING_TIMEOUT"

    # Verify task and step status
    task = store.state("Save1").tasks[0]
    assert task.status == "failed"
    assert task.steps[0].status == "failed"
    assert task.steps[0].reason_code == "WAITING_TIMEOUT"

    # Verify it appears in overview anomalies
    overview = store.overview("Save1")
    anomaly = next((a for a in overview["anomalies"] if a["taskId"] == "task-w"), None)
    assert anomaly is not None
    assert anomaly["reasonCode"] == "WAITING_TIMEOUT"


def test_quota_baseline_reset_on_session_rotation(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False, backend="agy")
    task = ActiveChatTask(
        request_id="req-cross",
        save_id="save1",
        prompt="p",
        start_max_step_idx=100,  # from old session
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    # agy silent new session
    mock_proc.communicate.return_value = (
        json.dumps({"status": "SUCCESS", "response": "ok", "conversation_id": "cid-new"}),
        "",
    )
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: mock_proc)

    called_args = []
    def fake_check(cid, baseline):
        called_args.append((cid, baseline))
        return False, ""

    monkeypatch.setattr("stardew_ai_runtime.chat_bridge.check_new_quota_error", fake_check)

    bridge._execute_agy_turn(task, "cid-old", "prompt")
    assert len(called_args) == 1
    assert called_args[0] == ("cid-new", -1)  # baseline must be reset to -1, not 100!
