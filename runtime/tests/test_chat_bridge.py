"""Unit and integration tests for ChatBridge and in-game chat protocol."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stardew_ai_runtime.chat_bridge import (
    ActiveChatTask,
    ChatBridge,
    get_command_usage_delta,
    get_max_gen_idx,
)
from stardew_ai_runtime.protocol import (
    ChatCancelPayload,
    ChatReplyPayload,
    ChatSubmitPayload,
    Envelope,
)
from stardew_ai_runtime.scheduler import DiscoveryError


def test_chat_submit_payload_roundtrip() -> None:
    submit = ChatSubmitPayload(
        request_id="req-123",
        text="帮我给农场的作物浇水",
        source="text",
        save_id="Farm_12345",
    )
    mapping = submit.to_mapping()
    assert mapping["requestId"] == "req-123"
    assert mapping["text"] == "帮我给农场的作物浇水"
    assert mapping["saveId"] == "Farm_12345"

    submit2 = ChatSubmitPayload.from_mapping(mapping)
    assert submit2.request_id == submit.request_id
    assert submit2.text == submit.text
    assert submit2.save_id == submit.save_id


def test_chat_reply_payload_roundtrip() -> None:
    reply = ChatReplyPayload(
        request_id="req-123",
        status="completed",
        reply_text="胡萝卜已全部种植并浇水完毕！",
        tokens_used=12345,
        prompt_tokens=10000,
        output_tokens=2345,
        cached_tokens=45000,
        conversation_id="cid-999",
    )
    mapping = reply.to_mapping()
    assert mapping["requestId"] == "req-123"
    assert mapping["status"] == "completed"
    assert mapping["tokensUsed"] == 12345
    assert mapping["conversationId"] == "cid-999"

    reply2 = ChatReplyPayload.from_mapping(mapping)
    assert reply2.request_id == reply.request_id
    assert reply2.tokens_used == 12345
    assert reply2.reply_text == "胡萝卜已全部种植并浇水完毕！"


def test_chat_cancel_payload_roundtrip() -> None:
    cancel = ChatCancelPayload(
        request_id="req-123",
        reason="player_cancelled_in_menu",
    )
    mapping = cancel.to_mapping()
    assert mapping["requestId"] == "req-123"
    assert mapping["reason"] == "player_cancelled_in_menu"

    cancel2 = ChatCancelPayload.from_mapping(mapping)
    assert cancel2.request_id == "req-123"
    assert cancel2.reason == "player_cancelled_in_menu"


def test_envelope_chat_submit_and_reply_and_cancel() -> None:
    env_submit = Envelope.create_chat_submit(
        sender_instance_id="mod-instance-1",
        request_id="req-456",
        text="把箱子里的种子种下",
        save_id="Farm_ABC",
    )
    assert env_submit.message_type == "chat.submit"
    assert env_submit.payload["text"] == "把箱子里的种子种下"

    mapping = env_submit.to_mapping()
    env_parsed = Envelope.from_mapping(mapping)
    assert env_parsed.message_type == "chat.submit"

    env_reply = Envelope.create_chat_reply(
        sender_instance_id="bridge-1",
        request_id="req-456",
        status="completed",
        reply_text="任务已成功完成！",
        save_id="Farm_ABC",
        tokens_used=5000,
    )
    assert env_reply.message_type == "chat.reply"
    assert env_reply.payload["tokensUsed"] == 5000
    assert env_reply.payload["replyText"] == "任务已成功完成！"

    env_cancel = Envelope.create_chat_cancel(
        sender_instance_id="mod-instance-1",
        request_id="req-456",
        reason="player_cancelled",
        save_id="Farm_ABC",
    )
    assert env_cancel.message_type == "chat.cancel"
    assert env_cancel.payload["reason"] == "player_cancelled"


def test_chat_bridge_session_persistence(tmp_path: Path) -> None:
    sessions_file = tmp_path / "chat_sessions.json"
    bridge = ChatBridge(sessions_file=sessions_file)

    assert bridge.get_conversation_id("Save1") is None
    bridge.record_conversation_id("Save1", "conv-uuid-1")
    assert bridge.get_conversation_id("Save1") == "conv-uuid-1"

    # Reload in a fresh bridge instance
    bridge2 = ChatBridge(sessions_file=sessions_file)
    assert bridge2.get_conversation_id("Save1") == "conv-uuid-1"
    assert bridge2.get_conversation_id("Save2") is None


def test_format_agent_prompt() -> None:
    bridge = ChatBridge()
    prompt = bridge._format_agent_prompt("收割所有成熟作物")
    assert "收割所有成熟作物" in prompt
    assert "stardew-companion" in prompt
    assert "MCP" in prompt


def test_execute_agy_turn_success_parsing() -> None:
    bridge = ChatBridge(model="gemini-3.8-flash", effort="medium")
    mock_stdout = json.dumps({
        "conversation_id": "cid-abc-123",
        "status": "SUCCESS",
        "response": "所有作物均已浇水完成！",
        "usage": {
            "input_tokens": 12000,
            "output_tokens": 800,
            "total_tokens": 12800,
            "cache_read_tokens": 30000,
        },
    })

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (mock_stdout, "")
        mock_popen.return_value = mock_proc

        task = ActiveChatTask(request_id="r1", save_id="s1")
        res = bridge._execute_agy_turn(
            active_task=task,
            conversation_id="cid-abc-123",
            prompt="给农作物浇水",
        )

        assert res["success"] is True
        assert res["response"] == "所有作物均已浇水完成！"
        assert res["conversation_id"] == "cid-abc-123"
        assert res["usage"]["total_tokens"] == 12800


def test_execute_agy_turn_empty_response_not_falsified() -> None:
    """Empty response + SUCCESS cannot be falsified as '操作已完成'."""
    bridge = ChatBridge()
    mock_stdout = json.dumps({
        "conversation_id": "cid-empty-001",
        "status": "SUCCESS",
        "response": "   ",  # empty/whitespace response
    })

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (mock_stdout, "")
        mock_popen.return_value = mock_proc

        task = ActiveChatTask(request_id="r1", save_id="s1")
        res = bridge._execute_agy_turn(
            active_task=task,
            conversation_id="cid-empty-001",
            prompt="任何指令",
        )

        assert res["success"] is False
        assert "未返回具体汇报说明" in res["response"]
        assert res["error"] == "EMPTY_MODEL_RESPONSE"


def test_execute_agy_turn_effort_and_model_rules() -> None:
    """On resume, DO NOT re-supply --model; pass --effort medium."""
    bridge = ChatBridge(model="gemini-3.8-flash", effort="medium")

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (json.dumps({"status": "SUCCESS", "response": "ok"}), "")
        mock_popen.return_value = mock_proc

        # 1. New conversation: --model AND --effort should be present
        task1 = ActiveChatTask(request_id="r1", save_id="s1")
        bridge._execute_agy_turn(task1, conversation_id=None, prompt="test prompt")
        cmd_new = mock_popen.call_args[0][0]
        assert "--model" in cmd_new
        assert "gemini-3.8-flash" in cmd_new
        assert "--effort" in cmd_new
        assert "medium" in cmd_new
        assert "--conversation" not in cmd_new

        # 2. Resumed conversation: --conversation AND --effort present, --model MUST NOT be present
        task2 = ActiveChatTask(request_id="r2", save_id="s1")
        bridge._execute_agy_turn(task2, conversation_id="existing-cid-999", prompt="test prompt")
        cmd_resume = mock_popen.call_args[0][0]
        assert "--conversation" in cmd_resume
        assert "existing-cid-999" in cmd_resume
        assert "--effort" in cmd_resume
        assert "medium" in cmd_resume
        assert "--model" not in cmd_resume


def test_execute_agy_turn_quota_exhaustion() -> None:
    """Genuine quota exhaustion check."""
    bridge = ChatBridge()
    failure_codes: list[str] = []
    bridge._backend_failure_callback = failure_codes.append
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", "Error: RESOURCE_EXHAUSTED: quota exceeded for model")
        mock_popen.return_value = mock_proc

        task = ActiveChatTask(request_id="r1", save_id="s1")
        res = bridge._execute_agy_turn(
            active_task=task,
            conversation_id=None,
            prompt="种胡萝卜",
        )

        assert res["success"] is False
        assert "额度已用尽" in res["response"]
        assert res["error"] == "RESOURCE_EXHAUSTED"
        assert failure_codes == ["RESOURCE_EXHAUSTED"]


def test_execute_agy_turn_rate_limit_notifies_and_is_not_quota() -> None:
    bridge = ChatBridge()
    failure_codes: list[str] = []
    bridge._backend_failure_callback = failure_codes.append
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", "429 Too Many Requests: rate limit exceeded")
        mock_popen.return_value = mock_proc

        res = bridge._execute_agy_turn(
            active_task=ActiveChatTask(request_id="r2", save_id="s1"),
            conversation_id=None,
            prompt="种胡萝卜",
        )

    assert res["error"] == "RATE_LIMIT_EXCEEDED"
    assert failure_codes == ["RATE_LIMIT_EXCEEDED"]


def test_handle_chat_submit_player_preempts_old_job_with_fake_provider(tmp_path: Path) -> None:
    async def run() -> None:
        bridge=ChatBridge(run_dir=tmp_path,enable_plan_worker=False)
        ws=AsyncMock()
        old=ActiveChatTask(request_id="old",save_id="Save1")
        bridge._active_task=old
        with patch.object(bridge,"_execute_turn",return_value={"success":True,"response":"new decision", "conversation_id":None,"usage":None,"provider":"fake"}) as provider:
            await bridge.handle_chat_submit(ws,"new","new player instruction","Save1")
        assert old.cancelled
        assert provider.call_count==1
        assert json.loads(ws.send_text.call_args[0][0])["payload"]["status"]=="decision-completed"
    asyncio.run(run())


def test_handle_chat_cancel_immediate_abort() -> None:
    """Cancellation kills running process and sends cancelled reply."""
    async def run() -> None:
        bridge = ChatBridge()
        mock_ws = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # process is running

        task = ActiveChatTask(request_id="req-to-cancel", save_id="Save1", process=mock_proc)
        bridge._active_task = task

        await bridge.handle_chat_cancel(
            ws=mock_ws,
            request_id="req-to-cancel",
            reason="player_clicked_cancel",
            save_id="Save1",
        )

        # Process must have been killed
        assert mock_proc.kill.called
        assert task.cancelled is True
        assert bridge._active_task is None

    asyncio.run(run())



def test_get_command_usage_delta_calculation(tmp_path: Path) -> None:
    """Verifies exact boundary calculation from SQLite gen_metadata."""
    # Create a dummy conversation DB with 3 turns
    db_path = tmp_path / "test-conv-123.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")

    # Construct protobuf blobs using Tag 1 -> Tag 4 format:
    # Tag 1 (wire 2): length-delimited
    #   Tag 4 (wire 2): length-delimited
    #     Tag 2 (wire 0): input_tokens
    #     Tag 3 (wire 0): output_tokens
    #     Tag 5 (wire 0): cache_read_tokens
    #     Tag 9 (wire 0): thinking_tokens
    def make_proto(inp: int, out: int, cache: int, think: int) -> bytes:
        # inner: Tag 4
        # Tag 2: (2 << 3) | 0 = 16
        # Tag 3: (3 << 3) | 0 = 24
        # Tag 5: (5 << 3) | 0 = 40
        # Tag 9: (9 << 3) | 0 = 72
        def varint(val: int) -> bytes:
            buf = bytearray()
            while val > 0x7F:
                buf.append((val & 0x7F) | 0x80)
                val >>= 7
            buf.append(val & 0x7F)
            return bytes(buf)

        inner = b"".join([
            varint(16) + varint(inp),
            varint(24) + varint(out),
            varint(40) + varint(cache),
            varint(72) + varint(think),
        ])
        # mid: Tag 4 in Tag 1
        # Tag 4: (4 << 3) | 2 = 34
        mid = varint(34) + varint(len(inner)) + inner
        # outer: Tag 1 in root
        # Tag 1: (1 << 3) | 2 = 10
        outer = varint(10) + varint(len(mid)) + mid
        return outer

    # Turn 0: old command (5000 in, 500 out)
    con.execute("INSERT INTO gen_metadata VALUES (0, ?, ?)", (make_proto(5000, 500, 10000, 100), 100))
    # Turn 1: new command gen 1 (6000 in, 300 out)
    con.execute("INSERT INTO gen_metadata VALUES (1, ?, ?)", (make_proto(6000, 300, 15000, 200), 100))
    # Turn 2: new command gen 2 (7000 in, 400 out)
    con.execute("INSERT INTO gen_metadata VALUES (2, ?, ?)", (make_proto(7000, 400, 15000, 300), 100))
    con.commit()
    con.close()

    with patch("stardew_ai_runtime.chat_bridge.get_conversation_db_path", return_value=db_path):
        assert get_max_gen_idx("test-conv-123") == 2

        # Delta for new command starting after idx 0: should ONLY sum idx 1 and 2!
        delta = get_command_usage_delta("test-conv-123", start_idx=0)
        assert delta is not None
        assert delta["input_tokens"] == 13000  # 6000 + 7000
        assert delta["output_tokens"] == 700   # 300 + 400
        assert delta["cache_read_tokens"] == 30000  # 15000 + 15000
        assert delta["thinking_tokens"] == 500      # 200 + 300
        assert delta["total_tokens"] == 13700       # 13000 + 700
        assert delta["generations_count"] == 2
        assert delta["start_idx"] == 1
        assert delta["end_idx"] == 2
        assert delta["source"] == "db_gen_metadata_delta"


def test_old_stdout_error_ignored_on_successful_turn() -> None:
    """Historical stdout containing error string does NOT trigger quota error when turn succeeds."""
    bridge = ChatBridge()
    # Simulate stdout that has old history with RESOURCE_EXHAUSTED, but current turn completed with SUCCESS
    mock_stdout = (
        "Previous turns:\nUser: Can you check error RESOURCE_EXHAUSTED?\n"
        + json.dumps({
            "conversation_id": "cid-hist-1",
            "status": "SUCCESS",
            "response": "胡萝卜浇水完成！",
        })
    )
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (mock_stdout, "")
        mock_popen.return_value = mock_proc

        task = ActiveChatTask(request_id="r1", save_id="s1")
        res = bridge._execute_agy_turn(task, conversation_id="cid-hist-1", prompt="浇水")
        assert res["success"] is True
        assert res["response"] == "胡萝卜浇水完成！"


def test_rate_limit_distinguished_from_quota() -> None:
    """Differentiate transient 429 rate limit from hard quota exhaustion."""
    bridge = ChatBridge()
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", "Error 429: rate limit exceeded. Please retry later.")
        mock_popen.return_value = mock_proc

        task = ActiveChatTask(request_id="r1", save_id="s1")
        res = bridge._execute_agy_turn(task, conversation_id=None, prompt="浇水")
        assert res["success"] is False
        assert res["error"] == "RATE_LIMIT_EXCEEDED"
        assert "Rate Limit" in res["response"]


def test_cancel_race_pre_and_post_spawn() -> None:
    """Task cancelled pre-spawn skips Popen; post-spawn immediately kills process."""
    bridge = ChatBridge()

    # Pre-spawn cancellation
    task_pre = ActiveChatTask(request_id="r_pre", save_id="s1")
    task_pre.cancelled = True
    with patch("subprocess.Popen") as mock_popen:
        res_pre = bridge._execute_agy_turn(task_pre, conversation_id=None, prompt="test")
        assert res_pre["success"] is False
        assert res_pre["error"] == "CANCELLED"
        assert not mock_popen.called

    # Post-spawn cancellation
    task_post = ActiveChatTask(request_id="r_post", save_id="s1")
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 9999

        def popen_side_effect(*args, **kwargs):
            # Simulate cancel arriving right as process spawns
            task_post.cancelled = True
            return mock_proc

        mock_popen.side_effect = popen_side_effect
        res_post = bridge._execute_agy_turn(task_post, conversation_id=None, prompt="test")
        assert res_post["success"] is False
        assert res_post["error"] == "CANCELLED"
        assert mock_proc.kill.called


def test_resume_omits_cumulative_cli_usage_when_delta_missing(tmp_path: Path) -> None:
    """Resumed session NEVER shows cumulative CLI usage if delta missing."""
    async def run() -> None:
        cmd_file = tmp_path / "chat_commands.jsonl"
        sessions_file = tmp_path / "chat_sessions.json"
        bridge = ChatBridge(backend="agy", sessions_file=sessions_file, commands_file=cmd_file)
        bridge.record_conversation_id("Save1", "conv-existing-1")
        # A resumable session must carry the fingerprint of the profile it was
        # created with; a legacy session without one is intentionally NOT resumed.
        bridge._save_profile_fingerprints(
            {f"{bridge.backend_name}:Save1": bridge.profile_fingerprint()}
        )

        mock_ws = AsyncMock()
        with patch("stardew_ai_runtime.compatibility.assert_native_compatible"), patch.object(bridge, "_execute_agy_turn") as mock_turn, \
             patch("stardew_ai_runtime.chat_bridge.get_command_usage_delta", return_value=None):
            mock_turn.return_value = {
                "success": True,
                "response": "第 2 条指令执行完成",
                "conversation_id": "conv-existing-1",
                "usage": {
                    "input_tokens": 100000,
                    "output_tokens": 5000,
                    "total_tokens": 105000,
                },
                "duration": 5.0,
            }

            await bridge.handle_chat_submit(
                ws=mock_ws,
                request_id="req-resumed",
                text="第 2 条指令",
                save_id="Save1",
            )

            # Check sent reply
            assert mock_ws.send_text.called
            sent_reply = json.loads(mock_ws.send_text.call_args[0][0])
            payload = sent_reply["payload"]
            assert payload["status"] == "completed"
            assert payload.get("tokensUsed") is None
            assert payload["usageSource"] == "unavailable_in_resume"

            # Check recorded JSONL
            records = [json.loads(line) for line in cmd_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert len(records) == 1
            assert records[0]["requestId"] == "req-resumed"
            assert records[0]["usage"] is None
            assert records[0]["missingReason"] == "db_delta_unavailable_resumed_session_cumulative_ignored"

    asyncio.run(run())


def test_new_conversation_allows_session_local_cli_usage(tmp_path: Path) -> None:
    """New session allows session-local CLI fallback when delta missing."""
    async def run() -> None:
        cmd_file = tmp_path / "chat_commands.jsonl"
        sessions_file = tmp_path / "chat_sessions.json"
        bridge = ChatBridge(backend="agy", sessions_file=sessions_file, commands_file=cmd_file)

        mock_ws = AsyncMock()
        with patch("stardew_ai_runtime.compatibility.assert_native_compatible"), patch.object(bridge, "_execute_agy_turn") as mock_turn, \
             patch("stardew_ai_runtime.chat_bridge.get_command_usage_delta", return_value=None):
            mock_turn.return_value = {
                "success": True,
                "response": "新会话第 1 条指令执行完成",
                "conversation_id": "conv-new-1",
                "usage": {
                    "input_tokens": 8000,
                    "output_tokens": 400,
                    "total_tokens": 8400,
                },
                "duration": 3.0,
            }

            await bridge.handle_chat_submit(
                ws=mock_ws,
                request_id="req-new-1",
                text="新指令",
                save_id="SaveNew",
            )

            assert mock_ws.send_text.called
            sent_reply = json.loads(mock_ws.send_text.call_args[0][0])
            payload = sent_reply["payload"]
            assert payload["tokensUsed"] == 8400
            assert payload["usageSource"] == "cli_direct_new_session"

    asyncio.run(run())


def test_handle_chat_cancel_records_command_in_jsonl(tmp_path: Path) -> None:
    """Cancelled commands are persisted into chat_commands.jsonl."""
    async def run() -> None:
        cmd_file = tmp_path / "chat_commands.jsonl"
        sessions_file = tmp_path / "chat_sessions.json"
        bridge = ChatBridge(sessions_file=sessions_file, commands_file=cmd_file)
        mock_ws = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        task = ActiveChatTask(
            request_id="req-cancel-persist",
            save_id="Save1",
            prompt="种防风草",
            process=mock_proc,
        )
        bridge._active_task = task

        await bridge.handle_chat_cancel(
            ws=mock_ws,
            request_id="req-cancel-persist",
            reason="user_cancel",
            save_id="Save1",
        )

        assert mock_proc.kill.called
        records = [json.loads(line) for line in cmd_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 1
        assert records[0]["requestId"] == "req-cancel-persist"
        assert records[0]["status"] == "cancelled"
        assert records[0]["prompt"] == "种防风草"

    asyncio.run(run())


def test_resume_ignores_old_cumulative_quota_error(tmp_path: Path) -> None:
    """Resumed session ignores old cumulative error in parsed JSON when turn succeeds."""
    db_path = tmp_path / "conv-resumed-old-err.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)")
    con.execute("INSERT INTO steps VALUES (10, 17, ?)", (b'{"error": "OLD_RESOURCE_EXHAUSTED"}',))
    con.commit()
    con.close()

    bridge = ChatBridge()
    mock_stdout = json.dumps({
        "conversation_id": "conv-resumed-old-err",
        "status": "SUCCESS",
        "response": "浇水完成！",
        "error": "RESOURCE_EXHAUSTED: quota exceeded",
    })

    with patch("subprocess.Popen") as mock_popen, \
         patch("stardew_ai_runtime.chat_bridge.get_conversation_db_path", return_value=db_path):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (mock_stdout, "")
        mock_popen.return_value = mock_proc

        task = ActiveChatTask(
            request_id="r1",
            save_id="s1",
            start_max_step_idx=10,  # Baseline is 10
        )
        res = bridge._execute_agy_turn(
            task,
            conversation_id="conv-resumed-old-err",
            prompt="浇水",
        )

        assert res["success"] is True
        assert res["response"] == "浇水完成！"


def test_resume_detects_genuine_new_step17_quota_error(tmp_path: Path) -> None:
    """Resumed session detects fresh RESOURCE_EXHAUSTED in steps table with idx > baseline."""
    db_path = tmp_path / "conv-resumed-fresh-err.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)")
    con.execute("INSERT INTO steps VALUES (5, 1, ?)", (b'{"type": "USER_INPUT"}',))
    con.execute("INSERT INTO steps VALUES (6, 17, ?)", (b'{"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota reached"}',))
    con.commit()
    con.close()

    bridge = ChatBridge()
    mock_stdout = json.dumps({
        "conversation_id": "conv-resumed-fresh-err",
        "status": "ERROR",
        "response": "",
    })

    with patch("subprocess.Popen") as mock_popen, \
         patch("stardew_ai_runtime.chat_bridge.get_conversation_db_path", return_value=db_path):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (mock_stdout, "")
        mock_popen.return_value = mock_proc

        task = ActiveChatTask(
            request_id="r2",
            save_id="s1",
            start_max_step_idx=5,  # Baseline is 5, step 6 is new!
        )
        res = bridge._execute_agy_turn(
            task,
            conversation_id="conv-resumed-fresh-err",
            prompt="播种",
        )

        assert res["success"] is False
        assert res["error"] == "RESOURCE_EXHAUSTED"
        assert "额度已用尽" in res["response"]


def test_chat_bridge_zero_args_auto_discovery_no_type_error() -> None:
    """Verifies that ChatBridge with run_dir=None does not raise TypeError and handles DiscoveryError gracefully."""
    bridge = ChatBridge(run_dir=None)
    stop_event = asyncio.Event()

    calls = 0

    def mock_disc(run_dir: Any = None, timeout_seconds: float = 1.0) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert run_dir is None, "Expected run_dir to be None for default zero-arg entry"
        stop_event.set()
        raise DiscoveryError("Game is not running or transport discovery file not found")

    with patch("stardew_ai_runtime.chat_bridge.resolve_discovery", side_effect=mock_disc):
        asyncio.run(bridge.run(stop_event))

    assert calls >= 1
    assert bridge.run_dir is None


def test_chat_bridge_zero_args_auto_discovers_and_connects(tmp_path: Path) -> None:
    """Verifies that ChatBridge with run_dir=None updates run_dir when discovered and connects."""
    bridge = ChatBridge(run_dir=None)
    stop_event = asyncio.Event()

    mock_mod_dir = str(tmp_path / "Mods" / "StardewAI.Companion.Mod")
    mock_disc_result = {
        "host": "127.0.0.1",
        "port": 54321,
        "sessionToken": "test-sec-token",
        "saveId": "save-test-1",
        "modDir": mock_mod_dir,
    }

    mock_ws = MagicMock()

    async def mock_receive_text(timeout: float = 2.0) -> str:
        stop_event.set()
        raise TimeoutError()

    mock_ws.receive_text = mock_receive_text
    mock_ws.close = AsyncMock()

    with patch("stardew_ai_runtime.chat_bridge.resolve_discovery", return_value=mock_disc_result), \
         patch("stardew_ai_runtime.chat_bridge.WebSocketClient.connect", return_value=mock_ws) as mock_connect:
        asyncio.run(bridge.run(stop_event))

    mock_connect.assert_called_once_with(
        host="127.0.0.1",
        port=54321,
        path="/chat",
        headers={"Authorization": "Bearer test-sec-token"},
        timeout=5.0,
    )
    assert bridge.run_dir == Path(mock_mod_dir)


def test_autonomy_chat_channel_lifecycle_replays_initial_snapshot_and_enable(tmp_path: Path) -> None:
    """Native snapshots use /chat, so enable-after-connect works without a second socket."""
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", False)
    bridge._autonomy.set_preferences("save-1", goal="种植", budget_limit=75, box_preference="作物入箱")
    enabled = False
    sent: list[str] = []
    stop_event = asyncio.Event()
    snapshot = {
        "protocolVersion": "0.1", "messageType": "world.snapshot", "messageId": "m1",
        "senderInstanceId": "mod", "sequenceNumber": 1, "worldRevision": 7,
        "sentAt": "2026-01-01T00:00:00Z", "saveId": "save-1", "gameSessionId": "g1",
        "payload": {"world": {"season": "spring", "dayOfMonth": 1},
                    "farmWork": {"matureCropCount": 1, "cropUnwateredTiles": []}},
    }

    class FakeSocket:
        async def send_text(self, value: str) -> None:
            sent.append(value)

        async def receive_text(self, timeout: float = 2.0) -> str:
            nonlocal enabled
            if not enabled:
                bridge._autonomy.set_enabled("save-1", True)
                enabled = True
                return json.dumps(snapshot)
            await asyncio.sleep(0)
            stop_event.set()
            raise TimeoutError()

    async def exercise() -> None:
        with patch.object(bridge, "handle_chat_submit", new=AsyncMock()) as submit:
            await bridge._receive_loop(FakeSocket(), "save-1", stop_event)
            submit.assert_awaited_once()
            assert submit.await_args.args[0].__class__.__name__ == "FakeSocket"
            assert "75" in submit.await_args.args[2]
            assert "作物入箱" in submit.await_args.args[2]
            await bridge._send_reply(
                submit.await_args.args[0],
                Envelope.create_chat_reply(bridge.instance_id, "autonomy-reply", "completed", "已完成", save_id="save-1"),
            )

    asyncio.run(exercise())
    assert len(bridge._autonomy_requests) == 1
    assert json.loads(sent[-1])["messageType"] == "chat.reply"


def test_autonomy_cancel_disables_future_work(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)
    bridge._active_task = ActiveChatTask("autonomy-1", "save-1", "auto")

    class ReplySocket:
        async def send_text(self, _text: str) -> None:
            return None

    asyncio.run(bridge.handle_chat_cancel(ReplySocket(), "autonomy-1", "player cancelled", "save-1"))
    assert bridge._autonomy.state("save-1").enabled is False


def test_any_player_chat_cancel_disables_autonomy_even_when_idle(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)

    class ReplySocket:
        async def send_text(self, _text: str) -> None:
            return None

    asyncio.run(bridge.handle_chat_cancel(ReplySocket(), None, "player stopped autonomy", "save-1"))
    assert bridge._autonomy.state("save-1").enabled is False


def test_external_disable_is_applied_on_event_without_timeout(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)
    bridge._active_task = ActiveChatTask("autonomy-2", "save-1", "auto")
    bridge._autonomy.set_enabled("save-1", False)

    async def run() -> None:
        with patch.object(bridge, "handle_chat_cancel", new=AsyncMock()) as cancel:
            await bridge._stop_autonomy_if_disabled(MagicMock(), "save-1")
            cancel.assert_awaited_once()

    asyncio.run(run())


def test_record_command_isolates_central_file_under_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under pytest, _record_command must NOT write to user's home central file."""
    home_mock = tmp_path / "fake_home"
    isolated_central = tmp_path / "isolated_central.jsonl"
    monkeypatch.setattr(Path, "home", lambda: home_mock)
    monkeypatch.setenv("STARDEW_CENTRAL_COMMANDS_FILE", str(isolated_central))

    cmd_file = tmp_path / "local_commands.jsonl"
    bridge = ChatBridge(commands_file=cmd_file)
    bridge._record_command(
        request_id="req-test-iso",
        save_id="SaveIso",
        conversation_id="conv-iso-1",
        prompt="test prompt",
        status="completed",
        start_idx=0,
        end_idx=1,
        usage=None,
    )

    # Wrote to isolated central file specified in env
    assert isolated_central.is_file()
    assert "req-test-iso" in isolated_central.read_text(encoding="utf-8")
    # Did NOT write to fake home
    fake_home_central = home_mock / ".gemini" / "antigravity-cli" / "chat_commands.jsonl"
    assert not fake_home_central.is_file()


