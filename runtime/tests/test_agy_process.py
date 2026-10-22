"""Native agy event protocol and interruption checks, without model calls."""
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stardew_ai_runtime.agy_process import agy_input_message, run_agy_process

CID = "00000000-0000-4000-8000-000000000123"


@pytest.fixture(autouse=True)
def _isolate_provider_db(monkeypatch):
    monkeypatch.setattr("stardew_ai_runtime.usage_meter.read_agy_generations", lambda *a, **k: [])
    monkeypatch.setattr("stardew_ai_runtime.usage_meter._agy_generation_boundary", lambda *a, **k: (-1, 0))


def _task():
    return SimpleNamespace(request_id="agy-test", save_id="Save1", cancelled=False, process=None)


def _fake(tmp_path: Path, body: str):
    path = tmp_path / "fake_agy.py"
    path.write_text("import json,sys,time\n" + f"CID={CID!r}\n" + body, encoding="utf-8")
    return [sys.executable, "-X", "utf8", str(path)]


def test_native_input_is_one_ndjson_record_with_unicode_newlines_and_quotes():
    prompt = "中文\n\"引号\"\\路径"
    data = agy_input_message(prompt)
    assert len(data.splitlines()) == 1
    assert json.loads(data) == {"event": "user", "message": {"role": "user", "content": prompt}}


def test_long_stdin_prompt_native_events_early_cid_and_terminal_json(tmp_path):
    prompt = "田地\n\"正常工具\"\\" * 10000
    observed = tmp_path / "input.json"
    cmd = _fake(tmp_path,
        f"print(json.dumps({{'event':'init','conversation_id':CID}}),flush=True)\n"
        "message=json.loads(sys.stdin.readline())\n"
        f"open({str(observed)!r},'w',encoding='utf-8').write(json.dumps(message,ensure_ascii=False))\n"
        "assert sys.stdin.read()==''\n"
        "assert '--print' not in sys.argv and '--input-format' in sys.argv\n"
        "print(json.dumps({'event':'step_update','step_update':{'tool_name':'submit_plan'}}),flush=True)\n"
        f"print(json.dumps({{'event':'result','result':{{'conversation_id':CID,'status':'SUCCESS','response':'完成','usage':{{'input_tokens':100,'output_tokens':10}}}}}}),flush=True)\n")
    task = _task()
    events = []
    cids = []
    result = run_agy_process(task, cmd, prompt, timeout_seconds=5,
                             on_conversation=cids.append, on_event=events.append)
    assert json.loads(observed.read_text(encoding="utf-8"))["message"]["content"] == prompt
    assert cids == [CID] and task.provider_conversation_id == CID
    assert any(e["event"] == "step_update" for e in events)
    assert json.loads(result.stdout)["status"] == "SUCCESS"
    assert json.loads(result.stdout)["response"] == "完成"
    assert result.returncode == 0 and task.process is None


def test_filled_stderr_does_not_block_large_stdin_or_terminal(tmp_path):
    cmd = _fake(tmp_path,
        "sys.stderr.write('progress '*300000);sys.stderr.flush()\n"
        "message=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'ok'}}),flush=True)\n")
    result = run_agy_process(_task(), cmd, "中" * 50000, timeout_seconds=5)
    assert result.returncode == 0
    assert json.loads(result.stdout)["response"] == "ok"
    assert len(result.stderr) <= 65536


def test_cancel_after_early_cid_terminates_child_and_preserves_association(tmp_path):
    cmd = _fake(tmp_path, "print(json.dumps({'event':'init','conversation_id':CID}),flush=True)\n"
                "time.sleep(10)\n")
    task = _task()
    child = []

    def cancel(cid):
        child.append(task.process)
        task.cancelled = True

    result = run_agy_process(task, cmd, "很长输入" * 100000, timeout_seconds=5, on_conversation=cancel)
    assert result.cancelled and result.conversation_id == CID
    assert json.loads(result.stdout)["error"] == "CANCELLED"
    assert child[0].poll() is not None and task.process is None


