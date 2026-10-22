"""agy 0.3.10 NDJSON transport; prompts never enter the command line.

The native protocol uses event=user/message and event=init, step_update, result
on stdout. It is not the similarly named Claude stream-json protocol.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .usage_meter import AgyTurnReceipts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgyProcessOutcome:
    stdout: str
    stderr: str
    conversation_id: str | None
    returncode: int | None
    cancelled: bool = False
    timed_out: bool = False


def agy_input_message(prompt: str) -> str:
    """One native user message; embedded newlines remain inside one JSON line."""
    return json.dumps({"event": "user", "message": {"role": "user", "content": prompt}},
                      ensure_ascii=False) + "\n"


def terminate_agy_process(proc: Any) -> None:
    """Stop this owned CLI and its Windows tool children before reaping it."""
    if proc.poll() is not None:
        return
    if os.name == "nt" and isinstance(getattr(proc, "pid", None), int):
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        proc.kill()


def run_agy_process(
    active_task: Any, command: Sequence[str], prompt: str, session_id: str | None = None,
    *, timeout_seconds: float = 600, cwd: str | Path | None = None,
    run_dir: Path | None = None, model: str = "", start_idx: int = -1,
    on_conversation: Callable[[str], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgyProcessOutcome:
    """Drain both pipes, send one stdin turn and expose CID before completion.

    command contains executable/model/effort/mode/permission options, but no
    print/input/output/log options. stdout in the outcome is the unwrapped final
    result JSON, compatible with the existing bridge's terminal parser.
    """
    if getattr(active_task, "cancelled", False):
        return AgyProcessOutcome("", "", session_id, None, cancelled=True)
    if any(arg in {"--print", "-p", "--prompt", "--input-format", "--output-format", "--log-file"}
           for arg in command):
        raise ValueError("agy command must omit prompt and transport options")
    tracker = None
    if run_dir is not None and getattr(active_task, "request_id", "") and getattr(active_task, "save_id", ""):
        tracker = AgyTurnReceipts(Path(run_dir), request_id=active_task.request_id,
                                  save_id=active_task.save_id, model=model,
                                  conversation_id=session_id, start_idx=start_idx,
                                  prompt=prompt)
        tracker.begin()
    cmd = [*command, "--input-format", "stream-json", "--output-format", "stream-json"]
    if tracker is not None:
        cmd.extend(["--log-file", str(tracker.log_file)])
    proc = None
    readers: list[threading.Thread] = []
    writer = None
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stderr = ""
    terminal: dict[str, Any] | None = None
    cid = session_id
    cancelled = timed_out = False
    deadline = time.monotonic() + timeout_seconds

    def read(stream: Any, channel: str) -> None:
        try:
            for line in stream:
                events.put((channel, line))
        except (OSError, ValueError):
            pass
        finally:
            events.put((channel, None))

    def handle_stdout(line: str, *, progress: bool = True) -> None:
        nonlocal cid, terminal
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("event") == "init" and isinstance(event.get("conversation_id"), str):
            cid = event["conversation_id"]
            active_task.provider_conversation_id = cid
            if tracker is not None:
                tracker.bind(cid)
            if on_conversation:
                on_conversation(cid)
        elif event.get("event") == "result" and isinstance(event.get("result"), dict):
            terminal = event["result"]
            returned_cid = terminal.get("conversation_id")
            cid = returned_cid if isinstance(returned_cid, str) and returned_cid else cid
            if isinstance(cid, str) and cid != getattr(active_task, "provider_conversation_id", None):
                active_task.provider_conversation_id = cid
                if tracker is not None:
                    tracker.bind(cid)
                if on_conversation:
                    on_conversation(cid)
        if tracker is not None:
            tracker.collect()
        if progress and on_event:
            on_event(event)

    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd is not None else None,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        active_task.process = proc
        for stream, channel in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            reader = threading.Thread(target=read, args=(stream, channel), daemon=True)
            reader.start()
            readers.append(reader)

        def write() -> None:
            try:
                proc.stdin.write(agy_input_message(prompt))
                proc.stdin.flush()
            except (OSError, ValueError):
                pass  # Exit/cancel closes the pipe; the reader still drains output.
            finally:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        done: set[str] = set()
        while len(done) < 2:
            if getattr(active_task, "cancelled", False):
                cancelled = True
                terminate_agy_process(proc)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                terminate_agy_process(proc)
                break
            try:
                channel, line = events.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                done.add(channel)
                continue
            if channel == "stderr":
                stderr = (stderr + line)[-65536:]
                continue
            handle_stdout(line)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            terminate_agy_process(proc)
            proc.wait(timeout=2)
        if cancelled or timed_out:
            # Cancellation may win the loop check after init/result was already
            # buffered. Keep its CID and receipts without sending late progress.
            for reader in readers:
                reader.join(timeout=2)
            while True:
                try:
                    channel, line = events.get_nowait()
                except queue.Empty:
                    break
                if line is not None and channel == "stdout":
                    handle_stdout(line, progress=False)
                elif line is not None and channel == "stderr":
                    stderr = (stderr + line)[-65536:]
        if terminal is None:
            terminal = {"conversation_id": cid, "status": "ERROR", "response": "",
                        "error": "CANCELLED" if cancelled else "TIMEOUT" if timed_out else "AGY_STREAM_RESULT_MISSING"}
        return AgyProcessOutcome(json.dumps(terminal, ensure_ascii=False), stderr,
                                 cid, proc.returncode, cancelled, timed_out)
    finally:
        if proc is not None:
            try:
                terminate_agy_process(proc)
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                logger.warning("Could not finish agy child cleanup")
        if writer is not None:
            writer.join(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        active_task.process = None
        if tracker is not None:
            tracker.finish("cancelled" if cancelled else "timeout" if timed_out else
                           "completed" if terminal and terminal.get("status") == "SUCCESS" else "failed")
