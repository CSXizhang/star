"""Game-owned lifetime must not depend on sockets, provider threads, or PID lookup."""
import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from stardew_ai_runtime import owner_process


class FakeLifetime:
    def __init__(self):
        self.exited = threading.Event()
        self.close = Mock()

    def has_exited(self):
        return self.exited.is_set()


def test_owner_exit_cancels_bridge_and_passes_stop_event(monkeypatch):
    lifetime = FakeLifetime()
    factory = Mock(return_value=lifetime)
    monkeypatch.setattr(owner_process, "_WindowsProcessLifetime", factory)
    force_exit = Mock()
    monkeypatch.setattr(owner_process.os, "_exit", force_exit)
    guard = owner_process.OwnerProcessGuard(123)

    async def scenario():
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def bridge(stop):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert stop.is_set()
                cleaned.set()

        task = asyncio.create_task(guard.run(bridge))
        await started.wait()
        lifetime.exited.set()
        await asyncio.wait_for(task, 2)
        assert cleaned.is_set() and guard.owner_exited.is_set()

    try:
        asyncio.run(scenario())
    finally:
        guard.close()
    factory.assert_called_once_with(123)
    lifetime.close.assert_called_once()
    force_exit.assert_not_called()


def test_exited_owner_never_starts_bridge(monkeypatch):
    lifetime = FakeLifetime()
    lifetime.exited.set()
    monkeypatch.setattr(owner_process, "_WindowsProcessLifetime", lambda pid: lifetime)
    guard = owner_process.OwnerProcessGuard(123)
    try:
        assert guard.owner_exited.wait(2)
        bridge = Mock()
        asyncio.run(guard.run(bridge))
        bridge.assert_not_called()
    finally:
        guard.close()


def test_external_cancellation_is_not_swallowed(monkeypatch):
    lifetime = FakeLifetime()
    monkeypatch.setattr(owner_process, "_WindowsProcessLifetime", lambda pid: lifetime)
    guard = owner_process.OwnerProcessGuard(123)

    async def scenario():
        started = asyncio.Event()

        async def bridge(stop):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(guard.run(bridge))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not guard.owner_exited.is_set()

    try:
        asyncio.run(scenario())
    finally:
        guard.close()