def test_kimi_submit_uses_wire_usage_and_skips_agy_db(tmp_path: Path) -> None:
    """Kimi usage comes from the provider wire, never the agy SQLite bill."""
    async def run() -> None:
        cmd_file = tmp_path / "chat_commands.jsonl"
        bridge = ChatBridge(
            backend="kimi",
            sessions_file=tmp_path / "chat_sessions.json",
            commands_file=cmd_file,
        )
        wire_usage = {
            "input_tokens": 1371,
            "output_tokens": 599,
            "cache_read_tokens": 48640,
            "cache_creation_tokens": 100,
            "total_tokens": 50710,
            "thinking_tokens": None,
            "generations_count": 3,
            "source": "kimi_wire_usage_record",
            "unknown": False,
        }
        mock_ws = AsyncMock()
        with patch.object(bridge, "_execute_turn") as mock_turn, \
             patch("stardew_ai_runtime.chat_bridge.wire_offset", return_value=1234) as offset, \
             patch("stardew_ai_runtime.chat_bridge.read_usage_since", return_value=wire_usage) as read_usage, \
             patch("stardew_ai_runtime.chat_bridge.get_max_gen_idx") as agy_idx:
            mock_turn.return_value = {
                "success": True,
                "response": "已浇完",
                "conversation_id": "session_kimi_1",
                "duration": 2.5,
            }

            await bridge.handle_chat_submit(
                ws=mock_ws, request_id="req-kimi-1", text="浇水", save_id="SaveK"
            )

            assert mock_ws.send_text.called
            payload = json.loads(mock_ws.send_text.call_args[0][0])["payload"]
            assert payload["status"] == "completed"
            assert payload["tokensUsed"] == 50710
            assert payload["usageSource"] == "kimi_wire_usage_record"

            offset.assert_called_once()
            read_usage.assert_called_once()
            assert read_usage.call_args[0][1] == 1234
            agy_idx.assert_not_called()

            records = [
                json.loads(line)
                for line in cmd_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert records[0]["usage"]["input_tokens"] == 1371
            assert records[0]["usage"]["cache_read_tokens"] == 48640

    asyncio.run(run())


def test_kimi_unknown_wire_usage_is_reported_unknown_not_zero(tmp_path: Path) -> None:
    async def run() -> None:
        cmd_file = tmp_path / "chat_commands.jsonl"
        bridge = ChatBridge(
            backend="kimi",
            sessions_file=tmp_path / "chat_sessions.json",
            commands_file=cmd_file,
        )
        mock_ws = AsyncMock()
        with patch.object(bridge, "_execute_turn") as mock_turn, \
             patch("stardew_ai_runtime.chat_bridge.wire_offset", return_value=0), \
             patch("stardew_ai_runtime.chat_bridge.read_usage_since", return_value=None):
            mock_turn.return_value = {
                "success": True,
                "response": "完成",
                "conversation_id": "session_kimi_2",
                "duration": 1.0,
            }

            await bridge.handle_chat_submit(
                ws=mock_ws, request_id="req-kimi-2", text="浇水", save_id="SaveK"
            )

            payload = json.loads(mock_ws.send_text.call_args[0][0])["payload"]
            assert payload.get("tokensUsed") is None
            assert payload["usageSource"] == "unknown"

            record = json.loads(cmd_file.read_text(encoding="utf-8").splitlines()[0])
            assert record["usage"] is None
            assert record["missingReason"] == "kimi_wire_usage_unavailable"

    asyncio.run(run())


def test_kimi_cancel_still_settles_wire_usage(tmp_path: Path) -> None:
    async def run() -> None:
        cmd_file = tmp_path / "chat_commands.jsonl"
        bridge = ChatBridge(
            backend="kimi",
            sessions_file=tmp_path / "chat_sessions.json",
            commands_file=cmd_file,
        )
        bridge.record_conversation_id("SaveK", "session_kimi_cancel")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        bridge._active_task = ActiveChatTask(
            request_id="req-kimi-cancel",
            save_id="SaveK",
            prompt="浇水",
            process=mock_proc,
            wire_start_offset=42,
        )
        wire_usage = {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_tokens": 5,
            "total_tokens": 17,
            "source": "kimi_wire_usage_record",
        }
        with patch("stardew_ai_runtime.chat_bridge.read_usage_since", return_value=wire_usage) as read_usage:
            await bridge.handle_chat_cancel(
                ws=AsyncMock(),
                request_id="req-kimi-cancel",
                reason="player_cancel",
                save_id="SaveK",
            )

        read_usage.assert_called_once()
        assert read_usage.call_args[0][1] == 42
        record = json.loads(cmd_file.read_text(encoding="utf-8").splitlines()[0])
        assert record["status"] == "cancelled"
        assert record["usage"]["total_tokens"] == 17

    asyncio.run(run())


def test_autonomy_control_records_user_goal_and_pauses_work(tmp_path: Path) -> None:
    """F8 controls persist a user-sourced goal and block autonomous step scheduling."""

    async def run() -> None:
        bridge = ChatBridge(run_dir=tmp_path)
        mock_ws = AsyncMock()

        control = Envelope.create_autonomy_control(
            bridge.instance_id,
            "req-goal-1",
            "SaveK",
            "set_preferences",
            goal="把农场种满防风草",
        )
        await bridge._handle_autonomy_control(mock_ws, control, "SaveK")

        goals = bridge._work_store.list_goals("SaveK")
        assert len(goals) == 1
        assert goals[0]["text"] == "把农场种满防风草"
        assert goals[0]["source"] == "user"

        pause = Envelope.create_autonomy_control(bridge.instance_id, "req-pause-1", "SaveK", "pause")
        await bridge._handle_autonomy_control(mock_ws, pause, "SaveK")
        assert bridge._work_store.state("SaveK").paused is True

        resume = Envelope.create_autonomy_control(bridge.instance_id, "req-resume-1", "SaveK", "resume")
        await bridge._handle_autonomy_control(mock_ws, resume, "SaveK")
        assert bridge._work_store.state("SaveK").paused is False

    asyncio.run(run())


def test_chat_prompt_injects_compact_live_context() -> None:
    bridge = ChatBridge()
    bridge._latest_snapshot_payload = {
        "world": {
            "year": 1,
            "season": "spring",
            "dayOfMonth": 6,
            "timeOfDay": 610,
            "isRaining": True,
            "weatherIcon": "1",
            "currentLocation": "Farm",
        },
        "companion": {
            "stamina": 120,
            "tileX": 61,
            "tileY": 17,
            "waterCanLevel": 20,
            "availableMoney": 900,
            "moneyStatus": "ok",
        },
        "inventory": {
            "freeSlots": 4,
            "slots": [{"itemId": "(O)472", "name": "Parsnip Seeds", "stack": 4}],
        },
        "farmWork": {"tilledUnwateredTiles": [{"x": i, "y": i} for i in range(100)]},
    }
    bridge._latest_snapshot_revision = 12
    prompt = bridge._format_agent_prompt("浇水", "save-1")
    assert '"weather":{"icon":"1","isRaining":true}' in prompt
    assert '"day":6' in prompt
    assert "Parsnip Seeds" in prompt
    assert '"funds":900' in prompt
    assert '"freeSlots":4' in prompt
    assert "tilledUnwateredTiles" not in prompt, "no farm bulk dump in the prompt"
    assert "submit_plan" in prompt and "remember_intent" in prompt
    assert "收割所有成熟作物" not in prompt


def test_prompt_never_turns_missing_weather_into_clear() -> None:
    bridge = ChatBridge()
    bridge._latest_snapshot_payload = {
        "world": {"year": 1, "season": "winter", "dayOfMonth": 3, "isRaining": False},
    }
    prompt = bridge._format_agent_prompt("在吗", "save-1")
    assert '"weather":{"isRaining":false' in prompt
    assert '"clear"' not in prompt


def test_prompt_marks_missing_native_fields_unknown() -> None:
    bridge = ChatBridge()
    prompt = bridge._format_agent_prompt("在吗", "save-1")
    assert '"provenance":"unknown"' in prompt
    assert "实时上下文" in prompt


def test_day_advance_settles_once_and_rotates_provider_session(tmp_path: Path) -> None:
    async def run() -> None:
        bridge = ChatBridge(run_dir=tmp_path, backend="kimi")
        store = bridge._work_store
        assert store is not None
        goal_id = store.add_goal("save-1", "照料农场", source="user").id
        store.create_plan(
            "save-1",
            goal_id,
            [{"id": "t1", "title": "浇水", "steps": [{"id": "s1", "operation": "water_auto"}]}],
        )
        bridge.record_conversation_id("save-1", "conv-1")

        await bridge._settle_day_advance("save-1", {"year": 1, "season": "spring", "dayOfMonth": 5})
        assert bridge._last_day_settlement["reasonCode"] == "FIRST_OBSERVATION"
        first_cid = bridge.get_conversation_id("save-1")

        await bridge._settle_day_advance("save-1", {"year": 1, "season": "spring", "dayOfMonth": 6})
        assert bridge._last_day_settlement["settled"] is True
        # Safe rotation on the real day advance; the old id stays attributable.
        assert bridge.get_conversation_id("save-1") is None
        history = bridge._session_history["kimi:save-1"]
        assert history[-1] == "conv-1"
        assert first_cid == "conv-1"

        # Replaying the same day must not settle or rotate again.
        bridge.record_conversation_id("save-1", "conv-2")
        await bridge._settle_day_advance("save-1", {"year": 1, "season": "spring", "dayOfMonth": 6})
        assert bridge._last_day_settlement["reasonCode"] == "ALREADY_SETTLED"
        assert bridge.get_conversation_id("save-1") == "conv-2"

    asyncio.run(run())


def test_receive_loop_settles_only_a_real_day_change(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    stop_event = asyncio.Event()
    day = {"value": 1}
    sent_snapshots = 0

    def snapshot_message() -> str:
        return json.dumps(
            {
                "protocolVersion": "0.1",
                "messageType": "world.snapshot",
                "messageId": f"m{day['value']}",
                "senderInstanceId": "mod",
                "sequenceNumber": 1,
                "worldRevision": 7,
                "sentAt": "2026-01-01T00:00:00Z",
                "saveId": "save-1",
                "gameSessionId": "g1",
                "payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": day["value"]}},
            }
        )

    class FakeSocket:
        async def send_text(self, _value: str) -> None:
            return None

        async def receive_text(self, timeout: float = 2.0) -> str:
            nonlocal sent_snapshots
            if sent_snapshots < 2:
                sent_snapshots += 1
                if sent_snapshots == 2:
                    day["value"] = 2
                return snapshot_message()
            await asyncio.sleep(0)
            stop_event.set()
            raise TimeoutError()

    async def exercise() -> None:
        with patch.object(bridge, "handle_chat_submit", new=AsyncMock()):
            await bridge._receive_loop(FakeSocket(), "save-1", stop_event)

    asyncio.run(exercise())
    # First snapshot records the day; only the real advance to day 2 settles.
    assert bridge._current_game_day_key == "1:spring:2"
    assert bridge._last_day_settlement["settled"] is True
    assert bridge._last_day_settlement["fromDay"] == "1:spring:1"
    assert bridge._work_store.last_settled_day("save-1") == "1:spring:2"