def test_timeout_terminates_child_and_missing_terminal_is_explicit(tmp_path):
    cmd = _fake(tmp_path, "print(json.dumps({'event':'init','conversation_id':CID}),flush=True)\n"
                "time.sleep(10)\n")
    result = run_agy_process(_task(), cmd, "只读", timeout_seconds=0.3)
    assert result.timed_out and result.conversation_id == CID
    assert json.loads(result.stdout)["error"] == "TIMEOUT"
    result = run_agy_process(_task(), _fake(tmp_path, "print('{}',flush=True)\n"), "只读", timeout_seconds=5)
    assert json.loads(result.stdout)["error"] == "AGY_STREAM_RESULT_MISSING"


def test_cancel_before_spawn_makes_no_process_or_receipt(tmp_path):
    task = _task()
    task.cancelled = True
    result = run_agy_process(task, ["does-not-exist"], "只读", run_dir=tmp_path)
    assert result.cancelled and task.process is None
    assert not (tmp_path / "data").exists()


def test_terminal_only_cid_is_bound_and_private_receipt_omits_prompt(tmp_path):
    task = _task()
    result = run_agy_process(task, _fake(tmp_path,
        "print(json.dumps({'event':'result','result':{'conversation_id':CID,'status':'SUCCESS','response':'ok'}}),flush=True)\n"),
        "私有输入不进账本", run_dir=tmp_path, timeout_seconds=5)
    assert result.conversation_id == CID and task.provider_conversation_id == CID
    data = (tmp_path / "data" / "agy-turns.jsonl").read_text(encoding="utf-8")
    states = [json.loads(line) for line in data.splitlines()]
    assert states[0]["conversationId"] is None and states[0]["status"] == "started"
    assert states[-1]["conversationId"] == CID and states[-1]["status"] == "completed"
    assert "私有输入不进账本" not in data


def test_cancel_persists_known_cid_for_owned_turn_without_terminal(tmp_path):
    task = _task()
    result = run_agy_process(task, _fake(tmp_path,
        "print(json.dumps({'event':'init','conversation_id':CID}),flush=True)\ntime.sleep(10)\n"),
        "只读", run_dir=tmp_path, timeout_seconds=5,
        on_conversation=lambda cid: setattr(task, "cancelled", True))
    assert result.cancelled
    states = [json.loads(line) for line in (tmp_path / "data" / "agy-turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert states[-1]["status"] == "cancelled" and states[-1]["conversationId"] == CID


@pytest.mark.skipif(os.name != "nt", reason="Windows child-tree cleanup")
def test_cancel_terminates_owned_tool_child_process_tree(tmp_path):
    import ctypes
    from ctypes import wintypes

    child_file = tmp_path / "tool-child.pid"
    cmd = _fake(tmp_path,
        "import subprocess\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],creationflags=subprocess.CREATE_NO_WINDOW)\n"
        f"open({str(child_file)!r},'w').write(str(child.pid))\n"
        "print(json.dumps({'event':'init','conversation_id':CID}),flush=True)\ntime.sleep(30)\n")
    task = _task()
    result = run_agy_process(task, cmd, "只读", timeout_seconds=5,
        on_conversation=lambda cid: setattr(task, "cancelled", True))
    assert result.cancelled
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.restype = wintypes.HANDLE
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x1000, False, int(child_file.read_text()))
    if handle:
        exit_code = wintypes.DWORD()
        try:
            assert api.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            assert exit_code.value != 259  # STILL_ACTIVE
        finally:
            api.CloseHandle(handle)


@pytest.mark.parametrize("event", ["init", "result"])
def test_cancel_race_preserves_already_buffered_cid_without_late_progress(monkeypatch, event):
    task = _task()
    payload = {"event": "init", "conversation_id": CID} if event == "init" else {
        "event": "result", "result": {"conversation_id": CID, "status": "SUCCESS", "response": "ok"}}
    proc = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(payload) + "\n"),
                           stderr=io.StringIO(), poll=Mock(return_value=0), wait=Mock(return_value=0), returncode=0)

    def spawn(*args, **kwargs):
        task.cancelled = True
        return proc

    monkeypatch.setattr("stardew_ai_runtime.agy_process.subprocess.Popen", spawn)
    cid_callback, progress = Mock(), Mock()
    result = run_agy_process(task, ["fake"], "只读", on_conversation=cid_callback, on_event=progress)
    assert result.cancelled and result.conversation_id == CID and task.provider_conversation_id == CID
    cid_callback.assert_called_once_with(CID)
    progress.assert_not_called()
