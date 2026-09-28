import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from stardew_ai_runtime.agent_backends import CodexBackend, KimiBackend
from stardew_ai_runtime.chat_backend_config import load_chat_backend_config


def _task():
    task = MagicMock()
    task.cancelled = False
    task.request_id = "kimi-test"
    task.process = None
    return task


def test_kimi_stream_json_collects_reply_session_and_tool_progress(tmp_path: Path) -> None:
    proc = MagicMock()
    proc.stdout = iter([
        json.dumps({"role": "meta", "type": "system.version", "version": "0.42.0"}) + "\n",
        json.dumps({"role": "assistant", "content": "只读状态："}) + "\n",
        json.dumps({"type": "tool_start", "tool_name": "get_status"}) + "\n",
        json.dumps({"role": "assistant", "content": "已完成。"}) + "\n",
        json.dumps({"role": "meta", "type": "session.resume_hint", "session_id": "kimi-s-1"}) + "\n",
    ])
    proc.returncode = 0
    proc.communicate.return_value = ("", "")
    events = []
    with patch("subprocess.Popen", return_value=proc) as popen:
        result = KimiBackend(command="kimi.exe", cwd=tmp_path, auto=False, progress=events.append).run(
            _task(), None, "查询状态"
        )
    cmd = popen.call_args.args[0]
    assert cmd == ["kimi.exe", "--model", "kimi-code/k3", "--output-format", "stream-json", "-p", "查询状态"]
    assert result["success"] is True
    assert result["response"] == "只读状态：已完成。"
    assert result["conversation_id"] == "kimi-s-1"
    assert result["usage"] is None
    assert any(e.tool_name == "get_status" for e in events)


def test_kimi_deadline_drains_filled_stderr_and_terminates_fake_cli(tmp_path: Path) -> None:
    fake = tmp_path / "fake_kimi.py"
    fake.write_text(
        "import sys, time\n"
        "sys.stderr.write('progress ' * 300000)\n"
        "sys.stderr.flush()\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    task = _task()
    result = KimiBackend(command=[sys.executable, str(fake)], timeout_seconds=0.2).run(task, None, "只读")
    assert result["success"] is False
    assert result["error"] == "TIMEOUT"
    assert task.process is None


def test_kimi_stream_json_reports_error_without_quota_retry() -> None:
    proc = MagicMock()
    proc.stdout = iter([json.dumps({"type": "error", "message": "quota exceeded"}) + "\n"])
    proc.returncode = 1
    proc.poll.return_value = None
    proc.communicate.return_value = ("", "quota exceeded")
    with patch("subprocess.Popen", return_value=proc) as popen:
        result = KimiBackend(command="kimi.exe", auto=False).run(_task(), None, "只读")
    assert result["success"] is False
    assert result["error"] == "RESOURCE_EXHAUSTED"
    assert popen.call_count == 1


def test_chat_backend_config_defaults_to_kimi_and_accepts_explicit_file(tmp_path: Path) -> None:
    assert load_chat_backend_config(tmp_path / "missing.json") == {"backend": "kimi", "model": "kimi-code/k3"}
    config = tmp_path / "chat-backend.json"
    config.write_text(json.dumps({"backend": "agy", "model": "gemini-test"}), encoding="utf-8")
    assert load_chat_backend_config(config) == {"backend": "agy", "model": "gemini-test"}


def test_codex_backend_uses_run_local_mcp_and_qualified_session(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    config = tmp_path / "chat-backend.json"
    config.write_text('{"backend":"codex"}', encoding="utf-8")
    assert load_chat_backend_config(config) == {"backend": "codex", "model": ""}

    proc = MagicMock()
    proc.stdout = iter([
        json.dumps({"type": "thread.started", "thread_id": "codex-session-1"}) + "\n",
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "已看过农场。"}}) + "\n",
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7}}) + "\n",
    ])
    proc.stderr = iter([])
    proc.returncode = 0
    with patch.dict("os.environ", {"STARDEW_MCP_SURFACE": "life", "STARDEW_LIFE_MODE": "plan"}), \
         patch("subprocess.Popen", return_value=proc) as popen:
        result = CodexBackend(tmp_path, model="gpt-local").run(_task(), None, "只读观察")
    cmd = popen.call_args.args[0]
    assert result["success"] is True
    assert result["conversation_id"] == "codex-session-1"
    assert result["response"] == "已看过农场。"
    mcp_args = next(value for value in cmd if value.startswith("mcp_servers.stardew-companion.args="))
    assert json.loads(mcp_args.split("=", 1)[1])[4] == str(tmp_path)
    assert "mcp_servers.stardew-companion.env.STARDEW_MCP_SURFACE=\"life\"" in cmd
    assert "mcp_servers.stardew-companion.env.STARDEW_LIFE_MODE=\"plan\"" in cmd
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-local"


def test_codex_resume_uses_exact_thread_id(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    proc = MagicMock()
    proc.stdout = iter([json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "continued"}})])
    proc.stderr = iter([])
    proc.returncode = 0
    with patch("subprocess.Popen", return_value=proc) as popen:
        result = CodexBackend(tmp_path).run(_task(), "thread-for-this-save", "next turn")
    assert popen.call_args.args[0][1:4] == ["exec", "resume", "thread-for-this-save"]
    assert result["conversation_id"] == "thread-for-this-save"
    assert result["success"]


def test_codex_backend_cleans_up_process_after_progress_exception(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    proc = MagicMock()
    proc.poll.return_value = None
    task = _task()

    def fail_progress(_event):
        raise RuntimeError("callback failed")

    with patch("subprocess.Popen", return_value=proc), \
         patch.object(CodexBackend, "terminate") as terminate:
        result = CodexBackend(tmp_path, progress=fail_progress).run(task, None, "只读观察")

    assert result["success"] is False
    terminate.assert_called_once_with(proc)
    assert task.process is None


def _ok_proc() -> MagicMock:
    proc = MagicMock()
    proc.stdout = iter([json.dumps({"role": "assistant", "content": "ok"}) + "\n"])
    proc.returncode = 0
    proc.communicate.return_value = ("", "")
    return proc


def test_kimi_agent_file_is_only_passed_for_new_sessions(tmp_path: Path) -> None:
    """Official CLI rejects --agent-file with --session; bind the profile only at creation."""
    agent_file = tmp_path / "stardew-game.md"

    with patch("subprocess.Popen", return_value=_ok_proc()) as popen:
        KimiBackend(command="kimi.exe", agent_file=agent_file).run(_task(), None, "hi")
    cmd = popen.call_args.args[0]
    assert "--agent-file" in cmd
    assert cmd[cmd.index("--agent-file") + 1] == str(agent_file)

    with patch("subprocess.Popen", return_value=_ok_proc()) as popen:
        KimiBackend(command="kimi.exe", agent_file=agent_file).run(_task(), "session-1", "hi")
    resumed_cmd = popen.call_args.args[0]
    assert "--agent-file" not in resumed_cmd
    assert resumed_cmd[resumed_cmd.index("--session") + 1] == "session-1"


def test_chat_backend_config_keeps_agent_profile_fields(tmp_path: Path) -> None:
    config = tmp_path / "chat-backend.json"
    config.write_text(
        json.dumps({"backend": "kimi", "model": "kimi-code/k3", "agentFile": ".kimi-code/agents/stardew-game.md"}),
        encoding="utf-8",
    )
    loaded = load_chat_backend_config(config)
    assert loaded["agentFile"] == ".kimi-code/agents/stardew-game.md"
