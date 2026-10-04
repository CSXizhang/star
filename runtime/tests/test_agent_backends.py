import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stardew_ai_runtime.agent_backends import CodexBackend, KimiBackend, McodeBackend
from stardew_ai_runtime.agent_instructions import runtime_instructions
from stardew_ai_runtime.chat_backend_config import load_chat_backend_config


@pytest.fixture(autouse=True)
def _isolated_codex_config():
    # Backend tests never read the machine's user configuration or launch CLI.
    with patch("stardew_ai_runtime.agent_backends.unrelated_mcp_config",
               return_value=["-c", "mcp_servers.unrelated.enabled=false"]):
        yield


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
    assert load_chat_backend_config(config) == {"backend": "codex", "model": "", "effort": "low"}

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
        result = CodexBackend(tmp_path, model="gpt-local", effort="xhigh", http_provider="custom").run(_task(), None, "只读观察")
    cmd = popen.call_args.args[0]
    assert result["success"] is True
    assert result["conversation_id"] == "codex-session-1"
    assert result["response"] == "已看过农场。"
    mcp_args = next(value for value in cmd if value.startswith("mcp_servers.stardew-companion.args="))
    assert json.loads(mcp_args.split("=", 1)[1])[4] == str(tmp_path)
    assert "mcp_servers.stardew-companion.env.STARDEW_MCP_SURFACE=\"life\"" in cmd
    assert "mcp_servers.stardew-companion.env.STARDEW_LIFE_MODE=\"plan\"" in cmd
    assert 'sandbox_mode="danger-full-access"' in cmd
    assert "approval_policy=never" in cmd
    assert 'model_reasoning_effort="xhigh"' in cmd
    assert "model_providers.custom.supports_websockets=false" in cmd
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-local"
    assert "mcp_servers.unrelated.enabled=false" in cmd
    assert "features.shell_tool=false" in cmd
    assert "features.plugins=false" in cmd
    assert "features.apps=false" in cmd
    instructions_file = tmp_path / "config" / "agent-system-instructions.md"
    assert instructions_file.read_text(encoding="utf-8") == runtime_instructions()
    assert f"model_instructions_file={json.dumps(str(instructions_file))}" in cmd


def test_codex_resume_uses_exact_thread_id(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    proc = MagicMock()
    proc.stdout = iter([json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "continued"}})])
    proc.stderr = iter([])
    proc.returncode = 0
    with patch("subprocess.Popen", return_value=proc) as popen:
        result = CodexBackend(tmp_path, effort="high").run(_task(), "thread-for-this-save", "next turn")
    assert popen.call_args.args[0][1:4] == ["exec", "resume", "thread-for-this-save"]
    assert 'sandbox_mode="danger-full-access"' in popen.call_args.args[0]
    assert "approval_policy=never" in popen.call_args.args[0]
    assert 'model_reasoning_effort="high"' in popen.call_args.args[0]
    assert "mcp_servers.unrelated.enabled=false" in popen.call_args.args[0]
    assert "features.shell_tool=false" in popen.call_args.args[0]
    assert "features.plugins=false" in popen.call_args.args[0]
    assert any(arg.startswith("model_instructions_file=") for arg in popen.call_args.args[0])
    assert result["conversation_id"] == "thread-for-this-save"
    assert result["success"]


def test_codex_removes_shared_core_from_user_prompt(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    proc = MagicMock()
    proc.stdout = iter([json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "ok"}})])
    proc.stderr = iter([])
    proc.returncode = 0
    with patch("subprocess.Popen", return_value=proc) as popen:
        result = CodexBackend(tmp_path).run(_task(), None, runtime_instructions() + "\n只读观察")
    assert result["success"]
    assert popen.call_args.args[0][-1] == "只读观察"


def test_codex_config_inspection_failure_does_not_start_model(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    with patch("stardew_ai_runtime.agent_backends.unrelated_mcp_config",
               side_effect=RuntimeError("private config contents")), \
         patch("subprocess.Popen") as popen:
        result = CodexBackend(tmp_path).run(_task(), "existing", "只读观察")
    assert result["success"] is False
    assert result["error"] == "CODEX_GAME_PROFILE_FAILED"
    assert "private" not in json.dumps(result)
    popen.assert_not_called()


def test_codex_tool_approval_refusal_is_failure_even_when_cli_exits_zero(tmp_path):
    python = tmp_path / "runtime" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    proc = MagicMock()
    proc.stdout = iter([
        json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "submit_plan",
            "status": "failed", "error": {"message": "MCP tool call requires approval, but approval policy is never"}}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "正常待命"}}),
    ])
    proc.stderr = iter([])
    proc.returncode = 0
    with patch("subprocess.Popen", return_value=proc) as popen, patch.object(CodexBackend, "terminate") as terminate:
        result = CodexBackend(tmp_path).run(_task(), "existing-session", "继续")
    assert result["success"] is False and result["error"] == "TOOL_APPROVAL_REQUIRED"
    assert "submit_plan" in result["response"] and "未执行" in result["response"]
    assert popen.call_count == 1 and terminate.called
    assert "approval_policy=never" in popen.call_args.args[0]


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


