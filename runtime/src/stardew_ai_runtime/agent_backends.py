"""Small provider adapters used by the in-game chat bridge.

The bridge owns scheduling and MCP lifecycle.  Backends only translate a
provider's CLI process into the common result and progress shapes.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("stardew_ai_runtime.agent_backends")


@dataclass(frozen=True, slots=True)
class BackendProgress:
    provider: str
    kind: str
    message: str
    tool_name: str | None = None


@dataclass(slots=True)
class BackendResult:
    success: bool
    reply: str = ""
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    provider: str = "unknown"
    duration: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": self.success,
            "response": self.reply,
            "conversation_id": self.session_id,
            "usage": self.usage,
            "provider": self.provider,
            "duration": self.duration,
        }
        if self.error:
            result["error"] = self.error.get("code") or self.error.get("message")
            result["error_detail"] = self.error
        return result


class ActiveTaskLike(Protocol):
    request_id: str
    cancelled: bool
    process: Any


ProgressCallback = Callable[[BackendProgress], None]


class AgyBackend:
    name = "agy"

    def __init__(self, execute: Callable[..., dict[str, Any]]):
        self._execute = execute

    def run(self, active_task: ActiveTaskLike, session_id: str | None, prompt: str) -> dict[str, Any]:
        return self._execute(active_task, session_id, prompt)


class KimiBackend:
    name = "kimi"

    def __init__(
        self,
        model: str = "kimi-code/k3",
        command: str | list[str] | None = None,
        cwd: str | Path | None = None,
        progress: ProgressCallback | None = None,
        auto: bool = False,
        timeout_seconds: float = 600.0,
        agent: str | None = None,
        agent_file: str | Path | None = None,
    ):
        self.model = model
        self.command = command or os.getenv("KIMI_CMD") or "kimi.exe"
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.progress = progress
        self.timeout_seconds = timeout_seconds
        self.auto = auto
        # Agent profiles are bound at session creation; Kimi rejects --agent-file
        # (and --agent) together with --session/--continue (official CLI help).
        self.agent = agent
        self.agent_file = Path(agent_file) if agent_file else None

    def _emit(self, kind: str, message: str, tool_name: str | None = None) -> None:
        event = BackendProgress(self.name, kind, message[:160], tool_name)
        if self.progress:
            self.progress(event)
        logger.info("Kimi %s%s", message, f" (tool={tool_name})" if tool_name else "")

    @staticmethod
    def _usage(event: dict[str, Any]) -> dict[str, Any] | None:
        raw = event.get("usage")
        if not isinstance(raw, dict):
            return None
        keys = {
            "input_tokens": ("input_tokens", "prompt_tokens", "input"),
            "output_tokens": ("output_tokens", "completion_tokens", "output"),
            "cache_read_tokens": ("cache_read_tokens", "cache_read"),
            "thinking_tokens": ("thinking_tokens", "thinking"),
            "total_tokens": ("total_tokens", "total"),
        }
        result: dict[str, Any] = {}
        for target, names in keys.items():
            for name in names:
                if isinstance(raw.get(name), int):
                    result[target] = raw[name]
                    break
        return result or None

    @staticmethod
    def classify_error(message: str) -> tuple[str, str]:
        value = message.lower()
        if any(token in value for token in ("429", "rate limit", "too many requests", "rate_limit")):
            return "RATE_LIMIT_EXCEEDED", "Kimi 请求过于频繁，请稍后重试。"
        if any(token in value for token in ("unauthorized", "unauthenticated", "authentication", "login required", "token expired", "not logged in")):
            return "AUTHENTICATION_REQUIRED", "Kimi 登录或认证已失效，请先完成 Kimi 登录。"
        if any(token in value for token in ("resource_exhausted", "quota exceeded", "individual quota", "quota reached")):
            return "RESOURCE_EXHAUSTED", "AI 模型额度已用尽，任务已停止。"
        return "KIMI_REQUEST_FAILED", "Kimi 请求失败，请稍后重试。"

    def _event(self, event: dict[str, Any], chunks: list[str], session: list[str | None]) -> dict[str, Any] | None:
        kind = str(event.get("type") or "")
        if kind == "session.resume_hint" and event.get("session_id"):
            session[0] = str(event["session_id"])
            return None
        if event.get("session_id") and not session[0]:
            session[0] = str(event["session_id"])
        usage = self._usage(event)
        if usage:
            return usage
        tool = event.get("tool_name") or event.get("toolName") or event.get("tool") or event.get("name")
        if kind in {"tool_call", "tool_start", "tool_started", "tool_use"}:
            name = str(tool or "")
            label = "正在观察" if any(x in name for x in ("get_", "query_", "observe_", "discover_")) else "正在选择 / 提交作业"
            self._emit("tool_started", label, name or None)
            return None
        if event.get("role") == "assistant" or kind in {"assistant", "message", "text"}:
            content = event.get("content", event.get("text", ""))
            if isinstance(content, str) and content:
                chunks.append(content)
        if kind in {"error", "session.error"} or event.get("error"):
            return {"__error__": str(event.get("error") or event.get("message") or "Kimi 请求失败")}
        return None

    @staticmethod
    def terminate(proc: Any) -> None:
        """Stop only the process tree owned by this backend invocation."""
        if os.name == "nt" and isinstance(getattr(proc, "pid", None), int) and proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif proc.poll() is None:
            proc.kill()

    def run(self, active_task: ActiveTaskLike, session_id: str | None, prompt: str) -> dict[str, Any]:
        cmd = list(self.command) if isinstance(self.command, list) else [self.command]
        cmd.extend(["--model", self.model])
        if self.auto:
            cmd.append("--auto")
        if session_id is None:
            # New session only: bind the game-only agent profile. Resumed sessions
            # keep the profile bound at creation and reject these flags.
            if self.agent_file is not None:
                cmd.extend(["--agent-file", str(self.agent_file)])
            elif self.agent:
                cmd.extend(["--agent", self.agent])
        cmd.extend(["--output-format", "stream-json"])
        if session_id:
            cmd.extend(["--session", session_id])
        cmd.extend(["-p", prompt])
        start = time.monotonic()
        proc: Any = None
        readers: list[threading.Thread] = []
        if active_task.cancelled:
            return BackendResult(False, "任务已取消。", session_id, provider=self.name, duration=0).as_dict()
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(self.cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            active_task.process = proc
            self._emit("started", "已启动 Kimi")
            chunks: list[str] = []
            found_session: list[str | None] = [session_id]
            usage: dict[str, Any] | None = None
            events: queue.Queue[tuple[str, str | None]] = queue.Queue()
            stderr_errors: list[str] = []
            stderr_limit = 16 * 1024

            def read_stream(stream: Any, channel: str) -> None:
                try:
                    for line in stream:
                        events.put((channel, line))
                finally:
                    events.put((channel, None))

            for stream, channel in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                if stream is not None:
                    reader = threading.Thread(target=read_stream, args=(stream, channel), daemon=True)
                    reader.start()
                    readers.append(reader)

            stdout_eof = proc.stdout is None
            stderr_eof = proc.stderr is None
            fatal_error: tuple[str, str] | None = None
            deadline = start + self.timeout_seconds
            while not (stdout_eof and stderr_eof):
                if active_task.cancelled:
                    self.terminate(proc)
                    return BackendResult(False, "任务已取消。", found_session[0], provider=self.name, duration=time.monotonic() - start).as_dict()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.terminate(proc)
                    return BackendResult(False, "执行超时，操作已中止。", found_session[0], error={"code": "TIMEOUT", "message": "Kimi process deadline exceeded"}, provider=self.name, duration=time.monotonic() - start).as_dict()
                try:
                    channel, line = events.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    if channel == "stdout":
                        stdout_eof = True
                    else:
                        stderr_eof = True
                    continue
                if channel == "stderr":
                    code_text = line.lower()
                    if any(token in code_text for token in ("error", "failed", "quota", "resource_exhausted", "unauthorized", "authentication", "429", "rate limit")):
                        stderr_errors.append(line)
                        while sum(len(item) for item in stderr_errors) > stderr_limit:
                            stderr_errors.pop(0)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    parsed = self._event(event, chunks, found_session)
                    if parsed and "__error__" in parsed:
                        code, message = self.classify_error(parsed["__error__"])
                        fatal_error = (code, message)
                        self.terminate(proc)
                        break
                    if parsed:
                        usage = parsed

            if fatal_error:
                return BackendResult(False, fatal_error[1], found_session[0], usage, {"code": fatal_error[0], "message": fatal_error[1]}, self.name, time.monotonic() - start).as_dict()
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self.terminate(proc)
                return BackendResult(False, "执行超时，操作已中止。", found_session[0], error={"code": "TIMEOUT", "message": "Kimi process deadline exceeded"}, provider=self.name, duration=time.monotonic() - start).as_dict()
            duration = time.monotonic() - start
            if active_task.cancelled:
                return BackendResult(False, "任务已取消。", found_session[0], provider=self.name, duration=duration).as_dict()
            err_text = "".join(stderr_errors).strip()
            if proc.returncode != 0:
                code, message = self.classify_error(err_text) if err_text else (f"EXIT_CODE_{proc.returncode}", f"Kimi 执行出错（退出码 {proc.returncode}）。")
                return BackendResult(False, message, found_session[0], usage, {"code": code, "message": err_text[:300]}, self.name, duration).as_dict()
            reply = "".join(chunks).strip()
            if not reply:
                return BackendResult(False, "模型已执行但未返回具体汇报说明。", found_session[0], usage, {"code": "EMPTY_MODEL_RESPONSE", "message": "empty assistant response"}, self.name, duration).as_dict()
            self._emit("completed", "Kimi 已完成回复")
            return BackendResult(True, reply, found_session[0], usage, provider=self.name, duration=duration).as_dict()
        except subprocess.TimeoutExpired:
            try:
                self.terminate(proc)
            except Exception:
                pass
            return BackendResult(False, "执行超时 (10分钟)，操作已中止。", session_id, provider=self.name, duration=time.monotonic() - start).as_dict()
        except Exception as ex:
            return BackendResult(False, f"调用 Kimi 服务发生异常：{ex}", session_id, error={"code": "BACKEND_START_FAILED", "message": str(ex)}, provider=self.name, duration=time.monotonic() - start).as_dict()
        finally:
            for reader in readers:
                reader.join(timeout=1.0)
            active_task.process = None
