"""Small provider adapters used by the in-game chat bridge.

The bridge owns scheduling and MCP lifecycle.  Backends only translate a
provider's CLI process into the common result and progress shapes.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .agent_instructions import runtime_instructions
from .codex_game_profile import game_tool_config, unrelated_mcp_config, write_game_instructions
from .codex_usage import CodexUsageMeter

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


class CodexBackend:
    """Use the locally authenticated Codex CLI and bind MCP to this game run."""

    name = "codex"

    @staticmethod
    def _approval_denial(item: dict[str, Any]) -> str | None:
        """Recognize a CLI tool error, never infer denial from agent prose."""
        error = item.get("error")
        result = item.get("result")
        if not error and not (isinstance(result, dict) and result.get("isError")) and item.get("status") != "failed":
            return None
        values = [error]
        if isinstance(result, dict) and (result.get("isError") or item.get("status") == "failed"):
            values.extend(result.get("content") or [])
        for value in values:
            message = value.get("message", value.get("text", "")) if isinstance(value, dict) else value
            if isinstance(message, str) and "MCP tool call requires approval, but approval policy is never" in message:
                return message
        return None

    @staticmethod
    def terminate(proc: Any) -> None:
        KimiBackend.terminate(proc)

    def __init__(self, run_dir: Path, model: str = "", command: str = "codex.exe",
                 progress: ProgressCallback | None = None, timeout_seconds: float = 600.0,
                 effort: str | None = None, http_provider: str | None = None):
        self.run_dir = Path(run_dir)
        self.model = model
        self.command = command
        self.progress = progress
        self.timeout_seconds = timeout_seconds
        if effort not in {None, "default", "low", "medium", "high", "xhigh"}:
            raise ValueError("Unsupported Codex reasoning effort")
        self.effort = effort
        if http_provider is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", http_provider):
            raise ValueError("Invalid Codex HTTP provider identifier")
        self.http_provider = http_provider

    def run(self, active_task: ActiveTaskLike, session_id: str | None, prompt: str) -> dict[str, Any]:
        meter = CodexUsageMeter(session_id)
        result = self._run(active_task, session_id, prompt)
        result.update(meter.finish(result.get("conversation_id"), result.get("usage")))
        return result

    def _run(self, active_task: ActiveTaskLike, session_id: str | None, prompt: str) -> dict[str, Any]:
        start = time.monotonic()
        python = self.run_dir / "runtime" / "python" / "python.exe"
        if not python.is_file():
            return BackendResult(False, "游戏运行环境不完整。", session_id,
                                 error={"code": "BACKEND_START_FAILED", "message": "bundled Python missing"},
                                 provider=self.name).as_dict()
        try:
            inherited_mcp = unrelated_mcp_config(self.command, self.run_dir)
            instructions = getattr(active_task, "instructions_text", None)
            if not isinstance(instructions, str) or not instructions:
                instructions = runtime_instructions()
            instructions_file = write_game_instructions(self.run_dir, instructions)
            prompt = prompt.replace(instructions, "").strip()
        except Exception:
            # Do not start a model with an unverified inherited tool surface.
            # CLI configuration can include secrets, so keep errors generic.
            logger.warning("Could not prepare the Codex game tool profile")
            return BackendResult(False, "GPT 游戏工具配置失败，请检查本机 Codex 配置。", session_id,
                                 error={"code": "CODEX_GAME_PROFILE_FAILED",
                                        "message": "Could not prepare run-local Codex game profile"},
                                 provider=self.name).as_dict()
        # CLI -c overrides only this invocation. Never edit the user's MCP config.
        mcp = [
            "-c", "mcp_servers.stardew-companion.enabled=true",
            "-c", f'mcp_servers.stardew-companion.command={json.dumps(str(python))}',
            "-c", 'mcp_servers.stardew-companion.args=' + json.dumps([
                "-B", "-m", "stardew_ai_runtime.mcp_server", "--run-dir", str(self.run_dir),
                "--surface", "life" if os.environ.get("STARDEW_MCP_SURFACE") == "life" else "light",
            ]),
        ]
        # Codex sanitizes the environment inherited by MCP servers. Pass the
        # bridge's turn-scoped authority explicitly to this invocation only.
        for key in ("STARDEW_MCP_SURFACE", "STARDEW_LIFE_MODE",
                    "STARDEW_LIFE_PROPOSAL_ID", "STARDEW_LIFE_TURN_ID", "STARDEW_LIFE_SAVE_ID", "STARDEW_DECISION_TOKEN"):
            value = os.environ.get(key)
            if value is not None:
                mcp.extend(["-c", f'mcp_servers.stardew-companion.env.{key}={json.dumps(value)}'])
        cmd = [self.command, "exec"]
        if session_id:
            cmd.extend(["resume", session_id])
        # Resume accepts configuration overrides but not the new-session -s flag.
        # Keep this bridge-owned process in the same sandbox on every turn.
        cmd.extend(["--json", "--skip-git-repo-check", "-c", "approval_policy=never",
                    "-c", 'sandbox_mode="danger-full-access"',
                    *game_tool_config(), *inherited_mcp,
                    "-c", f"model_instructions_file={json.dumps(str(instructions_file))}", *mcp])
        if self.model:
            cmd.extend(["-m", self.model])
        if self.effort and self.effort != "default":
            cmd.extend(["-c", f'model_reasoning_effort={json.dumps(self.effort)}'])
        if self.http_provider:
            # Optional run-local workaround for a proxy which times out on the
            # Responses WebSocket transport. Keep auth, endpoint and approvals.
            cmd.extend(["-c", f"model_providers.{self.http_provider}.supports_websockets=false"])
        cmd.append(prompt)
        proc: Any = None
        try:
            proc = subprocess.Popen(cmd, cwd=str(self.run_dir), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            active_task.process = proc
            if self.progress:
                self.progress(BackendProgress(self.name, "started", "已启动 GPT"))
            events: queue.Queue[tuple[str, str | None]] = queue.Queue()

            def read(stream: Any, channel: str) -> None:
                try:
                    for line in stream:
                        events.put((channel, line))
                finally:
                    events.put((channel, None))

            readers = [threading.Thread(target=read, args=(stream, channel), daemon=True)
                       for stream, channel in ((proc.stdout, "stdout"), (proc.stderr, "stderr"))]
            for reader in readers:
                reader.start()
            done = set()
            reply = ""
            usage = None
            errors: list[str] = []
            deadline = start + self.timeout_seconds
            while len(done) < 2 and time.monotonic() < deadline:
                if active_task.cancelled:
                    KimiBackend.terminate(proc)
                    return BackendResult(False, "任务已取消。", session_id, provider=self.name,
                                         duration=time.monotonic() - start).as_dict()
                try:
                    channel, line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    done.add(channel)
                    continue
                if channel == "stderr":
                    errors.append(line[:500])
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "thread.started":
                    session_id = event.get("thread_id") or session_id
                elif kind == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message":
                        reply = str(item.get("text") or reply)
                    elif item.get("type") == "mcp_tool_call":
                        denial = self._approval_denial(item)
                        if denial:
                            # A CLI approval refusal never reached MCP. Stop this
                            # invocation, preserve policy, and surface the blocker.
                            self.terminate(proc)
                            tool = str(item.get("tool") or "MCP tool")
                            return BackendResult(False, f"工具 {tool} 被审批策略拒绝，未执行；自主工作已暂停。",
                                session_id, usage, {"code": "TOOL_APPROVAL_REQUIRED", "message": denial},
                                self.name, time.monotonic() - start).as_dict()
                        if self.progress:
                            self.progress(BackendProgress(self.name, "tool_started", "正在处理游戏任务",
                                                          item.get("tool")))
                elif kind == "turn.completed":
                    usage = event.get("usage")
                elif kind == "turn.failed":
                    errors.append(str(event.get("error") or "Codex turn failed"))
            if len(done) < 2:
                KimiBackend.terminate(proc)
                return BackendResult(False, "GPT 执行超时，操作已中止。", session_id,
                                     error={"code": "TIMEOUT", "message": "Codex process deadline exceeded"},
                                     provider=self.name, duration=time.monotonic() - start).as_dict()
            proc.wait(timeout=2)
            if proc.returncode != 0 or not reply:
                detail = "".join(errors)[-500:]
                return BackendResult(False, "GPT 请求失败，请稍后重试。", session_id, usage,
                                     {"code": "CODEX_REQUEST_FAILED", "message": detail}, self.name,
                                     time.monotonic() - start).as_dict()
            if self.progress:
                self.progress(BackendProgress(self.name, "completed", "GPT 已完成回复"))
            return BackendResult(True, reply, session_id, usage, provider=self.name,
                                 duration=time.monotonic() - start).as_dict()
        except Exception as ex:
            return BackendResult(False, "GPT 后端启动失败。", session_id,
                                 error={"code": "BACKEND_START_FAILED", "message": str(ex)},
                                 provider=self.name, duration=time.monotonic() - start).as_dict()
        finally:
            if proc is not None:
                try:
                    if proc.poll() is None:
                        self.terminate(proc)
                except Exception:
                    logger.exception("Failed to clean up Codex process")
            active_task.process = None


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

class DshBackend:
    """Official dsh SDK JSON-RPC adapter, with a reusable chat-only runtime.

    Uses the installed SDK profile protocol directly, so packaged Python needs
    no second dsh installation. Work runtimes close at handoff to release their
    native observation socket; durable provider sessions still resume.
    """

    name = "dsh"
    terminate = staticmethod(KimiBackend.terminate)

    def __init__(self, run_dir: Path, model: str = "deepseek-flash", command: str = "dsh",
                 progress: ProgressCallback | None = None, timeout_seconds: float = 180,
                 effort: str | None = "low"):
        self.run_dir = Path(run_dir)
        self.model = model or "deepseek-flash"
        self.command = command
        self.progress = progress
        self.timeout_seconds = timeout_seconds
        self.effort = effort if effort not in {None, "default"} else "low"
        if self.effort not in {"off", "low", "high", "max"}:
            raise ValueError("Unsupported dsh reasoning effort")
        self._process: Any = None
        self._events: queue.Queue = queue.Queue()
        self._surface: str | None = None
        self._rpc_id = 0
        self._diagnostic_state = {"code": "DSH_RUNTIME_FAILED"}

    def _command(self) -> list[str]:
        import shutil
        executable = shutil.which(self.command)
        if executable is None:
            raise FileNotFoundError("Install DeepSeek Harness (dsh) first")
        if Path(executable).suffix.lower() in {".cmd", ".ps1"}:
            # npm shims are not native executables. Use their declared JS entry,
            # never pass prompt text or credentials through a command shell.
            package = Path(executable).parent / "node_modules" / "@deepseek-ai" / "dsh"
            manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
            node = shutil.which("node")
            if not node:
                raise FileNotFoundError("dsh npm installation requires Node.js")
            return [node, str(package / manifest["bin"]["dsh"])]
        return [executable]

    def close(self) -> None:
        proc, self._process = self._process, None
        self._surface = None
        if proc is not None:
            self.terminate(proc)

    def _send(self, method: str, params: dict | None = None) -> int:
        self._rpc_id += 1
        frame = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method}
        if params is not None:
            frame["params"] = params
        self._process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self._process.stdin.flush()
        return self._rpc_id

    def _prepare(self, surface: str) -> tuple[Path, dict[str, str]]:
        import sys
        context = {key: os.environ[key] for key in (
            "STARDEW_MCP_SURFACE", "STARDEW_LIFE_MODE", "STARDEW_LIFE_TURN_ID",
            "STARDEW_LIFE_SAVE_ID", "STARDEW_DECISION_TOKEN") if key in os.environ}
        context_path = self.run_dir / "data" / ("dsh-turn-" + surface + ".json")
        context_path.parent.mkdir(parents=True, exist_ok=True)
        temp = context_path.with_suffix(".tmp")
        temp.write_text(json.dumps(context), encoding="utf-8")
        temp.replace(context_path)
        python = self.run_dir / "runtime" / "python" / "python.exe"
        python = python if python.is_file() else Path(sys.executable)
        env = dict(os.environ)
        if not env.get("DEEPSEEK_API_KEY"):
            # Read only the official route's reference, never copy the credentials
            # file, mutate dsh settings, or log parser messages containing secrets.
            import yaml
            credential_home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
            credential_file = credential_home / ".credentials.yaml"
            try:
                credentials = yaml.safe_load(credential_file.read_text(encoding="utf-8")) or {}
                refs = credentials.get("refs", {}) if credentials.get("version") == 1 else credentials
                key = refs.get("DEEPSEEK_API_KEY")
                if isinstance(key, str) and key.strip():
                    env["DEEPSEEK_API_KEY"] = key
            except FileNotFoundError:
                pass
            except Exception:
                raise RuntimeError("DSH_CREDENTIAL_STORE_UNREADABLE") from None
        if not env.get("DEEPSEEK_API_KEY"):
            raise RuntimeError("DSH_MISSING_CREDENTIAL")
        # Separate homes prevent inherited coding tools/configuration from
        # entering this game-only profile. Credentials remain environment-owned.
        env["DSH_HOME"] = str(self.run_dir / "data" / "dsh-home")
        env["DSH_SYSTEM_PROMPT"] = "你是星露谷农场伙伴。只通过提供的游戏工具交流与工作，通常回复一到两句。"
        # Keep this shim beside the profile's official node_modules so Node can
        # resolve its dependencies. Never patch the user's global installation.
        resume_plugin = Path(env["DSH_HOME"]) / "profiles" / "stardew-sdk-resume.mjs"
        resume_plugin.parent.mkdir(parents=True, exist_ok=True)
        resume_plugin.write_bytes(Path(__file__).with_name("dsh_resume_plugin.mjs").read_bytes())
        patch = [
            {"id": "sdk-jsonrpc-server", "disabled": True},
            {"insert": [{"id": "stardew-sdk-resume", "name": resume_plugin.as_uri()}]},
            {"id": "persistent-bash", "disabled": True},
            {"id": "persistent-pwsh", "disabled": True},
            {"id": "session-log-deepseek", "disabled": True},
            {"insert": [{"id": "stardew-mcp", "name": "@deepseek-ai/dsh-mcp-client", "config": {
                "transport": "stdio", "serverName": "stardew-companion", "command": str(python),
                "args": ["-B", "-m", "stardew_ai_runtime.mcp_server", "--run-dir", str(self.run_dir), "--surface", surface],
                "env": {"STARDEW_TURN_CONTEXT": str(context_path), "STARDEW_MCP_SURFACE": surface,
                        "PYTHONPATH": os.environ.get("PYTHONPATH", str(Path(__file__).resolve().parents[1]))},
                "cwd": str(self.run_dir), "toolCallTimeoutMs": 120000, "failOnStartupError": True,
            }}]},
        ]
        patch_path = self.run_dir / "data" / ("dsh-" + surface + ".patch.json")
        patch_path.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
        return patch_path, env

    def _next(self, active_task: ActiveTaskLike, deadline: float) -> dict[str, Any]:
        while time.monotonic() < deadline:
            if active_task.cancelled:
                raise InterruptedError("cancelled")
            try:
                event = self._events.get(timeout=0.1)
            except queue.Empty:
                if self._process is None or self._process.poll() is not None:
                    raise RuntimeError("dsh runtime exited") from None
                continue
            if event is None:
                raise RuntimeError("dsh runtime closed its protocol stream")
            return event
        raise TimeoutError("dsh request timed out")

    def _start(self, active_task: ActiveTaskLike, surface: str, patch: Path, env: dict[str, str]) -> None:
        if self._process is not None and self._process.poll() is None and self._surface == surface:
            active_task.process = self._process
            return
        self.close()
        self._events = queue.Queue()
        # Each process owns its diagnostics. A late daemon reader only updates
        # its captured state, even if this backend has already restarted.
        diagnostic_state = self._diagnostic_state = {"code": "DSH_RUNTIME_FAILED"}
        self._process = subprocess.Popen(
            [*self._command(), "--profile", "sdk-minimal", "--patch", str(patch)],
            cwd=str(self.run_dir), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        proc = self._process
        active_task.process = proc
        events = self._events

        def read() -> None:
            try:
                for line in proc.stdout:
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict):
                            events.put(item)
                    except ValueError:
                        continue
            finally:
                events.put(None)

        def drain_errors() -> None:
            # Runtime diagnostics can contain account paths; no raw stderr in UI.
            for line in proc.stderr:
                # Keep only fixed categories. Never retain a credential-bearing
                # line, complete provider response or account path.
                if "MISSING_CREDENTIAL" in line or "no API key" in line:
                    diagnostic_state["code"] = "DSH_MISSING_CREDENTIAL"
                elif "401" in line or "authentication" in line.lower():
                    diagnostic_state["code"] = "DSH_AUTH_FAILED"
                elif "Cannot find package" in line or "plugin" in line.lower() and "not found" in line.lower():
                    diagnostic_state["code"] = "DSH_PROFILE_UNAVAILABLE"
                elif "DSH_RESUME_PROTOCOL_UNSUPPORTED" in line:
                    diagnostic_state["code"] = "DSH_RESUME_PROTOCOL_UNSUPPORTED"

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=drain_errors, daemon=True).start()
        request_id = self._send("initialize", {"cwd": str(self.run_dir), "provider": "deepseek-official",
            "model": self.model, "reasoningEffort": self.effort, "maxTokens": 8192})
        deadline = time.monotonic() + min(45, self.timeout_seconds)
        while True:
            frame = self._next(active_task, deadline)
            if frame.get("id") == request_id:
                if frame.get("error"):
                    raise RuntimeError("DSH_MODEL_UNAVAILABLE")
                info = (frame.get("result") or {}).get("serverInfo") or {}
                if info.get("name") != "deepseek-harness-sdk-runtime" or info.get("stardewSessionResume") != 1:
                    raise RuntimeError("DSH_PROTOCOL_MISMATCH")
                break
        self._surface = surface

    def run(self, active_task: ActiveTaskLike, session_id: str | None, prompt: str) -> dict[str, Any]:
        import uuid
        start = time.monotonic()
        session_id = session_id or "stardew-" + uuid.uuid4().hex
        surface = "life" if os.environ.get("STARDEW_MCP_SURFACE") == "life" else "light"
        usage: dict[str, Any] = {}
        seen_responses: set[str] = set()
        turn_nonce = uuid.uuid4().hex
        reply = ""
        try:
            patch, env = self._prepare(surface)
            self._start(active_task, surface, patch, env)
            if self.progress:
                self.progress(BackendProgress(self.name, "started", "正在想下一步"))
            request_id = self._send("session/prompt", {"sessionId": session_id,
                "contentBlocks": [{"type": "text", "text": prompt}]})
            deadline = time.monotonic() + self.timeout_seconds
            finish = None
            failure_code = "DSH_TURN_FAILED"
            running = False
            receipt = False
            idle_seen = False
            turn_started = False
            active_turn = None
            while True:
                if receipt and idle_seen and finish is not None:
                    break
                frame = self._next(active_task, deadline)
                if frame.get("id") == request_id:
                    if frame.get("error"):
                        error = str((frame.get("error") or {}).get("message", "")).lower()
                        code = ("DSH_SESSION_OWNED" if "owned" in error or "locked" in error
                                else "DSH_SESSION_EXISTS" if "already exists" in error
                                else "DSH_SESSION_CORRUPT" if "corrupt" in error or "format" in error
                                else "DSH_REQUEST_REJECTED")
                        raise RuntimeError(code)
                    receipt = True
                    continue
                params = frame.get("params") or {}
                if params.get("sessionId") != session_id:
                    continue  # Child/session output never replaces root output.
                if frame.get("method") == "session.status":
                    if params.get("status") == "running":
                        running = True
                    if params.get("status") == "idle" and (running or finish is not None):
                        idle_seen = True
                elif frame.get("method") == "session.event":
                    event = params.get("event") or {}
                    data = event.get("data") or {}
                    if event.get("type") == "turn/start":
                        turn_started = True
                        active_turn = data.get("turn")
                        idle_seen = False
                        finish = None
                    if not turn_started:
                        continue
                    if active_turn is not None and data.get("turn") not in (None, active_turn):
                        continue
                    if event.get("type") == "assistant/message":
                        message = data.get("message") or {}
                        content = message.get("content") or []
                        text = "".join(str(block.get("text", "")) for block in content if block.get("type") == "text")
                        if text:
                            reply = text
                            if self.progress:
                                self.progress(BackendProgress(self.name, "response", text[:180]))
                        from stardew_ai_runtime.usage_meter import (
                            append_response_receipt,
                            dsh_response_usage,
                        )
                        measured = dsh_response_usage(data.get("usage") or {})
                        identity = message.get("id") or event.get("seq")
                        response_id = f"{session_id}:{identity}" if identity is not None else f"{session_id}:{turn_nonce}:{len(seen_responses)}"
                        if measured and response_id not in seen_responses:
                            seen_responses.add(response_id)
                            append_response_receipt(self.run_dir, response_id=response_id,
                                request_id=getattr(active_task, "request_id", ""),
                                save_id=getattr(active_task, "save_id", ""), conversation_id=session_id,
                                model=self.model, usage=measured,
                                channel="autonomy" if getattr(active_task, "request_id", "").startswith("autonomy-") else "conversation")
                            for key in ("input_tokens", "uncached_input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "thinking_tokens"):
                                if key in measured:
                                    usage[key] = usage.get(key, 0) + measured[key]
                            usage.update(source="dsh_assistant_message", input_includes_cache=True,
                                         generations_count=len(seen_responses))
                            usage["latestRequestInputContext"] = measured.get("latestRequestInputContext")
                            if measured.get("partial"):
                                usage["partial"] = True
                    elif event.get("type") == "tool/result" and self.progress:
                        self.progress(BackendProgress(self.name, "tool", "正在处理这项安排"))
                    elif event.get("type") == "turn/end":
                        reason = data.get("reason") or {}
                        finish = reason.get("kind")
                        # Map only known categories; provider failure objects can
                        # include request details and must never be echoed.
                        failure = json.dumps(reason.get("failure") or {}).upper()
                        if "TRANSPORT" in failure:
                            failure_code = "DSH_TRANSPORT_FAILED"
                        elif "AUTHENTICATION" in failure or "UNAUTHORIZED" in failure:
                            failure_code = "DSH_AUTH_FAILED"
                        if not isinstance(finish, str):
                            raise RuntimeError("Invalid dsh turn/end protocol")
            success = finish == "completed"
            if not success and usage:
                usage["partial"] = True
            return BackendResult(success, reply, session_id, usage or None,
                error=None if success else {"code": failure_code, "message":
                    "DeepSeek 网络连接失败，请稍后重试。" if failure_code == "DSH_TRANSPORT_FAILED"
                    else "DeepSeek 登录凭据未通过。" if failure_code == "DSH_AUTH_FAILED"
                    else "DeepSeek 未完成本次回复，请重试。"},
                provider=self.name, duration=time.monotonic() - start).as_dict()
        except Exception as ex:
            self.close()
            if usage:
                usage["partial"] = True
            known = {"DSH_MISSING_CREDENTIAL", "DSH_CREDENTIAL_STORE_UNREADABLE", "DSH_MODEL_UNAVAILABLE", "DSH_PROTOCOL_MISMATCH",
                     "DSH_SESSION_OWNED", "DSH_SESSION_EXISTS", "DSH_SESSION_CORRUPT", "DSH_REQUEST_REJECTED"}
            code = ("CANCELLED" if isinstance(ex, InterruptedError) else "TIMEOUT" if isinstance(ex, TimeoutError)
                    else "DSH_CLI_MISSING" if isinstance(ex, FileNotFoundError) else str(ex) if str(ex) in known
                    else self._diagnostic_state["code"])
            messages = {
                "CANCELLED": "已停下。", "TIMEOUT": "DeepSeek 响应超时，请稍后再试。",
                "DSH_CLI_MISSING": "没找到 dsh，请先安装 DeepSeek Harness。",
                "DSH_MISSING_CREDENTIAL": "请先在 dsh 保存 DeepSeek 凭据，或设置 DEEPSEEK_API_KEY。",
                "DSH_CREDENTIAL_STORE_UNREADABLE": "无法读取本机 dsh 凭据，请在 dsh 中检查登录设置。",
                "DSH_AUTH_FAILED": "DeepSeek 登录凭据未通过，请在 dsh 中更新。",
                "DSH_MODEL_UNAVAILABLE": "dsh 不支持当前模型或推理设置，请检查 deepseek-flash 与 low 是否可用。",
                "DSH_PROTOCOL_MISMATCH": "dsh SDK 协议不兼容，请检查安装版本。",
                "DSH_PROFILE_UNAVAILABLE": "dsh 缺少 SDK 或 MCP 组件，请修复 DeepSeek Harness 安装。",
                "DSH_RESUME_PROTOCOL_UNSUPPORTED": "当前 dsh SDK 版本尚不支持安全恢复会话，请使用支持的版本。",
                "DSH_SESSION_OWNED": "这段对话仍被另一个 dsh 进程占用，请结束重复服务后继续。",
                "DSH_SESSION_EXISTS": "dsh 未能恢复已有对话，记录已保留，请检查版本兼容性。",
                "DSH_SESSION_CORRUPT": "dsh 对话记录无法读取，原记录已保留。",
                "DSH_REQUEST_REJECTED": "dsh 拒绝了本次对话请求，请检查服务日志。",
            }
            return BackendResult(False, messages.get(code, "dsh 运行中断，请检查 DeepSeek Harness 安装后重试。"),
                session_id, usage or None, error={"code": code, "message": messages.get(code, "dsh runtime failed")},
                provider=self.name, duration=time.monotonic() - start).as_dict()
        finally:
            if surface != "life":
                self.close()
            active_task.process = None


class McodeBackend:
    """Use the local MiniMax Code CLI (``mcode exec``) bound to this game run.

    The game MCP server is attached for this invocation only: each turn writes
    ``.mcp.json`` into a workspace directory owned by this game run and points
    ``mcode exec --cwd`` at it.  A workspace entry shadows a same-named server
    from the player's shared profile, and its ``env`` carries this turn's
    authority, so the player's own MiniMax Code configuration is never read or
    rewritten.  The data directory stays untouched, which keeps the existing
    login and model selection in charge.
    """

    name = "mcode"
    terminate = staticmethod(KimiBackend.terminate)
    server_name = "stardew-companion"
    #: ``--effort`` values mcode accepts; "default" omits the flag entirely.
    efforts = frozenset({"default", "low", "medium", "high", "xhigh", "max"})
    #: Stable ``mcode exec`` exit codes, mapped to this project's error codes.
    exit_codes = {
        2: ("MCODE_REQUEST_INVALID", "mcode 拒绝了本次调用参数，请检查模型标识与推理档位是否被该模型支持。"),
        3: ("MCODE_CONFIG_INVALID", "mcode 配置无效，请检查本机 MiniMax Code 配置。"),
        4: ("MCODE_RUNTIME_FAILED", "mcode 本轮运行失败，请稍后重试。"),
        6: ("TIMEOUT", "mcode 响应超时，操作已中止。"),
        7: ("MCODE_LIMIT_EXCEEDED", "mcode 超出本轮运行限制，请稍后重试。"),
        70: ("MCODE_INTERNAL_ERROR", "mcode 内部错误，请稍后重试。"),
    }

    def __init__(self, run_dir: Path, model: str = "", command: str = "mcode",
                 progress: ProgressCallback | None = None, timeout_seconds: float = 600.0,
                 effort: str | None = "low"):
        self.run_dir = Path(run_dir)
        self.model = model
        self.command = command
        self.progress = progress
        self.timeout_seconds = timeout_seconds
        if effort not in (None, *self.efforts):
            raise ValueError("Unsupported mcode reasoning effort")
        self.effort = effort

    def _executable(self) -> str:
        """Resolve the CLI, preferring the shell-free shim over PowerShell."""
        import shutil
        executable = shutil.which(self.command)
        if executable is None:
            raise FileNotFoundError("Install MiniMax Code CLI (mcode) first")
        if Path(executable).suffix.lower() == ".ps1":
            # An execution policy can block the npm .ps1 shim; its .cmd sibling
            # runs the same entry point without a command shell.
            sibling = Path(executable).with_suffix(".cmd")
            if sibling.is_file():
                return str(sibling)
        return executable

    def _write_workspace(self, surface: str) -> Path:
        """Write this turn's game-only MCP file and return its directory."""
        import sys
        python = self.run_dir / "runtime" / "python" / "python.exe"
        if not python.is_file():
            python = Path(sys.executable)
        context = {key: os.environ[key] for key in (
            "STARDEW_LIFE_MODE", "STARDEW_LIFE_TURN_ID", "STARDEW_LIFE_SAVE_ID",
            "STARDEW_LIFE_PROPOSAL_ID", "STARDEW_DECISION_TOKEN") if key in os.environ}
        context["STARDEW_MCP_SURFACE"] = surface
        context.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        workspace = self.run_dir / "data" / "mcode"
        workspace.mkdir(parents=True, exist_ok=True)
        document = {"mcpServers": {self.server_name: {
            "type": "stdio", "command": str(python),
            "args": ["-B", "-m", "stardew_ai_runtime.mcp_server", "--run-dir", str(self.run_dir),
                     "--surface", surface],
            "env": context, "cwd": str(self.run_dir),
        }}}
        path = workspace / ".mcp.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        return workspace

    def _command(self, executable: str, workspace: Path, session_id: str | None) -> list[str]:
        prefix = [executable]
        if Path(executable).suffix.lower() in {".cmd", ".ps1"}:
            import shutil
            # npm shims invoke cmd.exe; run their JS entry directly so neither
            # game paths nor model arguments are interpreted by a shell.
            entry = Path(executable).parent / "node_modules" / "@minimax-ai" / "code" / "cli.js"
            sibling = Path(executable).parent / "node.exe"
            node = str(sibling) if sibling.is_file() else shutil.which("node")
            if not node or not entry.is_file():
                raise FileNotFoundError("Repair the MiniMax Code npm installation and Node.js")
            prefix = [node, str(entry)]
        cmd = [*prefix, "exec", "--cwd", str(workspace),
               "--output-format", "stream-json", "--permission", "full",
               "--timeout", f"{max(1, int(self.timeout_seconds) + 30)}s", "--input", "-"]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort and self.effort != "default":
            cmd.extend(["--effort", self.effort])
        if session_id:
            cmd.extend(["--session", session_id])
        return cmd

    @staticmethod
    def _usage(raw: Any) -> dict[str, Any] | None:
        """Normalize one exec result's usage into the bridge's shared shape."""
        if not isinstance(raw, dict):
            return None

        def number(key: str) -> int | None:
            value = raw.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        input_tokens = number("inputTokens")
        output_tokens = number("outputTokens")
        cache_read = number("cacheReadTokens")
        total = number("totalTokens")
        if all(value is None for value in (input_tokens, output_tokens, cache_read, total)):
            return None
        # mcode reports input and cache-read separately; the shared shape counts
        # cache inside the input total, as the usage display expects.
        # An exec can contain multiple model requests. These totals are useful
        # for accounting but cannot measure the latest request's context size.
        # The CLI exposes exec totals, not a request count. A multi-request exec
        # must not be displayed as one model call merely because it has one result.
        usage: dict[str, Any] = {"input_includes_cache": True,
                                 "input_context_measured": False, "source": "mcode_exec_result"}
        if input_tokens is not None and cache_read is not None:
            usage["input_tokens"] = input_tokens + cache_read
        if output_tokens is not None:
            usage["output_tokens"] = output_tokens
        if cache_read is not None:
            usage["cache_read_tokens"] = cache_read
        if total is not None and cache_read is not None:
            usage["total_tokens"] = total + cache_read
        elif "input_tokens" in usage and output_tokens is not None:
            usage["total_tokens"] = usage["input_tokens"] + output_tokens
        if any(field not in usage for field in ("total_tokens", "input_tokens", "output_tokens", "cache_read_tokens")):
            usage["partial"] = True
        return usage

    @staticmethod
    def _tool_label(name: str | None) -> str | None:
        """Drop the mcp__<server>__ prefix so replies show the real tool."""
        if not name:
            return None
        stripped = re.sub(r"^mcp__[A-Za-z0-9.-]+__", "", name)
        return stripped or name

    def _invoke(self, active_task: ActiveTaskLike, session_id: str | None, prompt: str,
                executable: str, workspace: Path, start: float) -> dict[str, Any]:
        cmd = self._command(executable, workspace, session_id)
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        proc: Any = None
        requested_session = session_id
        started = False
        # A file-backed stdin avoids both Windows command-line limits and a
        # blocking pipe write before the cancellation/deadline loop can start.
        prompt_stream = tempfile.TemporaryFile()
        try:
            prompt_stream.write(prompt.encode("utf-8"))
            prompt_stream.seek(0)
            proc = subprocess.Popen(cmd, cwd=str(workspace), stdin=prompt_stream, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            active_task.process = proc
            if self.progress:
                self.progress(BackendProgress(self.name, "started", "已启动 MiniMax Code"))

            def read(stream: Any, channel: str) -> None:
                try:
                    for line in stream:
                        events.put((channel, line))
                finally:
                    events.put((channel, None))

            readers = [threading.Thread(target=read, args=(stream, channel), daemon=True)
                       for stream, channel in ((proc.stdout, "stdout"), (proc.stderr, "stderr"))]
            for reader in readers:
                reader.start()
            done: set[str] = set()
            reply = ""
            usage = None
            status = ""
            errors: list[str] = []
            deadline = start + self.timeout_seconds
            while len(done) < 2 and time.monotonic() < deadline:
                if active_task.cancelled:
                    self.terminate(proc)
                    if usage:
                        usage["partial"] = True
                    return BackendResult(False, "任务已取消。", session_id, usage, provider=self.name,
                                         duration=time.monotonic() - start).as_dict()
                try:
                    channel, line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    done.add(channel)
                    continue
                if channel == "stderr":
                    errors.append(line[:500])
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind in {"session.started", "session.resumed", "turn.started", "item.started", "item.completed", "turn.completed"}:
                    started = True
                if kind in {"session.started", "session.resumed"}:
                    session_id = event.get("sessionId") or session_id
                elif kind == "turn.completed":
                    usage = self._usage(event.get("usage")) or usage
                elif kind == "turn.failed":
                    errors.append(str(event.get("error") or "mcode turn failed"))
                elif kind == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message" and item.get("content"):
                        reply = str(item["content"])
                    elif item.get("type") == "tool_call" and self.progress:
                        call = item.get("toolCall") or {}
                        self.progress(BackendProgress(self.name, "tool_started", "正在处理游戏任务",
                                                      self._tool_label(call.get("name"))))
                elif kind == "exec.completed":
                    result = event.get("result") or {}
                    session_id = result.get("sessionId") or session_id
                    status = str(result.get("status") or "")
                    if result.get("output"):
                        reply = str(result["output"])
                    usage = self._usage(result.get("usage")) or usage
                    if usage and result.get("usageIncomplete"):
                        usage["partial"] = True
            if len(done) < 2:
                self.terminate(proc)
                if usage:
                    usage["partial"] = True
                return BackendResult(False, "MiniMax Code 执行超时，操作已中止。", session_id, usage,
                                     error={"code": "TIMEOUT", "message": "mcode process deadline exceeded"},
                                     provider=self.name, duration=time.monotonic() - start).as_dict()
            proc.wait(timeout=2)
            if proc.returncode == 0 and status == "succeeded" and reply:
                if self.progress:
                    self.progress(BackendProgress(self.name, "completed", "MiniMax Code 已完成回复"))
                return BackendResult(True, reply, session_id, usage, provider=self.name,
                                     duration=time.monotonic() - start).as_dict()
            detail = "".join(errors)[-500:]
            if (requested_session and not started and usage is None
                    and re.search(r"session (?:not found|is archived|workspace does not match)", detail, re.IGNORECASE)):
                # A rotated or archived session must not strand the game loop.
                # This is checked before the generic exit-code mapping because
                # the CLI reports it as an ordinary runtime failure.
                return BackendResult(False, "mcode 无法恢复这段对话，已改用新会话。", None, usage,
                                     {"code": "MCODE_SESSION_UNAVAILABLE", "message": detail},
                                     self.name, time.monotonic() - start).as_dict()
            if proc.returncode in self.exit_codes:
                code, message = self.exit_codes[proc.returncode]
                return BackendResult(False, message, session_id, usage, {"code": code, "message": detail},
                                     self.name, time.monotonic() - start).as_dict()
            return BackendResult(False, "MiniMax Code 请求失败，请稍后重试。", session_id, usage,
                                 {"code": "MCODE_REQUEST_FAILED", "message": detail}, self.name,
                                 time.monotonic() - start).as_dict()
        finally:
            prompt_stream.close()
            if proc is not None:
                try:
                    if proc.poll() is None:
                        self.terminate(proc)
                except Exception:
                    logger.exception("Failed to clean up mcode process")
            active_task.process = None

    def run(self, active_task: ActiveTaskLike, session_id: str | None, prompt: str) -> dict[str, Any]:
        start = time.monotonic()
        try:
            executable = self._executable()
        except FileNotFoundError as ex:
            return BackendResult(False, "没找到 mcode，请先安装 MiniMax Code CLI。", session_id,
                                 error={"code": "MCODE_CLI_MISSING", "message": str(ex)},
                                 provider=self.name, duration=time.monotonic() - start).as_dict()
        surface = "life" if os.environ.get("STARDEW_MCP_SURFACE") == "life" else "light"
        try:
            workspace = self._write_workspace(surface)
        except OSError as ex:
            logger.warning("Could not prepare the mcode game tool profile")
            return BackendResult(False, "MCode 游戏工具配置失败，请检查 Mod 运行目录。", session_id,
                                 error={"code": "MCODE_GAME_PROFILE_FAILED", "message": str(ex)},
                                 provider=self.name, duration=time.monotonic() - start).as_dict()
        try:
            from .mcp_server import BASE_TOOLS, LIFE_TOOLS
            registered = LIFE_TOOLS if surface == "life" else BASE_TOOLS
            prompt = ("【当前工具绑定】规则里的短工具名只是简称；调用必须使用实际注册全名，并按工具 schema 填参。\n"
                      + "\n".join(f"{name} = mcp__{self.server_name}__{name}" for name in sorted(registered))
                      + "\n工具返回未找到时先核对注册名；未成功保存安排不能声称已派工或已经出发。\n\n" + prompt)
            result = self._invoke(active_task, session_id, prompt, executable, workspace, start)
            if result.get("error_detail", {}).get("code") == "MCODE_SESSION_UNAVAILABLE" and not active_task.cancelled:
                logger.info("mcode could not resume session %s; starting a fresh one", session_id)
                # A resumed life prompt can omit the core policy. A fresh
                # provider session must receive it again before using tools.
                result = self._invoke(active_task, None, runtime_instructions() + "\n\n" + prompt,
                                      executable, workspace, start)
            return result
        except OSError as ex:
            return BackendResult(False, "MiniMax Code 启动失败，请检查 CLI 和 Node.js 安装。", session_id,
                                 error={"code": "MCODE_START_FAILED", "message": str(ex)},
                                 provider=self.name, duration=time.monotonic() - start).as_dict()