def _mcode_proc(lines: list[dict], returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.stdout = iter([json.dumps(line) + "\n" for line in lines])
    proc.stderr = iter([stderr] if stderr else [])
    proc.returncode = returncode
    proc.poll.return_value = returncode
    return proc


def _run_mcode(backend: McodeBackend, proc: MagicMock, session_id=None, prompt="只读观察"):
    with patch("stardew_ai_runtime.agent_backends.McodeBackend._executable",
               return_value="mcode.exe"), \
         patch("subprocess.Popen", return_value=proc) as popen:
        return backend.run(_task(), session_id, prompt), popen


def test_chat_backend_config_defaults_mcode_to_local_model_and_low_effort(tmp_path: Path) -> None:
    config = tmp_path / "chat-backend.json"
    config.write_text('{"backend":"mcode"}', encoding="utf-8")
    assert load_chat_backend_config(config) == {"backend": "mcode", "model": "", "effort": "low"}


def test_mcode_backend_binds_run_local_mcp_workspace_and_normalizes_usage(tmp_path: Path) -> None:
    proc = _mcode_proc([
        {"type": "session.started", "sessionId": "mvs-session-1"},
        {"type": "item.completed", "item": {"type": "tool_call", "toolCall": {
            "name": "mcp__stardew-companion__get_status"}}},
        {"type": "item.completed", "item": {"type": "agent_message", "content": "农场还空着。"}},
        {"type": "exec.completed", "result": {"status": "succeeded", "sessionId": "mvs-session-1",
            "output": "农场还空着。", "usage": {"inputTokens": 666, "outputTokens": 2,
                                               "cacheReadTokens": 15984, "totalTokens": 668}}},
    ])
    events = []
    with patch.dict("os.environ", {"STARDEW_MCP_SURFACE": "life", "STARDEW_DECISION_TOKEN": "tok-1"}):
        result, popen = _run_mcode(
            McodeBackend(tmp_path, model="minimax/MiniMax-M3.1-Flash-Preview", effort="low",
                         progress=events.append), proc)
    cmd = popen.call_args.args[0]
    workspace = tmp_path / "data" / "mcode"
    assert cmd[0:2] == ["mcode.exe", "exec"]
    assert cmd[cmd.index("--cwd") + 1] == str(workspace)
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--permission") + 1] == "full"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--model") + 1] == "minimax/MiniMax-M3.1-Flash-Preview"
    assert "--session" not in cmd
    assert "只读观察" not in cmd
    assert cmd[cmd.index("--input") + 1] == "-"
    assert popen.call_args.kwargs["stdin"].closed
    assert result["success"] is True
    assert result["conversation_id"] == "mvs-session-1"
    assert result["response"] == "农场还空着。"
    # mcode reports input and cache separately; the shared shape counts cache inside input.
    assert result["usage"]["input_tokens"] == 666 + 15984
    assert result["usage"]["cache_read_tokens"] == 15984
    assert result["usage"]["total_tokens"] == 668 + 15984
    # The workspace file is this run's only game binding, and it carries the turn authority.
    profile = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))
    server = profile["mcpServers"]["stardew-companion"]
    assert server["args"][4] == str(tmp_path)
    assert server["env"]["STARDEW_MCP_SURFACE"] == "life"
    assert server["env"]["STARDEW_DECISION_TOKEN"] == "tok-1"
    assert any(e.tool_name == "get_status" and e.kind == "tool_started" for e in events)


def test_mcode_leaves_model_and_effort_to_the_cli_when_unset(tmp_path: Path) -> None:
    proc = _mcode_proc([{"type": "exec.completed", "result": {
        "status": "succeeded", "output": "好", "sessionId": "keep-me"}}])
    result, popen = _run_mcode(McodeBackend(tmp_path, effort="default"), proc, "keep-me", "继续")
    cmd = popen.call_args.args[0]
    assert "--model" not in cmd and "--effort" not in cmd
    assert cmd[cmd.index("--session") + 1] == "keep-me"
    assert result["conversation_id"] == "keep-me"
    assert result["success"] is True


def test_mcode_retries_once_with_a_fresh_session_when_resume_fails(tmp_path: Path) -> None:
    failed = _mcode_proc([], returncode=4, stderr="mcode exec failed: session not found\n")
    fresh = _mcode_proc([{"type": "exec.completed", "result": {
        "status": "succeeded", "output": "新会话已开工", "sessionId": "fresh-1"}}])
    with patch("stardew_ai_runtime.agent_backends.McodeBackend._executable", return_value="mcode.exe"), \
         patch("subprocess.Popen", side_effect=[failed, fresh]) as popen:
        result = McodeBackend(tmp_path).run(_task(), "gone-1", "继续")
    assert result["success"] is True
    assert result["conversation_id"] == "fresh-1"
    assert popen.call_count == 2
    assert "--session" in popen.call_args_list[0].args[0]
    assert "--session" not in popen.call_args_list[1].args[0]


@pytest.mark.parametrize("returncode,expected", [(2, "MCODE_REQUEST_INVALID"), (3, "MCODE_CONFIG_INVALID"),
                                                (6, "TIMEOUT"), (70, "MCODE_INTERNAL_ERROR")])