def test_autonomy_state_payload_exposes_plan_action_and_wait_reason(tmp_path: Path) -> None:
    from stardew_ai_runtime.plan_executor import StepExecution

    bridge = ChatBridge(run_dir=tmp_path)
    assert bridge._plan_worker is not None
    bridge._plan_worker.last_result = StepExecution(
        status="executed", operation="water_auto", outcome="completed", task_id="t1", step_id="s1"
    )
    bridge._plan_worker.pending_reasons.append("SOIL_BLOCKED")
    payload = bridge._autonomy_state_payload("save-1", bridge._autonomy.state("save-1"))
    assert payload["lastPlanAction"]["operation"] == "water_auto"
    assert payload["planWaitReason"] == "SOIL_BLOCKED"


def test_injected_context_and_f8_expose_persisted_wait_condition(tmp_path: Path) -> None:
    """Round7: the waiting reason must be visible through the *real* presentation.

    A committed step parked behind an explicit native ``gameDay`` condition is
    written through the production ``WorkStore``; the bridge must then surface it
    in (1) the compact decision context auto-injected into every prompt and (2)
    the F8 autonomy state payload — no extra model tool query required.
    """
    from stardew_ai_runtime.decision_context import render_decision_context
    from stardew_ai_runtime.work_state import WorkStore

    bridge = ChatBridge(run_dir=tmp_path)
    store: WorkStore = bridge._work_store
    assert store is not None and bridge._plan_worker is not None

    save_id = "Save1"
    goal = store.add_goal(save_id, "照料农场", source="user")
    # The real native snapshot the chat channel would have received.
    bridge._latest_snapshot_payload = {
        "world": {"year": 1, "season": "spring", "dayOfMonth": 1, "weatherIcon": "0"},
        "companion": {"availableMoney": 500, "moneyStatus": "ok", "stamina": 270, "maxStamina": 270},
        "inventory": {"slots": [], "freeSlots": 12},
    }
    bridge._latest_snapshot_revision = 7

    submitted = store.create_plan(
        save_id,
        goal_id=goal.id,
        tasks=[
            {
                "id": "wait-task",
                "title": "native waiting condition",
                "steps": [
                    {
                        "id": "wait-step",
                        "operation": "water_zone",
                        "params": {"center_x": 65, "center_y": 15, "radius": 0},
                        "wait": {"type": "gameDay", "params": {"year": 9999, "season": "winter", "day": 28}},
                    }
                ],
            }
        ],
    )
    assert submitted
    assert not store.has_ready_step(save_id)
    persisted = store.list_tasks(save_id)[0]
    assert persisted["status"] == "waiting"
    assert persisted["steps"][0]["wait"]["type"] == "gameDay"

    # (1) Auto-injected decision context (what every prompt carries).
    context = bridge._decision_context(save_id, origin="free-mode")
    waiting = [
        entry
        for entry in context["currentTask"]["waitingFor"]
        if entry.get("taskId") == "wait-task"
    ]
    assert waiting, context["currentTask"]
    assert waiting[0]["waitingOn"][0]["condition"]["type"] == "gameDay"
    rendered = render_decision_context(context)
    assert "\n" not in rendered and "wait-task" in rendered

    # (2) F8 autonomy payload (what the settings UI shows).
    payload = bridge._autonomy_state_payload(save_id, bridge._autonomy.state(save_id))
    rows = [row for row in payload["waitingConditions"] if row.get("taskId") == "wait-task"]
    assert rows, payload.get("waitingConditions")
    assert rows[0]["waitCondition"]["type"] == "gameDay"
    assert rows[0]["stepId"] == "wait-step"
    # The farm view is deliberately not asked to carry this unrelated memory.
    assert payload["hasExecutableWork"] is False