def test_transient_socket_failure_reconnects_while_owner_is_alive(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from stardew_ai_runtime.chat_bridge import ChatBridge
    from stardew_ai_runtime.websocket_client import WebSocketError

    lifetime = FakeLifetime()
    monkeypatch.setattr(owner_process, "_WindowsProcessLifetime", lambda pid: lifetime)
    guard = owner_process.OwnerProcessGuard(123)
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    sleep = asyncio.sleep

    async def scenario():
        reconnected = asyncio.Event()
        ws = SimpleNamespace(close=AsyncMock())
        connect = AsyncMock(side_effect=[WebSocketError("temporary disconnect"), ws])

        async def receive(*args):
            reconnected.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("stardew_ai_runtime.chat_bridge.resolve_discovery", lambda *a, **k: {
            "host": "127.0.0.1", "port": 1, "sessionToken": "fixture", "saveId": "farm"})
        monkeypatch.setattr("stardew_ai_runtime.chat_bridge.WebSocketClient.connect", connect)
        monkeypatch.setattr("stardew_ai_runtime.chat_bridge.asyncio.sleep", lambda delay: sleep(0.001))
        bridge._receive_loop = receive
        task = asyncio.create_task(guard.run(bridge.run))
        await asyncio.wait_for(reconnected.wait(), 2)
        assert connect.await_count == 2
        assert not task.done() and not guard.owner_exited.is_set()
        lifetime.exited.set()
        await asyncio.wait_for(task, 2)
        ws.close.assert_awaited_once()

    try:
        asyncio.run(scenario())
    finally:
        guard.close()


def test_handle_wait_failure_cannot_leave_unmonitored_service(monkeypatch):
    lifetime = FakeLifetime()
    lifetime.has_exited = Mock(side_effect=OSError("wait failed"))
    monkeypatch.setattr(owner_process, "_WindowsProcessLifetime", lambda pid: lifetime)
    forced = threading.Event()
    monkeypatch.setattr(owner_process.os, "_exit", lambda code: forced.set())
    guard = owner_process.OwnerProcessGuard(123, shutdown_timeout=0.05)
    try:
        assert forced.wait(2)
        assert guard.owner_exited.is_set()
    finally:
        guard.close()


def test_manual_main_keeps_original_resident_path(tmp_path, monkeypatch):
    from stardew_ai_runtime import chat_bridge

    factory = Mock(side_effect=AssertionError("manual launch must not acquire owner/job"))
    monkeypatch.setattr(chat_bridge, "OwnerProcessGuard", factory)
    calls = []

    async def run(self, stop_event=None):
        calls.append(stop_event)

    monkeypatch.setattr(chat_bridge.ChatBridge, "run", run)
    try:
        chat_bridge.main(["--run-dir", str(tmp_path)])
    finally:
        chat_bridge.remove_chat_bridge_file_logging()
    assert calls == [None]
    factory.assert_not_called()


def test_explicit_main_arguments_can_release_embedded_monitor(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from stardew_ai_runtime import chat_bridge

    guard = SimpleNamespace(owner_exited=threading.Event(), run=AsyncMock(), close=Mock())
    factory = Mock(return_value=guard)
    monkeypatch.setattr(chat_bridge, "OwnerProcessGuard", factory)
    try:
        chat_bridge.main(["--run-dir", str(tmp_path), "--owner-pid", "123"])
    finally:
        chat_bridge.remove_chat_bridge_file_logging()
    factory.assert_called_once_with(123)
    guard.run.assert_awaited_once()
    guard.close.assert_called_once()


@pytest.mark.parametrize("pid", ["0", "-1", "4294967296"])
def test_main_rejects_invalid_owner_before_startup(pid):
    from stardew_ai_runtime.chat_bridge import main

    with pytest.raises(SystemExit) as result:
        main(["--owner-pid", pid])
    assert result.value.code == 2


def test_windows_wait_retains_opened_owner_identity(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Win32 ctypes API")
    kernel = Mock()
    kernel.OpenProcess.return_value = 8765
    kernel.CreateJobObjectW.return_value = 5555
    kernel.SetInformationJobObject.return_value = True
    kernel.AssignProcessToJobObject.return_value = True
    kernel.WaitForSingleObject.side_effect = [0x102, 0]
    monkeypatch.setattr(owner_process.ctypes, "WinDLL", lambda *a, **k: kernel)
    lifetime = owner_process._WindowsProcessLifetime(123)
    assert not lifetime.has_exited()
    assert lifetime.has_exited()
    lifetime.close()
    kernel.OpenProcess.assert_called_once_with(0x00100000, False, 123)
    assert [call.args for call in kernel.WaitForSingleObject.call_args_list] == [(8765, 0), (8765, 0)]
    kernel.CloseHandle.assert_called_once_with(8765)  # Never close the live self-job.


def _wait_for_file(path, process, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            value = path.read_text()
            if value:
                return value
        if process.poll() is not None:
            raise AssertionError(f"fixture exited {process.returncode}: {process.stderr.read()}")
        time.sleep(0.02)
    raise AssertionError("fixture startup timed out")


def _wait_for_pid_exit(pid, timeout=3):
    # Wait on one retained handle rather than repeatedly polling the PID.
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87
        return
    try:
        assert kernel.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        kernel.CloseHandle(handle)


def _launch_owned_fixture(tmp_path, owner_pid, mode):
    marker = tmp_path / "ready"
    descendants = tmp_path / "descendants"
    child_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "grandchild=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(descendants)!r}).write_text(str(grandchild.pid)); "
        f"time.sleep(60) if {mode!r} != 'detached-grandchild' else None"
    )
    code = f"""
import asyncio, subprocess, sys, time
from pathlib import Path
from stardew_ai_runtime.owner_process import OwnerProcessGuard
guard = OwnerProcessGuard({owner_pid}, shutdown_timeout=0.3)
child = subprocess.Popen([sys.executable, '-c', {child_code!r}])
Path({str(marker)!r}).write_text(str(child.pid))
async def bridge(stop):
    if {mode!r} == 'event-loop-blocked':
        time.sleep(60)
    elif {mode!r} == 'executor-blocked':
        await asyncio.to_thread(time.sleep, 60)
    else:
        try:
            await asyncio.Event().wait()
        finally:
            if {mode!r} == 'cleanup-blocked':
                await asyncio.sleep(60)
try:
    asyncio.run(guard.run(bridge))
finally:
    guard.close()
"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(Path(owner_process.__file__).resolve().parents[1])
    process = subprocess.Popen([sys.executable, "-u", "-c", code], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    child_pid = int(_wait_for_file(marker, process))
    grandchild_pid = int(_wait_for_file(descendants, process))
    return process, child_pid, grandchild_pid


@pytest.mark.skipif(sys.platform != "win32", reason="Windows real process/job lifetime")
@pytest.mark.parametrize("mode", ["graceful", "event-loop-blocked", "executor-blocked", "cleanup-blocked", "detached-grandchild"])
def test_owner_exit_stops_only_owned_process_and_descendants(tmp_path, mode):
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    manual = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned = None
    try:
        owned, child_pid, grandchild_pid = _launch_owned_fixture(tmp_path, owner.pid, mode)
        assert owned.poll() is None and owner.poll() is None and manual.poll() is None
        started = time.monotonic()
        owner.terminate()
        owner.wait(timeout=3)
        assert owned.wait(timeout=3) == 0, owned.stderr.read()
        assert time.monotonic() - started < 3
        _wait_for_pid_exit(child_pid)
        _wait_for_pid_exit(grandchild_pid)
        assert manual.poll() is None
    finally:
        for process in (owned, owner, manual):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
        if owned is not None:
            owned.stderr.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows real CLI lifetime")
def test_main_guard_remains_armed_during_interpreter_thread_shutdown(tmp_path):
    marker = tmp_path / "ready"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    code = f"""
import sys, threading, time
from pathlib import Path
from stardew_ai_runtime import chat_bridge
from stardew_ai_runtime.owner_process import OwnerProcessGuard
chat_bridge.OwnerProcessGuard = lambda pid: OwnerProcessGuard(pid, shutdown_timeout=0.3)
async def run(self, stop_event=None):
    threading.Thread(target=time.sleep, args=(60,), daemon=False).start()
    Path({str(marker)!r}).write_text('ready')
chat_bridge.ChatBridge.run = run
sys.argv = ['fixture', '--run-dir', {str(tmp_path)!r}, '--owner-pid', '{owner.pid}']
chat_bridge.main()
"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(Path(owner_process.__file__).resolve().parents[1])
    owned = subprocess.Popen([sys.executable, "-u", "-c", code], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for_file(marker, owned)
        assert owned.poll() is None
        owner.terminate()
        owner.wait(timeout=3)
        assert owned.wait(timeout=3) == 0, owned.stderr.read()
    finally:
        for process in (owned, owner):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        owned.stderr.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows real process/job lifetime")
def test_launcher_forced_exit_also_cleans_job_without_touching_owner(tmp_path):
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned = None
    try:
        owned, child_pid, grandchild_pid = _launch_owned_fixture(tmp_path, owner.pid, "graceful")
        owned.kill()
        owned.wait(timeout=3)
        _wait_for_pid_exit(child_pid)
        _wait_for_pid_exit(grandchild_pid)
        assert owner.poll() is None
    finally:
        for process in (owned, owner):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
        if owned is not None:
            owned.stderr.close()