def test_mcode_maps_cli_exit_codes(tmp_path: Path, returncode: int, expected: str) -> None:
    proc = _mcode_proc([], returncode=returncode, stderr="mcode exec failed\n")
    result, _ = _run_mcode(McodeBackend(tmp_path), proc)
    assert result["success"] is False
    assert result["error"] == expected


def test_mcode_missing_cli_reports_install_hint_without_starting_process(tmp_path: Path) -> None:
    with patch("stardew_ai_runtime.agent_backends.McodeBackend._executable",
               side_effect=FileNotFoundError("Install MiniMax Code CLI (mcode) first")), \
         patch("subprocess.Popen") as popen:
        result = McodeBackend(tmp_path).run(_task(), None, "只读观察")
    assert result["success"] is False
    assert result["error"] == "MCODE_CLI_MISSING"
    assert "MiniMax Code CLI" in result["response"]
    popen.assert_not_called()


def test_mcode_rejects_unsupported_effort(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        McodeBackend(tmp_path, effort="turbo")


def test_mcode_deadline_terminates_fake_cli(tmp_path: Path) -> None:
    fake = tmp_path / "fake_mcode.py"
    fake.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    task = _task()
    backend = McodeBackend(tmp_path, command=sys.executable, timeout_seconds=0.2)
    backend._command = lambda *args, **kwargs: [sys.executable, str(fake)]  # type: ignore[method-assign]
    result = backend.run(task, None, "只读")
    assert result["success"] is False
    assert result["error"] == "TIMEOUT"
    assert task.process is None


def test_mcode_executable_prefers_cmd_sibling_over_powershell_shim(tmp_path: Path) -> None:
    ps1 = tmp_path / "mcode.ps1"
    ps1.write_text("# shim", encoding="utf-8")
    ps1.with_suffix(".cmd").write_text("@echo off", encoding="utf-8")
    with patch("shutil.which", return_value=str(ps1)):
        assert McodeBackend(tmp_path)._executable() == str(ps1.with_suffix(".cmd"))
    ps1.with_suffix(".cmd").unlink()
    with patch("shutil.which", return_value=str(ps1)):
        assert McodeBackend(tmp_path)._executable() == str(ps1)


def test_mcode_long_unicode_prompt_reaches_stdin_literally(tmp_path):
    import hashlib
    fake = tmp_path / "fake_mcode.py"
    fake.write_text(
        "import sys,json,hashlib\n"
        "text=sys.stdin.buffer.read().split(b'\\n\\n',1)[1]\n"
        "print(json.dumps({'type':'exec.completed','result':{'status':'succeeded',"
        "'sessionId':'fake','output':hashlib.sha256(text).hexdigest()}}))\n", encoding="utf-8")
    backend = McodeBackend(tmp_path, command=sys.executable)
    backend._command = lambda *args: [sys.executable, str(fake)]
    prompt = '中文 "quote" %PATH% & | < > ^\n' * 4000
    result = backend.run(_task(), None, prompt)
    assert result["success"]
    assert result["response"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("suffix", [".cmd", ".ps1"])
def test_mcode_npm_launch_uses_node_without_shell(tmp_path, suffix):
    prefix = tmp_path / "npm with spaces & symbols"
    entry = prefix / "node_modules/@minimax-ai/code/cli.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    node = prefix / "node.exe"
    node.touch()
    command = McodeBackend(tmp_path)._command(str(prefix / ("mcode" + suffix)), tmp_path, None)
    assert command[:3] == [str(node), str(entry), "exec"]
    assert command[command.index("--input") + 1] == "-"


def test_mcode_does_not_retry_session_error_after_turn_started(tmp_path):
    failed = _mcode_proc([
        {"type": "session.resumed", "sessionId": "session-1"},
        {"type": "item.completed", "item": {"type": "tool_call", "toolCall": {"name": "submit_plan"}}},
    ], returncode=4, stderr="session not found in tool response")
    result, popen = _run_mcode(McodeBackend(tmp_path), failed, "session-1")
    assert not result["success"]
    assert result["error"] == "MCODE_RUNTIME_FAILED"
    assert popen.call_count == 1


def test_mcode_missing_usage_fields_remain_unknown_and_zero_is_known():
    usage = McodeBackend._usage({"inputTokens": 10})
    assert usage["partial"]
    assert "output_tokens" not in usage and "total_tokens" not in usage and "cache_read_tokens" not in usage
    zero = McodeBackend._usage({"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "totalTokens": 0})
    assert zero["total_tokens"] == 0 and "partial" not in zero


def test_mcode_honors_provider_incomplete_usage(tmp_path):
    proc = _mcode_proc([{"type": "exec.completed", "result": {"status": "succeeded", "output": "ok",
        "usageIncomplete": True, "usage": {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 20, "totalTokens": 12}}}])
    result, _ = _run_mcode(McodeBackend(tmp_path), proc)
    assert result["success"] and result["usage"]["partial"]
    assert result["usage"]["total_tokens"] == 32
