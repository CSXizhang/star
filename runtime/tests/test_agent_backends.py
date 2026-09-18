import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from stardew_ai_runtime.agent_backends import KimiBackend
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
