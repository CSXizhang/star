"""agy bridge integration boundaries; all transport and game calls are mocked."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from stardew_ai_runtime import chat_bridge as bridge_module
from stardew_ai_runtime.agy_process import AgyProcessOutcome
from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge

OLD_CID = "8a529711-ba69-4525-9e02-1677048039d6"
NEW_CID = "512337d0-c926-4732-af39-eaa39da2de13"


def _bridge(tmp_path: Path, monkeypatch, *, run_dir: Path | None = None) -> ChatBridge:
    monkeypatch.setenv("STARDEW_CENTRAL_COMMANDS_FILE", "none")
    bridge = ChatBridge(run_dir=run_dir, backend="agy", model="gemini-test", effort="low",
                        commands_file=tmp_path / "ledger" / "commands.jsonl",
                        sessions_file=tmp_path / "sessions.json", enable_plan_worker=False)
    bridge._autonomy = None
    bridge._work_store = None
    return bridge


@pytest.mark.parametrize("returned_cid", [OLD_CID, NEW_CID])
def test_life_baselines_reach_transport_and_changed_cid_resets_usage_interval(tmp_path, monkeypatch, returned_cid):
    bridge = _bridge(tmp_path, monkeypatch)
    task = ActiveChatTask(request_id="life-contract", save_id="Save1")
    gen = Mock(return_value=7)
    steps = Mock(return_value=11)
    delta_usage = {"input_tokens": 10, "output_tokens": 3, "cache_read_tokens": 20,
                   "total_tokens": 13, "latestRequestInputContext": 30}
    delta = Mock(return_value=delta_usage)
    monkeypatch.setattr(bridge_module, "get_max_gen_idx", gen)
    monkeypatch.setattr(bridge_module, "get_max_step_idx", steps)
    monkeypatch.setattr(bridge_module, "get_command_usage_delta", delta)
    monkeypatch.setattr(bridge_module, "check_new_quota_error", lambda *args: (False, ""))
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda *args: None)
    observed = []

    def transport(active, command, prompt, session_id, **options):
        observed.append((active.start_max_idx, active.start_max_step_idx, options["start_idx"], session_id))
        active.provider_conversation_id = returned_cid
        options["on_conversation"](returned_cid)
        return AgyProcessOutcome(json.dumps({"conversation_id": returned_cid, "status": "SUCCESS", "response": "ok"}),
                                 "", returned_cid, 0)

    monkeypatch.setattr(bridge_module, "run_agy_process", transport)
    result = bridge._execute_life_turn(task, OLD_CID, "只读对话")
    assert observed == [(7, 11, 7, OLD_CID)]
    gen.assert_called_once_with(OLD_CID)
    steps.assert_called_once_with(OLD_CID)
    baseline = 7 if returned_cid == OLD_CID else -1
    delta.assert_called_once_with(returned_cid, baseline)
    assert task.start_max_idx == baseline
    assert task.start_max_step_idx == (11 if returned_cid == OLD_CID else -1)
    assert result["usage"] is delta_usage and result["success"]


def test_twenty_measured_multigeneration_turns_do_not_rotate_on_consumption_or_count(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, monkeypatch)
    bridge._session_token_budget = 100000
    bridge._session_request_checkpoint = 20
    usage = {"input_tokens": 1000000, "cache_read_tokens": 4000000,
             "output_tokens": 10000, "total_tokens": 1010000, "generations_count": 50,
             "latestRequestInputContext": 49999, "input_context_measured": True}
    for _ in range(20):
        assert bridge._note_session_context("Save1", usage) is None
    state = bridge._session_context_state["Save1"]
    assert state["latestInputContext"] == 49999 and state["maxInputContext"] == 49999
    assert state["unmeasuredRequests"] == 0 and bridge._session_requests["Save1"] == 20
    assert bridge._note_session_context("Save1", {**usage, "latestRequestInputContext": 100000}) == "SESSION_CONTEXT_BUDGET"


def test_fallback_requires_twenty_consecutive_unknown_turns_and_measurement_resets_it(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, monkeypatch)
    bridge._session_request_checkpoint = 20
    unknown = {"input_tokens": 1000000, "cache_read_tokens": 4000000,
               "generations_count": 50, "input_context_measured": False}
    for _ in range(19):
        assert bridge._note_session_context("Save1", unknown) is None
    assert bridge._note_session_context("Save1", {"latestRequestInputContext": 1,
        "input_context_measured": True, "generations_count": 50}) is None
    assert bridge._session_context_state["Save1"]["unmeasuredRequests"] == 0
    for _ in range(19):
        assert bridge._note_session_context("Save1", unknown) is None
    assert bridge._note_session_context("Save1", unknown) == "SESSION_REQUEST_CHECKPOINT"
    assert bridge._session_context_state["Save1"]["unmeasuredRequests"] == 20


def test_cancel_attributes_receipt_to_active_turn_and_includes_independent_cache_in_display(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, monkeypatch)
    task = ActiveChatTask(request_id="root-command-continuation", save_id="Save1", command_id="root-command",
                          start_max_idx=7, provider_conversation_id=NEW_CID)
    bridge._active_task = task
    bridge._current_command_id = Mock(return_value="root-command")
    bridge._break_command_chain = Mock()
    bridge._interrupt_short_job = AsyncMock(return_value=True)
    bridge._release_execution = AsyncMock()
    bridge._send_reply = AsyncMock()
    bridge.get_conversation_id = Mock(return_value=OLD_CID)
    usage = {"input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 3,
             "total_tokens": 13, "input_includes_cache": False}
    delta = Mock(return_value=usage)
    monkeypatch.setattr(bridge_module, "get_max_gen_idx", lambda cid: 8)
    monkeypatch.setattr(bridge_module, "get_command_usage_delta", delta)
    assert asyncio.run(bridge.handle_chat_cancel(None, "root-command", "player_cancelled", "Save1"))
    delta.assert_called_once_with(NEW_CID, 7)
    rows = [json.loads(line) for line in bridge._commands_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["requestId"] == task.request_id
    assert rows[0]["conversationId"] == NEW_CID and rows[0]["usage"]["total_tokens"] == 13
    reply = bridge._send_reply.call_args.args[1].payload
    assert reply["requestId"] == "root-command"
    assert reply["tokensUsed"] == 33 and reply["promptTokens"] == 30 and reply["cachedTokens"] == 20
    assert "33 token" in bridge._usage_display.summary_text(today=False)
    assert task.recorded


def test_cancel_uses_baseline_reset_by_buffered_cid_during_provider_join(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, monkeypatch)
    task = ActiveChatTask(request_id="cancel-buffered-cid", save_id="Save1", command_id="root-command",
                          start_max_idx=7, provider_conversation_id=OLD_CID)
    bridge._active_task = task
    bridge._current_command_id = Mock(return_value="root-command")
    bridge._break_command_chain = Mock()
    bridge._interrupt_short_job = AsyncMock(return_value=True)
    bridge._send_reply = AsyncMock()
    bridge.get_conversation_id = Mock(return_value=OLD_CID)
    # The transport finalizer can consume an already queued init only after the
    # cancellation handler yields to join the owned executor task.
    bridge.abort_active_task = Mock(side_effect=lambda reason: setattr(task, "cancelled", True))
    usage = {"input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 3,
             "total_tokens": 13, "input_includes_cache": False}
    delta = Mock(return_value=usage)
    monkeypatch.setattr(bridge_module, "get_max_gen_idx", lambda cid: 1)
    monkeypatch.setattr(bridge_module, "get_command_usage_delta", delta)

    async def scenario():
        async def transport_finalizer():
            task.provider_conversation_id = NEW_CID
            task.start_max_idx = -1
            task.start_max_step_idx = -1

        task.async_task = asyncio.create_task(transport_finalizer())
        assert await bridge.handle_chat_cancel(None, "root-command", "player_cancelled", "Save1")

    asyncio.run(scenario())
    delta.assert_called_once_with(NEW_CID, -1)
    row = json.loads(bridge._commands_file.read_text(encoding="utf-8").strip())
    assert row["conversationId"] == NEW_CID and row["startGenIdx"] == -1 and row["endGenIdx"] == 1
    assert row["usage"] == usage


@pytest.mark.parametrize("autodiscovered", [False, True])
def test_transport_recovery_and_ui_share_ledger_root_even_if_run_dir_changes(tmp_path, monkeypatch, autodiscovered):
    recovery = Mock(return_value=0)
    monkeypatch.setattr("stardew_ai_runtime.usage_meter.recover_agy_receipts", recovery)
    bridge = _bridge(tmp_path, monkeypatch, run_dir=None if autodiscovered else tmp_path / "game")
    if autodiscovered:
        bridge.run_dir = tmp_path / "discovered-game"
    observed = []

    def transport(active, command, prompt, session_id, **options):
        observed.append(options["run_dir"])
        return AgyProcessOutcome(json.dumps({"status": "SUCCESS", "response": "ok"}), "", None, 0)

    monkeypatch.setattr(bridge_module, "run_agy_process", transport)
    monkeypatch.setattr(bridge_module, "check_new_quota_error", lambda *args: (False, ""))
    result = bridge._execute_agy_turn(ActiveChatTask(request_id="ledger-test", save_id="Save1"), None, "只读")
    assert result["success"]
    root = bridge._commands_file.parent
    recovery.assert_called_once_with(root)
    assert observed == [root]
    assert bridge._usage_display._responses.journal == root / "data" / "model-usage.jsonl"


@pytest.mark.parametrize("path", ["abort", "join"])
def test_abort_and_join_delegate_agy_owned_process_tree_cleanup(tmp_path, monkeypatch, path):
    bridge = _bridge(tmp_path, monkeypatch)
    proc = Mock()
    proc.poll.return_value = None
    proc.pid = 9999
    task = ActiveChatTask(request_id="cleanup-test", save_id="Save1", process=proc)
    bridge._active_task = task
    wrong_backend_terminate = Mock(side_effect=AssertionError("must use agy tree cleanup"))
    bridge._backend = SimpleNamespace(terminate=wrong_backend_terminate)
    terminate = Mock()
    monkeypatch.setattr(bridge_module, "terminate_agy_process", terminate)
    if path == "abort":
        bridge.abort_active_task("contract teardown")
    else:
        async def scenario():
            future = asyncio.get_running_loop().create_future()
            joined = asyncio.create_task(bridge._join_interrupted_provider(task, future))
            await asyncio.sleep(0)
            assert not joined.done()  # executor ownership remains until its finalizer is done
            future.set_result({"success": False})
            await joined
        asyncio.run(scenario())
    terminate.assert_called_once_with(proc)
    wrong_backend_terminate.assert_not_called()
    proc.kill.assert_not_called()
    assert task.cancelled and task.abort_reason