def test_session_context_budget_rotates_on_latest_request_not_sum(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path, backend="kimi")
    bridge._session_token_budget = 100
    bridge.record_conversation_id("save-1", "conv-1")

    # Two small requests must NOT sum into a rotation (that double-counted
    # replayed/cached context before).
    assert (
        bridge._note_session_context(
            "save-1",
            {"latestRequestInputContext": 60, "input_context_measured": True, "generations_count": 1},
        )
        is None
    )
    assert (
        bridge._note_session_context(
            "save-1",
            {"latestRequestInputContext": 60, "input_context_measured": True, "generations_count": 1},
        )
        is None
    )
    # A single request whose own input context reaches the policy budget rotates.
    reason = bridge._note_session_context(
        "save-1",
        {"latestRequestInputContext": 120, "input_context_measured": True, "generations_count": 1},
    )
    assert reason == "SESSION_CONTEXT_BUDGET"
    bridge._rotate_provider_session("save-1", reason=reason)
    assert bridge.get_conversation_id("save-1") is None
    assert bridge._session_history["kimi:save-1"] == ["conv-1"]
    # A new session starts the context bookkeeping from zero.
    assert bridge._session_context_state["save-1"]["latestInputContext"] is None


def test_session_request_checkpoint_is_bounded_fallback_when_unmeasured(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._session_request_checkpoint = 3
    bridge._session_token_budget = 100000

    for _ in range(2):
        assert bridge._note_session_context("save-2", None) is None
    # Unknown measurement never invents a context length: it uses the bounded,
    # clearly-labelled request-count checkpoint instead.
    assert bridge._note_session_context("save-2", None) == "SESSION_REQUEST_CHECKPOINT"
    assert bridge._session_context_state["save-2"]["latestInputContext"] is None


def test_plan_worker_dispatch_uses_stable_command_id_and_counts_only_completed(
    tmp_path: Path,
) -> None:
    """Round6: the worker must dispatch through the internal plan tool (stable id
    reaches the real operation) and must never count a parked/failed attempt as a
    completed step."""
    from stardew_ai_runtime.chat_bridge import PlanWorker
    from stardew_ai_runtime.work_state import WorkStore

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, arguments))
            return {
                "terminalState": "succeeded",
                "completedCount": 1,
                "skippedCount": 0,
                "effects": [{"tile": {"x": 65, "y": 15}, "state": "watered"}],
                "commandId": arguments.get("command_id"),
            }

    store = WorkStore(tmp_path / "data" / "work-state.json")
    goal_id = store.add_goal("Save1", "g", source="user").id
    store.begin_decision("Save1", "test-decision-1")
    store.submit_plan(
        "Save1", goal_id=goal_id, decision_token="test-decision-1", tasks=
        [
            {
                "id": "t1",
                "title": "T",
                "steps": [{"id": "s1", "operation": "water_zone", "params": {"center_x": 65, "center_y": 15, "radius": 0}}],
            }
        ],
    )
    client = RecordingClient()
    worker = PlanWorker(store, client, worker_id="w")
    worker.supplied_save_id = "Save1"
    worker.snapshot_provider = lambda: (None, None)

    executions = asyncio.run(worker.evaluate())
    assert len(executions) == 1
    assert executions[0].outcome == "completed"
    assert worker.completed_steps == 1
    assert worker.attempted_steps == 1
    name, arguments = client.calls[0]
    assert name == "dispatch_plan_operation"
    assert arguments["operation"] == "water_zone"
    assert arguments["command_id"].startswith("plan:Save1:t1:s1:attempt-")
    assert executions[0].result["commandId"] == arguments["command_id"]

    # A transport failure is an attempt that is parked, never progress.
    class FailingClient:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise ConnectionError("transport down")

    store2 = WorkStore(tmp_path / "data2" / "work-state.json")
    goal2 = store2.add_goal("Save2", "g", source="user").id
    store2.begin_decision("Save2", "test-decision-2")
    store2.submit_plan(
        "Save2", goal_id=goal2, decision_token="test-decision-2", tasks=
        [
            {
                "id": "t1",
                "title": "T",
                "steps": [{"id": "s1", "operation": "water_zone", "params": {}}],
            }
        ],
    )
    worker2 = PlanWorker(store2, FailingClient(), worker_id="w")
    worker2.supplied_save_id = "Save2"
    worker2.snapshot_provider = lambda: (None, None)
    failing = asyncio.run(worker2.evaluate())
    assert len(failing) == 1
    assert failing[0].status == "executed"
    assert failing[0].outcome == "waiting"
    assert failing[0].reason_code == "TRANSPORT_RETRY_PENDING"
    assert worker2.attempted_steps == 1
    assert worker2.completed_steps == 0

