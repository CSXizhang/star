"""Lifetime of a bridge launched by the game, independent of WebSocket state."""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import os
import threading
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger("stardew_ai_runtime.owner_process")


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32), ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t), ("active_processes", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimitInformation), ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class _WindowsProcessLifetime:
    def __init__(self, owner_pid: int):
        if os.name != "nt":
            raise RuntimeError("--owner-pid requires Windows process handles")
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
        signatures = {
            "OpenProcess": ([dword, boolean, dword], handle),
            "WaitForSingleObject": ([handle, dword], dword),
            "CloseHandle": ([handle], boolean),
            "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], handle),
            "SetInformationJobObject": ([handle, ctypes.c_int32, ctypes.c_void_p, dword], boolean),
            "AssignProcessToJobObject": ([handle, handle], boolean),
            "GetCurrentProcess": ([], handle),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = args, result
        # SYNCHRONIZE only: the owner is observed, never terminated. Keeping this
        # handle pins its process identity even if Windows later reuses the PID.
        self._owner = self._kernel.OpenProcess(0x00100000, False, owner_pid)
        self._job = None
        if not self._owner:
            if ctypes.get_last_error() == 87:  # Owner already ended before startup.
                return
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._job = self._kernel.CreateJobObjectW(None, None)
            if not self._job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimitInformation()
            limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self._kernel.SetInformationJobObject(
                self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self._kernel.AssignProcessToJobObject(self._job, self._kernel.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            if self._job:
                self._kernel.CloseHandle(self._job)
            self.close()
            raise
        # This non-inheritable handle must stay open for the entire process
        # lifetime. Closing it here would kill the bridge itself. Windows closes
        # it on normal exit, os._exit, or a launcher Kill(), killing only this
        # bridge and the children it created after joining the unnamed job.

    def has_exited(self) -> bool:
        if not self._owner:
            return True
        status = self._kernel.WaitForSingleObject(self._owner, 0)
        if status == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return status == 0

    def close(self) -> None:
        if self._owner:
            self._kernel.CloseHandle(self._owner)
            self._owner = None


class OwnerProcessGuard:
    """Cancel on owner exit, then bound cleanup even if asyncio/threads stall."""

    def __init__(self, owner_pid: int, *, shutdown_timeout: float = 5.0):
        if not 0 < owner_pid <= 0xFFFFFFFF:
            raise ValueError("owner PID must be a positive Windows process ID")
        self._lifetime = _WindowsProcessLifetime(owner_pid)
        self._shutdown_timeout = shutdown_timeout
        self._closed = threading.Event()
        self.owner_exited = threading.Event()
        self._lock = threading.Lock()
        self._cancel: Callable[[], None] | None = None
        logger.info("Game owner guard active (owner_pid=%d, shutdown_timeout=%.1fs)", owner_pid, shutdown_timeout)
        self._thread = threading.Thread(target=self._watch, name="game-owner-watch", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        try:
            while not self._closed.is_set():
                # A failed retained-handle wait also stops this owned service;
                # it must never silently turn into an unmonitored daemon.
                try:
                    exited = self._lifetime.has_exited()
                except OSError:
                    exited = True
                if exited:
                    with self._lock:
                        self.owner_exited.set()
                        cancel = self._cancel
                    if cancel is not None:
                        cancel()
                    # Independent of the asyncio loop/default executor. The
                    # unnamed job cleans model/MCP descendants on this exit too.
                    if not self._closed.wait(self._shutdown_timeout):
                        os._exit(0)
                    return
                self._closed.wait(0.1)
        finally:
            self._lifetime.close()

    async def run(self, run_bridge: Callable[[asyncio.Event], Coroutine[Any, Any, None]]) -> None:
        if self.owner_exited.is_set():
            return
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        task = asyncio.create_task(run_bridge(stop))

        def stop_bridge() -> None:
            logger.info("Game owner ended; cancelling owned bridge (forced exit after %.1fs if needed)", self._shutdown_timeout)
            stop.set()
            if not task.done():
                task.cancel()

        def cancel() -> None:
            with contextlib.suppress(RuntimeError):  # Loop may have just finished.
                loop.call_soon_threadsafe(stop_bridge)

        with self._lock:
            self._cancel = cancel
            already_exited = self.owner_exited.is_set()
        if already_exited:
            cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not self.owner_exited.is_set():
                raise
        finally:
            with self._lock:
                self._cancel = None

    def close(self) -> None:
        # Call only after asyncio.run, including its default-executor shutdown,
        # has finished. Otherwise a blocked provider thread could escape the
        # bounded fallback while Python is still waiting to exit.
        self._closed.set()
        self._thread.join(timeout=1.0)
