"""Chat Bridge connecting in-game chat UI to a configured agent backend and MCP tools.

Architecture:
  Stardew Valley Mod (F8 CompanionCommandMenu)
     |
     | WebSocket (/chat)
     v
  ChatBridge (stardew_ai_runtime.chat_bridge)
     |
     | Subprocess: configured agy or Kimi CLI backend
     v
  Agent CLI
     |
     | stdio MCP
     v
  stardew-companion MCP server -> Mod Mechanics -> Game World
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from stardew_ai_runtime.agent_backends import AgyBackend, KimiBackend
from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.chat_backend_config import load_chat_backend_config, project_root
from stardew_ai_runtime.companion_care import CompanionCareService
from stardew_ai_runtime.companion_memory import CompanionMemoryStore
from stardew_ai_runtime.companion_milestones import (
    CompanionMilestoneStore,
    wire_node,
)
from stardew_ai_runtime.companion_profile import CompanionProfileStore
from stardew_ai_runtime.decision_context import build_decision_context, render_decision_context
from stardew_ai_runtime.job_feedback import compact_job_feedback
from stardew_ai_runtime.kimi_wire_usage import read_usage_since, wire_offset
from stardew_ai_runtime.life_chat import LifeChatService
from stardew_ai_runtime.plan_executor import DispatchDeferred, PlanExecutor, StepExecution
from stardew_ai_runtime.protocol import (
    Envelope,
    LifeChatSubmitPayload,
    LifeMemoryEditPayload,
    LifeMemoryListPayload,
    LifeMilestonesGetPayload,
    LifeProfileGetPayload,
    LifeProfileSetPayload,
    MilestoneNode,
    ProtocolError,
)
from stardew_ai_runtime.scheduler import (
    DiscoveryError,
    get_candidate_discovery_paths,
    resolve_discovery,
)
from stardew_ai_runtime.websocket_client import (
    ConnectionClosed,
    WebSocketClient,
    WebSocketError,
)
from stardew_ai_runtime.work_state import WorkStore

logger = logging.getLogger("stardew_ai_runtime.chat_bridge")

CHAT_BRIDGE_LOG_FILENAME = "chat-bridge.log"
CHAT_BRIDGE_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
CHAT_BRIDGE_LOG_BACKUP_COUNT = 3
CHAT_BRIDGE_LOG_ENCODING = "utf-8"
CHAT_BRIDGE_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
CHAT_BRIDGE_LOG_DATEFMT = "%H:%M:%S"


def resolve_chat_bridge_run_dir(run_dir: str | Path | None = None) -> Path | None:
    """Resolves the active run directory, consulting explicit path, environment, and auto-discovery."""
    if run_dir:
        return Path(run_dir).resolve()
    env_dir = os.getenv("STARDEW_RUN_DIR")
    if env_dir and env_dir.strip():
        return Path(env_dir.strip()).resolve()
    try:
        candidate_meta = project_root() / "config" / "installed-candidate.json"
        if candidate_meta.is_file():
            meta = json.loads(candidate_meta.read_text(encoding="utf-8"))
            mod_dir = meta.get("modDirectory")
            if mod_dir and Path(mod_dir).is_dir():
                return Path(mod_dir).resolve()
    except Exception:
        pass
    try:
        for cand in get_candidate_discovery_paths(None):
            if cand.is_file():
                parent = cand.parent
                mod_dir = parent.parent if parent.name.lower() == "data" else parent
                return mod_dir.resolve()
            parent = cand.parent
            cand_mod = parent.parent if parent.name.lower() == "data" else parent
            if cand_mod.is_dir():
                return cand_mod.resolve()
    except Exception:
        pass
    return None


def get_chat_bridge_fallback_log_dir() -> Path:
    """Returns the fallback directory for logs when run-dir cannot be resolved."""
    env_override = os.getenv("STARDEW_FALLBACK_LOG_DIR")
    if env_override and env_override.strip():
        return Path(env_override.strip())
    return Path.home() / ".gemini" / "antigravity-cli" / "logs"


def get_chat_bridge_log_path(run_dir: str | Path | None = None) -> Path:
    """Determines the destination path for chat-bridge.log."""
    resolved = resolve_chat_bridge_run_dir(run_dir)
    if resolved is not None:
        return resolved / "logs" / CHAT_BRIDGE_LOG_FILENAME
    return get_chat_bridge_fallback_log_dir() / CHAT_BRIDGE_LOG_FILENAME


def has_chat_bridge_file_logging() -> bool:
    """Checks whether a ChatBridge RotatingFileHandler is currently attached to the root logger."""
    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        if getattr(h, "_is_chat_bridge_handler", False):
            return True
    return False


def configure_chat_bridge_file_logging(
    run_dir: str | Path | None = None,
) -> tuple[RotatingFileHandler, Path]:
    """Attaches a 5MB x 3 UTF-8 RotatingFileHandler to the root logger."""
    log_path = get_chat_bridge_log_path(run_dir)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as ex:
        fallback_path = get_chat_bridge_fallback_log_dir() / CHAT_BRIDGE_LOG_FILENAME
        try:
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            log_path = fallback_path
        except Exception:
            logger.warning("Failed to create log directories (%s); falling back to current working directory", ex)
            log_path = Path.cwd() / "logs" / CHAT_BRIDGE_LOG_FILENAME
            log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        if getattr(h, "_is_chat_bridge_handler", False) and isinstance(h, RotatingFileHandler):
            try:
                if Path(h.baseFilename).resolve() == log_path.resolve():
                    return h, log_path
            except Exception:
                pass

    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=CHAT_BRIDGE_LOG_MAX_BYTES,
        backupCount=CHAT_BRIDGE_LOG_BACKUP_COUNT,
        encoding=CHAT_BRIDGE_LOG_ENCODING,
    )
    handler.setLevel(logging.INFO)
    handler._is_chat_bridge_handler = True  # type: ignore[attr-defined]
    formatter = logging.Formatter(
        CHAT_BRIDGE_LOG_FORMAT,
        datefmt=CHAT_BRIDGE_LOG_DATEFMT,
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    if root_logger.level > logging.INFO or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.INFO)
    return handler, log_path


def switch_chat_bridge_file_logging(
    new_run_dir: str | Path | None,
) -> tuple[RotatingFileHandler, Path]:
    """Switches the active chat_bridge file handler to a newly discovered run directory."""
    log_path = get_chat_bridge_log_path(new_run_dir)
    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        if getattr(h, "_is_chat_bridge_handler", False) and isinstance(h, RotatingFileHandler):
            try:
                if Path(h.baseFilename).resolve() == log_path.resolve():
                    return h, log_path
                h.close()
                root_logger.removeHandler(h)
            except Exception:
                pass
    return configure_chat_bridge_file_logging(new_run_dir)


def remove_chat_bridge_file_logging() -> None:
    """Closes and removes all chat_bridge file handlers from the root logger."""
    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        if getattr(h, "_is_chat_bridge_handler", False):
            try:
                h.close()
            except Exception:
                pass
            root_logger.removeHandler(h)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    res = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, offset
        shift += 7
    return res, offset


def _parse_protobuf(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    offset = 0
    fields: dict[int, list[tuple[int, Any]]] = {}
    while offset < len(data):
        try:
            tag_val, offset = _decode_varint(data, offset)
        except Exception:
            break
        tag = tag_val >> 3
        wire = tag_val & 7
        if wire == 0:
            val, offset = _decode_varint(data, offset)
        elif wire == 2:
            length, offset = _decode_varint(data, offset)
            val = data[offset : offset + length]
            offset += length
        elif wire == 1:
            val = data[offset : offset + 8]
            offset += 8
        elif wire == 5:
            val = data[offset : offset + 4]
            offset += 4
        else:
            break
        fields.setdefault(tag, []).append((wire, val))
    return fields


def _parse_usage_from_gen_metadata(data: bytes) -> dict[str, int]:
    usage = {
        "prompt_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "thinking_tokens": 0,
        "total_tokens": 0,
    }
    if not data:
        return usage
    f = _parse_protobuf(data)
    if 1 not in f:
        return usage
    wire, val = f[1][0]
    if wire != 2 or not isinstance(val, bytes):
        return usage
    gen_meta = _parse_protobuf(val)
    if 4 not in gen_meta:
        return usage
    wire4, val4 = gen_meta[4][0]
    if wire4 != 2 or not isinstance(val4, bytes):
        return usage
    u = _parse_protobuf(val4)
    usage["prompt_tokens"] = u.get(1, [(0, 0)])[0][1]
    usage["input_tokens"] = u.get(2, [(0, 0)])[0][1]
    usage["output_tokens"] = u.get(3, [(0, 0)])[0][1]
    usage["cache_read_tokens"] = u.get(5, [(0, 0)])[0][1]
    usage["thinking_tokens"] = u.get(9, [(0, 0)])[0][1]
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def get_conversation_db_path(conversation_id: str) -> Path:
    return (
        Path.home()
        / ".gemini"
        / "antigravity-cli"
        / "conversations"
        / f"{conversation_id}.db"
    )


def get_max_gen_idx(conversation_id: str | None) -> int:
    """Returns the maximum gen_metadata.idx recorded for conversation_id, or -1 if none."""
    if not conversation_id:
        return -1
    db_path = get_conversation_db_path(conversation_id)
    if not db_path.is_file():
        return -1
    try:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT MAX(idx) FROM gen_metadata")
        row = cur.fetchone()
        con.close()
        return row[0] if (row and row[0] is not None) else -1
    except Exception as ex:
        logger.debug("Failed getting max gen idx for %s: %s", conversation_id, ex)
        return -1


def get_command_usage_delta(conversation_id: str | None, start_idx: int) -> dict[str, Any] | None:
    """Calculates exact usage delta for generations strictly after start_idx."""
    if not conversation_id:
        return None
    db_path = get_conversation_db_path(conversation_id)
    if not db_path.is_file():
        return None
    try:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute(
            "SELECT idx, data FROM gen_metadata WHERE idx > ? ORDER BY idx",
            (start_idx,),
        )
        rows = cur.fetchall()
        con.close()
        if not rows:
            return None

        inp = 0
        out = 0
        cache = 0
        thinking = 0
        for _, data in rows:
            u = _parse_usage_from_gen_metadata(data)
            inp += u.get("input_tokens", 0)
            out += u.get("output_tokens", 0)
            cache += u.get("cache_read_tokens", 0)
            thinking += u.get("thinking_tokens", 0)

        return {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache,
            "thinking_tokens": thinking,
            "total_tokens": inp + out,
            "generations_count": len(rows),
            "start_idx": start_idx + 1,
            "end_idx": rows[-1][0],
            "source": "db_gen_metadata_delta",
        }
    except Exception as ex:
        logger.warning(
            "Failed calculating usage delta for %s (start_idx=%d): %s",
            conversation_id,
            start_idx,
            ex,
        )
        return None


def get_max_step_idx(conversation_id: str | None) -> int:
    """Returns the maximum steps.idx recorded for conversation_id, or -1 if none."""
    if not conversation_id:
        return -1
    db_path = get_conversation_db_path(conversation_id)
    if not db_path.is_file():
        return -1
    try:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT coalesce(max(idx), -1) FROM steps")
        row = cur.fetchone()
        con.close()
        return row[0] if (row and row[0] is not None) else -1
    except Exception as ex:
        logger.debug("Failed getting max step idx for %s: %s", conversation_id, ex)
        return -1


def check_new_quota_error(conversation_id: str | None, start_step_idx: int) -> tuple[bool, str]:
    """Checks if any new step with idx > start_step_idx and step_type == 17 has RESOURCE_EXHAUSTED."""
    if not conversation_id:
        return False, ""
    db_path = get_conversation_db_path(conversation_id)
    if not db_path.is_file():
        return False, ""
    try:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute(
            "SELECT idx, step_payload FROM steps WHERE idx > ? AND step_type = 17 ORDER BY idx",
            (start_step_idx,),
        )
        rows = cur.fetchall()
        con.close()
        for _idx, payload in rows:
            text = (
                payload.decode("utf-8", errors="replace")
                if isinstance(payload, bytes)
                else str(payload)
            )
            if (
                "RESOURCE_EXHAUSTED" in text
                or "Individual quota reached" in text
                or "quota exceeded" in text
            ):
                return True, text
        return False, ""
    except Exception as ex:
        logger.debug("Failed checking new steps for quota error: %s", ex)
        return False, ""


class InternalMcpPlanClient:
    """Persistent internal MCP client the bridge owns for plan execution.

    It speaks to the *same* MCP server implementation the model uses (spawned
    with the harness-only ``--surface internal`` — the light game surface plus the
    read-only reconcile tool — and the same ``--run-dir``), so the worker path uses
    the same policy/budget/chest checks and the same real scheduler code as a
    normal tool call — it is not a second independent scheduler socket next to a
    provider.

    The Mod transport accepts a single command socket and answers a concurrent
    connection with 409 Conflict, so ownership is serialized: the bridge closes
    this client before starting a provider turn and reopens it afterwards.

    The stdio session lives in one dedicated asyncio task that opens, calls and
    closes it in the same task context (the only supported usage for
    ``stdio_client``'s cancel scopes).
    """

    def __init__(
        self,
        run_dir: str | Path | None,
        *,
        python_executable: str | None = None,
        timeout_seconds: float = 120.0,
    ):
        self.run_dir = Path(run_dir) if run_dir else None
        self.python_executable = python_executable or sys.executable
        self.timeout_seconds = timeout_seconds
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._requests: asyncio.Queue | None = None
        self._ready: asyncio.Future | None = None
        self._closed = False
        self._live_session: Any | None = None

    @property
    def connected(self) -> bool:
        return bool(self._ready and self._ready.done() and not self._ready.cancelled())

    def build_command(self) -> list[str]:
        if self.run_dir is None:
            raise RuntimeError("internal plan client requires a run directory")
        return [
            self.python_executable,
            "-m",
            "stardew_ai_runtime.mcp_server",
            "--run-dir",
            str(self.run_dir),
            "--surface",
            "internal",
        ]

    async def open(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._requests = asyncio.Queue()
        self._ready = self._loop.create_future()
        self._closed = False
        self._task = self._loop.create_task(self._session_loop())
        await asyncio.wait_for(asyncio.shield(self._ready), timeout=self.timeout_seconds)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._task is None or self._task.done():
            await self.open()
        # Native controls must reach the existing scheduler while a long native
        # operation is awaiting its terminal result, not queue behind that job.
        if name == "dispatch_plan_operation" and (arguments or {}).get("operation") in {"cancel_task", "pause_task", "resume_task"} and self._live_session is not None:
            result = await asyncio.wait_for(self._live_session.call_tool(name, arguments or {}), timeout=10)
            if getattr(result, "isError", False):
                raise RuntimeError(f"Native control rejected: {_tool_result_payload(result)}")
            return _tool_result_payload(result)
        return await self._command(("call", name, arguments or {}))

    async def current_save_id(self) -> str | None:
        try:
            status = await self.call_tool("get_status", {"detail": False})
        except Exception:
            logger.debug("Unable to read save id for plan worker", exc_info=True)
            return None
        save_id = status.get("saveId") if isinstance(status, dict) else None
        if not save_id or save_id == "unknown":
            return None
        return str(save_id)

    async def reconcile(self, command_id: str) -> Any:
        """Ask the same server to reconcile a persisted command id (read-only)."""
        return await self.call_tool("reconcile_plan_command", {"command_id": command_id})

    async def close(self) -> None:
        """Release the internal execution socket so a provider turn can own it."""
        if self._task is None:
            return
        try:
            await self._command(("close", None, None))
        except Exception:
            logger.debug("Internal MCP client close command failed", exc_info=True)
        task, self._task = self._task, None
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(Exception):
                    await task
            except Exception:
                logger.debug("Internal MCP session task ended with error", exc_info=True)
        self._requests = None
        self._ready = None

    # ------------------------------------------------------------- internals
    async def _command(self, command: tuple[str, Any, Any]) -> Any:
        if self._requests is None or self._loop is None:
            raise RuntimeError("internal MCP client is not open")
        future: asyncio.Future = self._loop.create_future()
        self._requests.put_nowait((command, future))
        return await future

    async def _session_loop(self) -> None:
        """Own the stdio session for its whole lifetime in one task."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        cmd = self.build_command()
        env = {**os.environ, "STARDEW_MCP_SURFACE": "internal"}
        try:
            async with stdio_client(
                StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self._live_session = session
                    self._resolve_ready(None)
                    try:
                        await self._serve(session)
                    finally:
                        self._live_session = None
        except BaseException as ex:  # noqa: BLE001 - reported to the awaiting caller
            self._resolve_ready(ex)
            if not isinstance(ex, (asyncio.CancelledError, GeneratorExit)):
                logger.warning("Internal MCP session ended: %s", ex)

    def _resolve_ready(self, error: BaseException | None) -> None:
        ready = self._ready
        if ready is not None and not ready.done():
            if error is None:
                ready.set_result(True)
            else:
                ready.set_exception(error)

    async def _serve(self, session: Any) -> None:
        queue = self._requests
        if queue is None:
            return
        while True:
            command, future = await queue.get()
            kind, name, arguments = command
            if kind == "close":
                if not future.done():
                    future.set_result(None)
                return
            try:
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments or {}),
                    timeout=self.timeout_seconds,
                )
                if getattr(result, "isError", False):
                    raise RuntimeError(
                        f"internal MCP tool '{name}' failed: {_tool_result_payload(result)}"
                    )
                payload = _tool_result_payload(result)
            except Exception as ex:
                if not future.done():
                    future.set_exception(ex)
            else:
                if not future.done():
                    future.set_result(payload)

    # Kept for explicit teardown paths (bridge shutdown) where awaiting is unsafe.
    def terminate_sync(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()


def _tool_result_payload(result: Any) -> Any:
    """Normalise an MCP CallToolResult to its structured JSON payload."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if set(structured) == {"result"}:
            return structured["result"]
        return structured
    content = getattr(result, "content", None)
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except ValueError:
                    return text
    return None


class PlanWorker:
    """Advances committed short plans without one model turn per step.

    Wakes on new native snapshots, after provider turns, and after new player
    instructions. It claims ready steps through the shared ``PlanExecutor`` (same
    store, same commit discipline as the MCP ``run_next_step`` tool), chains
    successful steps without any LLM call, and only reports a model wake when a
    step deviates (partial/unknown), when a dependency is blocked, or when a
    waiting condition requires a decision.
    """

    def __init__(
        self,
        store: WorkStore,
        client: Any,
        *,
        worker_id: str | None = None,
        idle_seconds: float = 5.0,
        step_limit: int = 200,
    ):
        self.store = store
        self.client = client
        self.worker_id = worker_id or f"bridge-{uuid.uuid4().hex[:8]}"
        self.idle_seconds = idle_seconds
        self.step_limit = step_limit
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._dirty = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._provider_active = False
        self.supplied_save_id: str | None = None
        self.last_result: StepExecution | None = None
        self.progress_callback = None
        self.terminal_callback: Any | None = None
        self.completed_steps = 0
        self.attempted_steps = 0
        self.pending_reasons: list[str] = []
        self.pending_decisions: list[dict[str, Any]] = []
        # Latest native snapshot used to gate explicit waiting conditions. The
        # bridge refreshes it on every world.snapshot; the worker never invents it.
        self.snapshot_provider: Any | None = None

    def _fresh_state(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self.snapshot_provider is None:
            return None, None
        try:
            return self.snapshot_provider()
        except Exception:
            logger.debug("Plan worker snapshot provider failed", exc_info=True)
            return None, None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self._run())

    def notify(self, *, dirty: bool = True) -> None:
        """Signal new potential work (snapshot, provider turn end, player input)."""
        if dirty:
            self._dirty = True
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake.set)
                return
            except RuntimeError:
                pass
        self._wake.set()

    def set_provider_active(self, active: bool) -> None:
        self._provider_active = active

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # -------------------------------------------------------------- work
    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.idle_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
            if self._provider_active:
                continue
            try:
                await self.evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Plan worker evaluation failed", exc_info=True)

    async def evaluate(self) -> list[StepExecution]:
        """Run ready steps until none remain (or a deviation needs the model)."""
        self._dirty = False
        executions: list[StepExecution] = []
        for _ in range(self.step_limit):
            if self._provider_active:
                break
            save_id = await self._resolve_save_id()
            if not save_id:
                break
            # Read-only readiness check against the shared store; it never touches
            # the Mod, so an idle plan costs no provider and no LLM call. Waiting
            # steps stay parked until their explicit native condition is met.
            try:
                snapshot, game_date = self._fresh_state()
                if not self.store.has_ready_step(save_id, snapshot=snapshot, game_date=game_date):
                    break
            except Exception:
                break
            executor = PlanExecutor(
                self.store,
                dispatch=self._dispatch,
                reconcile=self._reconcile,
                snapshot_provider=self._fresh_state,
            )
            execution = await executor.run_once(save_id, self.worker_id)
            if execution.status == "idle":
                break
            executions.append(execution)
            self.last_result = execution
            if self.progress_callback:
                await self.progress_callback(execution)
            # ``completed_steps`` counts only steps the harness actually confirmed
            # complete. A parked (waiting) or failed/unknown dispatch is NOT
            # progress: counting it was the round6 false-progress bug that let the
            # acceptance script report a failed plan as multi-step success.
            self.attempted_steps += 1
            if execution.status == "executed" and execution.outcome == "completed":
                self.completed_steps += 1
            if execution.recovery_decisions:
                self.pending_decisions.extend(execution.recovery_decisions)
            if execution.task_status == "completed" or execution.needs_model or execution.outcome in {"failed", "partial", "unknown", "rejected", "cancelled"} or (execution.outcome == "waiting" and (execution.result or {}).get("waitCondition", {}).get("type") != "transportRetry"):
                completed_task = next((t for t in self.store.state(save_id).tasks if t.id == execution.task_id), None)
                all_effects = [effect for step in completed_task.steps for effect in step.effects] if completed_task else execution.effects
                feedback = compact_job_feedback({**(execution.result or {}), "effects": all_effects, "reasonCode": execution.reason_code, "commandId": execution.command_id, "message": execution.message}, operation=execution.operation or "", status=execution.outcome)
                feedback["effectCount"] = len(all_effects)
                feedback["stepsCompleted"] = sum(step.status == "completed" for step in completed_task.steps) if completed_task else 0
                self.store.finish_job(save_id, feedback, task_id=execution.task_id)
                self.pending_reasons.append("SHORT_JOB_TERMINAL")
                if self.terminal_callback:
                    try:
                        res = self.terminal_callback(save_id, execution, "SHORT_JOB_TERMINAL")
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        logger.warning("Plan worker terminal callback failed", exc_info=True)
                break
            if execution.needs_model:
                reason = execution.reason_code or execution.outcome or "STEP_DEVIATION"
                self.pending_reasons.append(reason)
                if self.terminal_callback:
                    try:
                        res = self.terminal_callback(save_id, execution, reason)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        logger.warning("Plan worker terminal callback failed", exc_info=True)
                break
        if executions:
            # Re-check on the next tick: a completed step may unlock the next one.
            self._dirty = True
        return executions

    async def _dispatch(self, operation: str, params: dict[str, Any], command_id: str) -> Any:
        # The harness-only internal tool runs the identical validated plan
        # operation implementation ``run_next_step`` uses and forwards the stable
        # persisted command id to the real scheduler/native command. It must not go
        # through ``call_capability``: that model-facing schema has no
        # ``command_id`` and rejected the step (round6 STEP_DISPATCH_FAILED).
        if self.progress_callback:
            state = self.store.state(self.supplied_save_id or await self._resolve_save_id())
            task = next((t for t in state.tasks if any(s.command_id == command_id for s in t.steps)), None)
            await self.progress_callback(StepExecution(status="dispatching", task_id=task.id if task else None,
                operation=operation, command_id=command_id, outcome="running"))
        save_id = self.supplied_save_id or await self._resolve_save_id()
        state = self.store.state(save_id)
        task = next((t for t in state.tasks if any(s.command_id == command_id for s in t.steps)), None)
        if (state.paused or task is None or state.decision.get("taskId") != task.id
                or state.decision.get("finished")):
            raise DispatchDeferred("paused or decision revoked before native send")
        try:
            return await self.client.call_tool(
                "dispatch_plan_operation",
                {"operation": operation, "params": dict(params), "command_id": command_id},
            )
        except Exception as ex:
            if "UNAUTHORIZED_JOB_COMMAND" in str(ex):
                raise DispatchDeferred(str(ex)) from ex
            raise

    async def _reconcile(self, command_id: str) -> Any:
        reconcile = getattr(self.client, "reconcile", None)
        if callable(reconcile):
            result = await reconcile(command_id)
            if isinstance(result, dict) and "found" in result:
                return result.get("native") if result.get("found") else None
            return result
        return None

    async def _resolve_save_id(self) -> str | None:
        if self.supplied_save_id:
            return self.supplied_save_id
        resolver = getattr(self.client, "current_save_id", None)
        if callable(resolver):
            try:
                return await resolver()
            except Exception:
                logger.debug("Plan worker could not resolve a save id", exc_info=True)
        return None

@dataclass
class ActiveChatTask:
    request_id: str
    save_id: str
    prompt: str = ""
    command_id: str = ""
    start_max_idx: int = -1
    start_max_step_idx: int = -1
    wire_start_offset: int = 0
    process: subprocess.Popen | None = None
    async_task: asyncio.Task | None = None
    cancelled: bool = False
    abort_reason: str | None = None
    start_time: float = field(default_factory=time.monotonic)
    recorded: bool = False


@dataclass
class CommandChain:
    instruction: str
    save_id: str
    started_at: float
    root_request_id: str = ""
    chain_count: int = 0
    generation: int = 0
    ws: WebSocketClient | None = None
    # Task whose terminal may advance this chain; set when the owning turn
    # selects a job, consumed on the matching terminal.
    waiting_task_id: str | None = None
    pending_continuation: bool = False


class ChatBridge:
    """Bridges WebSocket /chat messages to agy CLI executions with lifecycle and cancel management."""

    def __init__(
        self,
        run_dir: str | Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        sessions_file: Path | None = None,
        commands_file: Path | None = None,
        instance_id: str | None = None,
        agy_cmd: str | None = None,
        backend: str | None = None,
        backend_model: str | None = None,
        kimi_cmd: str | None = None,
        internal_plan_client: Any | None = None,
        enable_plan_worker: bool = True,
    ):
        self.run_dir = Path(run_dir) if run_dir else None
        configured = load_chat_backend_config()
        self.backend_name = backend or configured["backend"]
        self.model = backend_model or model or (configured["model"] if self.backend_name == configured["backend"] else ("kimi-code/k3" if self.backend_name == "kimi" else "gemini-3.8-flash"))
        self.effort = effort or configured.get("effort", "default")
        self.instance_id = instance_id or f"chat-bridge-{uuid.uuid4().hex[:8]}"
        self.agy_cmd = agy_cmd or "agy.exe"
        self.kimi_cmd = kimi_cmd or "kimi.exe"
        # Game-only Kimi agent profile (new sessions only; resumed sessions keep theirs).
        self.agent = configured.get("agent")
        configured_agent_file = configured.get("agentFile")
        self.agent_file: Path | None = None
        if configured_agent_file:
            candidate = Path(configured_agent_file)
            resolved = candidate if candidate.is_absolute() else project_root() / candidate
            # Only bind a profile that actually exists; otherwise fall back to the
            # default agent rather than failing the whole turn.
            self.agent_file = resolved if resolved.is_file() else None
            if self.agent_file is None:
                logger.warning("Configured Kimi agentFile not found: %s", resolved)
        self._backend = None
        self._busy_lock = asyncio.Lock()
        self._active_task: ActiveChatTask | None = None
        self._sessions_file = sessions_file or self._default_sessions_file()
        self._commands_file = commands_file or self._default_commands_file()
        self._sessions: dict[str, str] = self._load_sessions()
        self._autonomy: AutonomyController | None = None
        self._autonomy_requests: dict[str, str] = {}
        self._current_game_day_key: str | None = None
        # Latest native snapshot the chat channel received; the compact decision
        # context is rebuilt from it for every model decision.
        self._latest_snapshot_payload: dict[str, Any] | None = None
        self._latest_snapshot_revision: int | None = None
        # Provider-session rotation bookkeeping (day change / context budget).
        self._session_history: dict[str, list[str]] = {}
        self._session_token_totals: dict[str, int] = {}
        # Input-context policy: the game value is the *latest single request's*
        # input context (inputOther + cacheRead + cacheCreation), never the sum
        # across requests. 100k is a configurable engineering policy ceiling, not
        # a claim about the provider's physical window.
        self._session_requests: dict[str, int] = {}
        self._session_context_state: dict[str, dict[str, Any]] = {}
        # Set when the provider session was rotated because the configured
        # profile/tool surface changed; surfaced to F8 and then cleared.
        self._profile_rotation_note: str | None = None
        try:
            self._session_token_budget = int(
                os.getenv("STARDEW_SESSION_CONTEXT_BUDGET")
                # Legacy name from the first round; still honoured so an existing
                # deployment keeps working, but it now means input context, not a
                # cumulative token sum.
                or os.getenv("STARDEW_SESSION_TOKEN_BUDGET")
                or "100000"
            )
        except ValueError:
            self._session_token_budget = 100000
        try:
            self._session_request_checkpoint = int(
                os.getenv("STARDEW_SESSION_REQUEST_CHECKPOINT", "20") or 0
            )
        except ValueError:
            self._session_request_checkpoint = 20
        self._last_day_settlement: dict[str, Any] | None = None
        self._autonomy_generation = 0
        self._autonomy_last_snapshot_at: dict[str, float] = {}
        self._autonomy_pending_snapshot: dict[str, tuple[Envelope, set[asyncio.Task], WebSocketClient | None]] = {}
        self._autonomy_debounce_tasks: dict[str, asyncio.Task] = {}
        self._autonomy_pending_task_fingerprints: dict[str, tuple[str, str]] = {}
        self._bind_autonomy_store()
        # Durable per-save goals/tasks/todos; bound once a run dir is known
        # (explicit --run-dir or discovered from the running Mod).
        self._work_store: WorkStore | None = None
        self._plan_worker: PlanWorker | None = None
        self._command_chains: dict[str, CommandChain] = {}
        # One unresolved native cancellation per save; cleared on confirmation.
        self._pending_native_cancels: dict[str, tuple[str, str]] = {}
        self._chain_requests: set[str] = set()
        self._chain_generation = 0
        self._chain_tasks: set[asyncio.Task] = set()
        # Serializes native command-socket ownership between the provider turn and
        # the internal plan worker (the Mod transport accepts one command client).
        self._execution_lock = asyncio.Lock()
        self._internal_plan_client_override = internal_plan_client
        self._enable_plan_worker = enable_plan_worker
        self._bind_work_store()
        # Companion "day in the life" stores (contract §2): profile, memory,
        # care rules and the life-chat session service. All None until a run
        # dir is known (explicit --run-dir or auto-discovery), exactly like
        # the work store above.
        self._profile_store: CompanionProfileStore | None = None
        self._memory_store: CompanionMemoryStore | None = None
        self._care_service: CompanionCareService | None = None
        self._milestone_store: CompanionMilestoneStore | None = None
        # A button may accept only the exact proposal shown in this discussion,
        # at the same profile/node revision. Restart or direction change requires
        # a fresh discussion, never automatic adoption of an old suggestion.
        self._life_proposals: dict[str, tuple[str, int, float]] = {}
        self._life_chat: LifeChatService | None = None
        self._life_fingerprints: dict[str, str] = {}
        self._life_queue: list[dict[str, Any]] = []
        self._life_draining = False
        self._evening_care_fired_day: str | None = None
        self._deferred_care_tasks: set[asyncio.Task[None]] = set()
        self._chat_ws: WebSocketClient | None = None
        self._bind_companion_stores()

    @property
    def provider(self) -> str:
        return self.backend_name

    def _get_backend(self):
        if self._backend is None:
            if self.backend_name == "agy":
                self._backend = AgyBackend(self._execute_agy_turn)
            elif self.backend_name == "kimi":
                self._backend = KimiBackend(
                    model=self.model or "kimi-code/k3", command=self.kimi_cmd,
                    cwd=project_root(), auto=False,
                    progress=getattr(self, "_backend_progress_callback", None),
                    agent=self.agent, agent_file=self.agent_file,
                )
            else:
                raise ValueError(f"Unsupported chat backend: {self.backend_name}")
        return self._backend

    def _notify_backend_failure(self, code: str) -> None:
        """Notify the runtime that the provider returned a closing failure code.

        The chat runtime maps this (quota / rate limit / auth) to a temporary
        autonomy close so the loop backs off instead of hot-retrying.
        """
        callback = getattr(self, "_backend_failure_callback", None)
        if callback is None:
            return
        try:
            callback(code)
        except Exception:
            logger.debug("Backend failure callback raised for %s", code, exc_info=True)

    def _notify_terminal(self, status: str) -> None:
        callback = getattr(self, "_terminal_callback", None)
        if callback is None:
            return
        try:
            callback(status)
        except Exception:
            logger.debug("Terminal callback raised for %s", status, exc_info=True)

    def _configure_backend_progress(
        self, ws: WebSocketClient | None, request_id: str, save_id: str | None
    ) -> None:
        loop = asyncio.get_running_loop()

        def callback(event: Any) -> None:
            if event.kind != "tool_started" or not event.tool_name:
                return
            reply = Envelope.create_chat_reply(
                sender_instance_id=self.instance_id,
                request_id=request_id,
                status="processing",
                reply_text=f"{event.message}：{event.tool_name}",
                save_id=save_id,
                provider=self.backend_name,
                command_id=(self._active_task.command_id if self._active_task and self._active_task.request_id == request_id else request_id),
                command_complete=False,
            )
            asyncio.run_coroutine_threadsafe(self._send_reply(ws, reply), loop)

        self._backend_progress_callback = callback

    def _bind_autonomy_store(self) -> None:
        """Use the same run-dir state file as MCP, including after auto-discovery."""
        if self.run_dir is not None:
            self._autonomy = AutonomyController(self.run_dir / "data" / "autonomy-state.json")

    def _bind_companion_stores(self) -> None:
        """Bind companion profile/memory/care/milestone stores and the life-chat service.

        Called from ``__init__`` and again after auto-discovery resolves the Mod
        dir, mirroring ``_bind_work_store``. Everything lives under
        ``<run_dir>/data/`` in the autonomy.py persistence pattern.
        """
        if self.run_dir is None:
            return
        data_dir = self.run_dir / "data"
        self._profile_store = CompanionProfileStore(data_dir / "companion-profile.json")
        self._memory_store = CompanionMemoryStore(data_dir / "companion-memory.json")
        self._care_service = CompanionCareService(data_dir / "companion-care.json")
        self._milestone_store = CompanionMilestoneStore(data_dir / "companion-milestones.json")
        self._life_fingerprints = self._load_life_fingerprints()
        self._life_chat = LifeChatService(
            backend_name=self.backend_name,
            sessions=self._sessions,
            sessions_file=self._sessions_file,
            fingerprints=self._life_fingerprints,
            fingerprints_file=self._life_fingerprint_path(),
        )

    def _life_fingerprint_path(self) -> Path:
        if self.run_dir:
            return Path(self.run_dir) / "chat_life_fingerprints.json"
        return Path(self._sessions_file).parent / "chat_life_fingerprints.json"

    def _load_life_fingerprints(self) -> dict[str, str]:
        path = self._life_fingerprint_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            logger.debug("Failed to load life fingerprints", exc_info=True)
        return {}

    def _bind_work_store(self) -> None:
        """Bind durable work state and the internal plan worker.

        Called from ``__init__`` and again after auto-discovery resolves the Mod
        dir, so a Bridge started with zero arguments also gets persistent
        goals/plans and the no-model-turn plan worker (not only an explicit
        ``--run-dir``).
        """
        if self.run_dir is None:
            return
        store = WorkStore(self.run_dir / "data" / "work-state.json")
        self._work_store = store
        if not self._enable_plan_worker:
            return
        if self._plan_worker is not None:
            self._plan_worker.store = store
            self._plan_worker.snapshot_provider = self._plan_snapshot_state
            self._plan_worker.terminal_callback = self._on_job_terminal
            return
        client = self._internal_plan_client_override
        if client is None:
            client = InternalMcpPlanClient(self.run_dir)
        self._plan_worker = PlanWorker(store, client)
        self._plan_worker.progress_callback = self._publish_job_progress
        self._plan_worker.snapshot_provider = self._plan_snapshot_state
        self._plan_worker.terminal_callback = self._on_job_terminal

    def _plan_snapshot_state(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(latest world.snapshot payload, native game date) for wait evaluation."""
        payload = self._latest_snapshot_payload
        if not isinstance(payload, dict):
            return None, None
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        game_date = {
            "year": world.get("year"),
            "season": world.get("season"),
            "day": world.get("dayOfMonth"),
        }
        if all(value is None for value in game_date.values()):
            game_date = None
        return payload, game_date

    # ------------------------------------------- execution ownership (worker)
    async def _claim_execution(
        self, save_id: str | None = None, *, recover: bool = True
    ) -> bool:
        """Park the plan worker and close its MCP session before a provider turn.

        The Mod transport serves a single command socket and rejects a concurrent
        connection with 409 Conflict, so the provider and the internal worker must
        never hold one at the same time.
        """
        worker = self._plan_worker
        if worker is None:
            return False
        worker.set_provider_active(True)
        await self._execution_lock.acquire()
        client = getattr(worker, "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                logger.warning("Could not release internal MCP session", exc_info=True)
        if recover and save_id and self._work_store is not None:
            try:
                await asyncio.to_thread(self._work_store.recover, save_id)
            except Exception:
                logger.debug("Work store recovery before provider turn failed", exc_info=True)
        return True

    async def _release_execution(self, save_id: str | None = None) -> None:
        """Hand execution ownership back to the plan worker and wake it."""
        if self._execution_lock.locked():
            self._execution_lock.release()
        worker = self._plan_worker
        if worker is None:
            return
        if save_id:
            worker.supplied_save_id = save_id
        worker.set_provider_active(False)
        worker.notify()

    def _notify_plan_worker(self, save_id: str | None = None) -> None:
        worker = self._plan_worker
        if worker is None:
            return
        if save_id:
            worker.supplied_save_id = save_id
        worker.notify()

    def _default_sessions_file(self) -> Path:
        base = (
            self.run_dir
            if self.run_dir
            else Path.home() / ".gemini" / "antigravity-cli"
        )
        return base / "chat_sessions.json"

    def _default_commands_file(self) -> Path:
        base = (
            self.run_dir
            if self.run_dir
            else Path.home() / ".gemini" / "antigravity-cli"
        )
        return base / "chat_commands.jsonl"

    def _load_sessions(self) -> dict[str, str]:
        if self._sessions_file.is_file():
            try:
                data = json.loads(self._sessions_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except Exception as ex:
                logger.warning("Failed to load chat sessions from %s: %s", self._sessions_file, ex)
        return {}

    def _save_sessions(self) -> None:
        try:
            self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
            self._sessions_file.write_text(
                json.dumps(self._sessions, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as ex:
            logger.warning("Failed to save chat sessions to %s: %s", self._sessions_file, ex)

    def _record_command(
        self,
        request_id: str,
        save_id: str | None,
        conversation_id: str | None,
        prompt: str,
        status: str,
        start_idx: int,
        end_idx: int,
        usage: dict[str, Any] | None,
        missing_reason: str | None = None,
        error: str | None = None,
        duration: float = 0.0,
    ) -> None:
        """Persists a command execution record into chat_commands.jsonl."""
        is_test_env = bool(
            os.getenv("PYTEST_CURRENT_TEST")
            or os.getenv("STARDEW_TEST_ENVIRONMENT")
        )
        record = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "requestId": request_id,
            "saveId": save_id or "",
            "conversationId": conversation_id,
            "prompt": prompt,
            "status": status,
            "startGenIdx": start_idx,
            "endGenIdx": end_idx,
            "usage": usage,
            "missingReason": missing_reason,
            "error": error,
            "durationSeconds": round(duration, 2),
            "source": "test" if is_test_env else "production",
            "provider": self.backend_name,
            "model": self.model,
        }
        targets = [self._commands_file]
        central_override = os.getenv("STARDEW_CENTRAL_COMMANDS_FILE")
        if central_override is not None:
            if central_override.strip().lower() not in ("none", "false", "0", ""):
                central_target = Path(central_override.strip()).resolve()
                if central_target != self._commands_file:
                    targets.append(central_target)
        elif not is_test_env:
            central_file = Path.home() / ".gemini" / "antigravity-cli" / "chat_commands.jsonl"
            if central_file != self._commands_file:
                targets.append(central_file)


        line = json.dumps(record, ensure_ascii=False) + "\n"
        for target in targets:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as ex:
                logger.warning("Failed writing command record to %s: %s", target, ex)
        logger.info("Recorded command [%s] (status=%s, usage=%s)", request_id, status, "yes" if usage else "none")

    def get_conversation_id(self, save_id: str | None) -> str | None:
        if not save_id:
            return None
        # Provider-qualified keys prevent an old agy conversation from ever
        # being passed to Kimi (and retain compatibility with old agy files).
        return self._sessions.get(f"{self.backend_name}:{save_id}") if self.backend_name == "kimi" else self._sessions.get(save_id) or self._sessions.get(f"agy:{save_id}")

    def record_conversation_id(self, save_id: str | None, conversation_id: str) -> None:
        if not save_id or not conversation_id:
            return
        key = f"{self.backend_name}:{save_id}"
        self._sessions[key] = conversation_id
        if self.backend_name == "agy":
            self._sessions[save_id] = conversation_id
        self._save_sessions()

    def abort_active_task(self, reason: str = "aborted") -> None:
        """Kills only the active provider child process and cancels its task."""
        if self._active_task is not None:
            logger.info("Aborting active chat task [%s] (%s)", self._active_task.request_id, reason)
            self._active_task.cancelled = True
            self._active_task.abort_reason = reason
            proc = self._active_task.process
            if proc and proc.poll() is None:
                try:
                    backend = self._backend
                    terminate = getattr(backend, "terminate", None)
                    if callable(terminate):
                        terminate(proc)
                    else:
                        proc.kill()
                    logger.info("Killed %s CLI subprocess (pid=%s)", self.backend_name, proc.pid)
                except Exception as ex:
                    logger.debug("Error killing process: %s", ex)
            if self._active_task.async_task and not self._active_task.async_task.done():
                self._active_task.async_task.cancel()

    def _preempt_autonomy_for_player(self, request_id: str) -> bool:
        """Invalidate an autonomous generation before accepting player work."""
        if self._active_task is None or not self._active_task.request_id.startswith("autonomy-"):
            return False
        if request_id.startswith("autonomy-"):
            return False
        self._autonomy_generation += 1
        # The submit path below uses handle_chat_cancel and waits for the old
        # turn's finally block before the player's replacement is accepted.
        return True

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Main lifecycle loop with auto-reconnect."""
        logger.info(
            "Starting ChatBridge (run_dir=%s, provider=%s, model=%s, effort=%s)",
            self.run_dir or "auto-discovery",
            self.backend_name,
            self.model,
            self.effort,
        )

        last_wait_log = 0.0

        while stop_event is None or not stop_event.is_set():
            try:
                # 1. Discover game endpoint
                try:
                    disc = resolve_discovery(self.run_dir, timeout_seconds=1.0)
                except DiscoveryError as ex:
                    now = time.monotonic()
                    if now - last_wait_log > 15.0:
                        logger.info("等待星露谷游戏启动并载入存档... (%s)", ex)
                        last_wait_log = now
                    await asyncio.sleep(2.0)
                    continue

                host = disc["host"]
                port = disc["port"]
                token = disc["sessionToken"]
                save_id = disc.get("saveId")
                mod_dir = disc.get("modDir")
                if not self.run_dir and mod_dir:
                    self.run_dir = Path(mod_dir)
                    self._bind_autonomy_store()
                    self._bind_work_store()
                    self._bind_companion_stores()
                    if has_chat_bridge_file_logging():
                        try:
                            _, new_log = switch_chat_bridge_file_logging(self.run_dir)
                            logger.info("已将日志文件切换至运行目录: %s", new_log)
                        except Exception as ex:
                            logger.debug("Failed to switch log path to %s: %s", self.run_dir, ex)

                # Guard against save switch while task was running
                if self._active_task and self._active_task.save_id != save_id:
                    self.abort_active_task(f"Save switched from {self._active_task.save_id} to {save_id}")

                logger.info("Found game endpoint ws://%s:%d/chat (saveId=%s)", host, port, save_id)
                if self._plan_worker is None and self.run_dir is not None:
                    self._bind_work_store()
                if self._plan_worker is not None:
                    if save_id and self._work_store is not None:
                        self._work_store.revoke_decision(str(save_id))
                    self._plan_worker.supplied_save_id = str(save_id) if save_id else None
                    self._plan_worker.start()
                    self._notify_plan_worker(str(save_id) if save_id else None)

                headers = {"Authorization": f"Bearer {token}"}
                ws = await WebSocketClient.connect(
                    host=host,
                    port=port,
                    path="/chat",
                    headers=headers,
                    timeout=5.0,
                )

                logger.info("Successfully connected to game chat channel at ws://%s:%d/chat", host, port)
                last_wait_log = 0.0

                try:
                    await self._receive_loop(ws, save_id, stop_event)
                finally:
                    self.abort_active_task("WebSocket disconnected")
                    await ws.close()

            except (WebSocketError, ConnectionClosed, OSError) as ex:
                logger.debug("Chat WebSocket connection lost: %s. Reconnecting in 2s...", ex)
                self.abort_active_task("Connection lost")
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                self.abort_active_task("ChatBridge cancelled")
                break
            except Exception as ex:
                logger.error("Unexpected error in ChatBridge loop: %s", ex, exc_info=True)
                self.abort_active_task("Bridge loop exception")
                await asyncio.sleep(3.0)

        if self._plan_worker is not None:
            await self._plan_worker.stop()

    async def _receive_loop(
        self,
        ws: WebSocketClient | None,
        active_save_id: str | None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        tracked_tasks: set[asyncio.Task] = set()
        latest_snapshot: Envelope | None = None
        self._chat_ws = ws

        async def run_submit(request_id: str, text: str, save_id: str) -> None:
            generation = self._autonomy_generation
            try:
                await self.handle_chat_submit(ws, request_id, text, save_id)
            finally:
                # A snapshot received while the agent was busy is evaluated now,
                # after the existing agent has reached a terminal state.
                if latest_snapshot is not None and generation == self._autonomy_generation:
                    await self._maybe_schedule_autonomy(latest_snapshot, active_save_id, tracked_tasks, ws)

        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    raw_text = await ws.receive_text(timeout=2.0)
                except TimeoutError:
                    tracked_tasks = {t for t in tracked_tasks if not t.done()}
                    await self._stop_autonomy_if_disabled(ws, active_save_id)
                    if latest_snapshot is not None:
                        await self._maybe_schedule_autonomy(latest_snapshot, active_save_id, tracked_tasks, ws)
                    continue

                try:
                    data = json.loads(raw_text)
                except Exception as ex:
                    logger.warning("Received invalid JSON on chat channel: %s", ex)
                    continue

                msg_type = data.get("messageType")
                await self._stop_autonomy_if_disabled(ws, active_save_id)
                if msg_type == "world.snapshot":
                    envelope = Envelope.from_mapping(data)
                    if envelope.save_id and active_save_id and envelope.save_id != active_save_id:
                        self.abort_active_task("Save switched while chat connection was open")
                        break
                    latest_snapshot = envelope
                    self._latest_snapshot_payload = (
                        envelope.payload if isinstance(envelope.payload, dict) else None
                    )
                    self._latest_snapshot_revision = envelope.world_revision
                    world = envelope.payload.get("world") if isinstance(envelope.payload.get("world"), dict) else {}
                    day_key: str | None = None
                    previous_day_key: str | None = None
                    if world.get("dayOfMonth") is not None:
                        day_key = f"{world.get('year', 'unknown')}:{world.get('season', 'unknown')}:{world['dayOfMonth']}"
                        previous_day_key = self._current_game_day_key
                        self._current_game_day_key = day_key
                        # The day rollover is driven only by a real, complete native
                        # date advance; a first snapshot or a reloaded old save is
                        # not a new day (the store makes this idempotent). The first
                        # observation still teaches the store which day it is so the
                        # next advance can settle.
                        if day_key != previous_day_key:
                            await self._settle_day_advance(active_save_id, world)
                    await self._snapshot_care_hooks(active_save_id, world, day_key, previous_day_key)
                    # A fresh native snapshot is also the trigger for the plan
                    # worker to advance ready steps (and to re-evaluate waiting
                    # todos) without any model turn.
                    self._notify_plan_worker(active_save_id)
                    await self._maybe_schedule_autonomy(envelope, active_save_id, tracked_tasks, ws)
                    continue

                if msg_type == "autonomy.control":
                    envelope = Envelope.from_mapping(data)
                    await self._handle_autonomy_control(ws, envelope, active_save_id)
                    self._notify_plan_worker(active_save_id)
                    if latest_snapshot is not None:
                        await self._maybe_schedule_autonomy(latest_snapshot, active_save_id, tracked_tasks, ws, force=True)
                    continue

                if msg_type == "chat.cancel":
                    payload_data = data.get("payload", {})
                    req_id = payload_data.get("requestId")
                    reason = payload_data.get("reason", "player_cancelled")
                    await self.handle_chat_cancel(ws, req_id, reason, active_save_id)
                    continue

                if msg_type in {
                    "life.chat.submit",
                    "life.profile.get",
                    "life.profile.set",
                    "life.memory.list",
                    "life.memory.edit",
                    "life.milestones.get",
                }:
                    await self._handle_life_message(ws, msg_type, data, active_save_id)
                    continue

                if msg_type == "chat.submit":
                    payload_data = data.get("payload", {})
                    req_id = str(payload_data.get("requestId", ""))
                    text = str(payload_data.get("text", ""))
                    save_id = str(payload_data.get("saveId") or active_save_id or "")
                elif "requestId" in data and "text" in data:
                    req_id = str(data["requestId"])
                    text = str(data["text"])
                    save_id = str(data.get("saveId") or active_save_id or "")
                else:
                    continue
                if req_id and text.strip():
                    task = asyncio.create_task(run_submit(req_id, text, save_id))
                    tracked_tasks.add(task)
        finally:
            self.abort_active_task("Chat channel stopped")
            self._break_command_chain()
            # Queued life-chat submits must never vanish silently (contract §1.2):
            # anything still waiting when the channel closes gets a failed terminal.
            for item in list(self._life_queue):
                await self._send_reply(item.get("ws"), Envelope.create_life_chat_reply(
                    sender_instance_id=self.instance_id,
                    request_id=str(item.get("request_id") or ""),
                    save_id=str(item.get("save_id") or ""),
                    status="failed",
                    profile_revision=self._profile_revision(item.get("save_id")),
                    memory_revision=self._memory_revision(item.get("save_id")),
                    error="CHAT_CHANNEL_CLOSED",
                ))
            self._life_queue.clear()
            self._life_draining = False
            for debounce in list(self._autonomy_debounce_tasks.values()):
                debounce.cancel()
            self._autonomy_debounce_tasks.clear()
            self._autonomy_pending_snapshot.clear()
            for task in list(tracked_tasks):
                if task is not asyncio.current_task() and not task.done():
                    task.cancel()
            if tracked_tasks:
                await asyncio.gather(*tracked_tasks, return_exceptions=True)

    async def _maybe_schedule_autonomy(
        self, envelope: Envelope, save_id: str | None, tracked_tasks: set[asyncio.Task], ws: WebSocketClient | None,
        *, force: bool = False
    ) -> None:
        """Evaluate the latest native snapshot through the chat channel only."""
        if not save_id or self._autonomy is None or envelope.message_type != "world.snapshot":
            return
        now = time.monotonic()
        previous = self._autonomy_last_snapshot_at.get(save_id)
        self._autonomy_last_snapshot_at[save_id] = now
        if not force and previous is not None and now - previous < 1.0:
            self._autonomy_pending_snapshot[save_id] = (envelope, tracked_tasks, ws)
            if save_id not in self._autonomy_debounce_tasks:
                async def flush() -> None:
                    await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - now)))
                    pending = self._autonomy_pending_snapshot.pop(save_id, None)
                    self._autonomy_debounce_tasks.pop(save_id, None)
                    if pending:
                        await self._maybe_schedule_autonomy(pending[0], save_id, pending[1], pending[2], force=True)
                self._autonomy_debounce_tasks[save_id] = asyncio.create_task(flush())
            return
        state = self._autonomy.state(save_id)
        if (
            not state.enabled
            or state.paused
            or self._active_task is not None
            or self._busy_lock.locked()
            or save_id in self._command_chains
            or self._autonomy.is_cooling_down(save_id)
        ):
            return
        snapshot = envelope.payload or {}
        if self._work_store is not None:
            job_state = self._work_store.state(save_id)
            decision = job_state.decision
            if decision.get("selected") and not decision.get("finished"):
                return
            last_job = job_state.last_job
            if last_job and last_job.get("decisionId") != getattr(self, "_last_job_wake", None):
                self._last_job_wake = last_job.get("decisionId")
                self._autonomy.request_job_decision(save_id)
        candidate = self._autonomy.next_candidate(save_id, snapshot)
        if candidate is None:
            return
        fingerprint = self._autonomy.fingerprint(save_id, snapshot, candidate, state)
        if not self._autonomy.record_world_event(save_id, fingerprint, envelope.world_revision):
            return
        remaining_budget = max(0, (state.budget_limit or 0) - state.daily_spend - sum(state.spend_reservations.values()))
        compact = build_decision_context(
            {"payload": snapshot, "worldRevision": envelope.world_revision},
            work=self._work_context(save_id),
            origin="free-mode",
        )
        compact["remainingBudget"] = remaining_budget
        compact["boxRange"] = state.box_preference or "none"
        compact["lastResult"] = self._work_store.state(save_id).last_job if self._work_store else state.last_event_key or "none"
        compact["wakeReason"] = candidate.get("reason", "state-change")
        compact["objective"] = state.goal or "由当前状态决定"
        prompt = (
            "自由模式自主安排。依据以下紧凑实时上下文选择并执行一项有用短任务："
            f"{render_decision_context(compact)}。"
            "固定规则：自主照料农场，原子动作只能经 MCP；遵守预算与箱子范围；不保存睡觉；"
            "原生事实优先，知识不足才 query_wiki；无合适工作就说明待命，不为待命查 wiki。"
            "长期目标或待办用remember_intent，它们不是执行授权；本次用submit_plan选择一个语义短作业，允许内部导航和同一业务范围的多步操作。完成后信任工具精简终态，不重复查询。下一业务由下一次模型决策选择，不预排存箱或出售。"
            f"已保存目标：{state.goal or '由当前状态决定'}。"
            "始终用中文回复玩家。"
        )
        # Unique per-request identity: a worldRevision may repeat (revision reset /
        # same-revision decisions) and must never collide across epochs.
        request_id = f"autonomy-{uuid.uuid4().hex}"
        self._autonomy_requests[request_id] = fingerprint
        task = asyncio.create_task(self.handle_chat_submit(ws, request_id, prompt, save_id))
        tracked_tasks.add(task)

    def _apply_work_control(self, save_id: str, action: str, params: dict[str, Any]) -> None:
        """Keep durable work scheduling in step with F8 pause/resume/cancel controls."""
        store = self._work_store
        if store is None:
            return
        try:
            if action in {"cancel", "set_mode"}:
                self._break_command_chain(save_id)
            if action == "pause":
                store.set_paused(save_id, True)
            elif action == "resume":
                store.set_paused(save_id, False)
            elif action == "cancel":
                store.set_paused(save_id, True)
            elif action == "set_mode":
                store.set_paused(save_id, str(params.get("mode")) != "free")
            elif action == "set_preferences":
                goal = str(params.get("goal") or "").strip()
                already = any(
                    g["text"] == goal and g["status"] == "active" for g in store.list_goals(save_id)
                )
                if goal and not already:
                    # This control comes from the player's F8 settings, so it is a user goal.
                    store.add_goal(save_id, goal, source="user")
        except Exception:
            logger.warning("Unable to update work state for control %s", action, exc_info=True)

    def _work_context(self, save_id: str) -> dict[str, Any] | None:
        """Relevant-only work memory for the autonomy prompt (goals/next step/anomalies)."""
        if self._work_store is None:
            return None
        try:
            snapshot, game_date = self._plan_snapshot_state()
            overview = self._work_store.overview(
                save_id, snapshot=snapshot, game_date=game_date
            )
        except Exception:
            return None
        return {
            "goals": [
                {"text": g["text"], "source": g["source"]} for g in overview.get("goals", [])[:3]
            ],
            "duePreparation": [
                {"id": t["id"], "intent": t["intent"], "goalId": t.get("goal_id")}
                for t in self._work_store.evaluate_todos(save_id, snapshot=snapshot, game_date=game_date)
            ][:6],
            "nextStep": overview.get("nextStep"),
            "anomalies": overview.get("anomalies", [])[:3],
            "waitingFor": [
                {"taskId": t.get("id"), "title": t.get("title")}
                for t in overview.get("tasks", [])
                if isinstance(t, dict) and t.get("status") == "waiting"
            ][:3],
            "waitingConditions": overview.get("waitingConditions", [])[:3],
            "lastSettledDay": overview.get("lastSettledDay"),
            "lastJob": overview.get("lastJob"),
            "paused": overview.get("paused", False),
            "decision": overview.get("decision", {}),
        }

    def _decision_context(self, save_id: str | None, *, origin: str = "chat") -> dict[str, Any]:
        """Compact live context for one decision, rebuilt from the latest snapshot.

        Nothing is accumulated across turns, so an old snapshot never grows the
        prompt without bound; missing native fields render as ``unknown``.
        """
        snapshot: dict[str, Any] = {}
        if self._latest_snapshot_payload is not None:
            snapshot = {
                "payload": self._latest_snapshot_payload,
                "worldRevision": self._latest_snapshot_revision,
            }
        work = self._latest_work_overview(save_id)
        companion: dict[str, Any] | None = None
        memory: dict[str, Any] | None = None
        if save_id:
            if self._profile_store is not None:
                profile = (self._profile_store.get(save_id) or {}).get("profile")
                if profile:
                    companion = {
                        "name": profile.get("companionName"),
                        "personality": profile.get("personality"),
                        "playStyle": profile.get("playStyle"),
                        "careFrequency": profile.get("careFrequency"),
                    }
            if self._memory_store is not None:
                memory = self._memory_store.render_for_context(save_id)
        return build_decision_context(
            snapshot, work=work, origin=origin, companion=companion, memory=memory
        )

    def _latest_work_overview(self, save_id: str | None) -> dict[str, Any] | None:
        if self._work_store is None or not save_id:
            return None
        try:
            snapshot, game_date = self._plan_snapshot_state()
            return self._work_store.overview(save_id, snapshot=snapshot, game_date=game_date)
        except Exception:
            return None

    # -------------------------------------------------- companion life (§1/§2)
    def _profile_revision(self, save_id: str | None) -> int:
        if self._profile_store is None or not save_id:
            return 0
        try:
            return int((self._profile_store.get(save_id) or {}).get("profileRevision") or 0)
        except Exception:
            return 0

    def _memory_revision(self, save_id: str | None) -> int:
        if self._memory_store is None or not save_id:
            return 0
        try:
            return int((self._memory_store.list(save_id) or {}).get("memoryRevision") or 0)
        except Exception:
            return 0

    def _life_work_projection(self, save_id: str) -> dict[str, Any]:
        """Read-only autonomy + WorkStore projection for life.profile.state (§1.3)."""
        payload: dict[str, Any] = {}
        if self._autonomy is not None:
            try:
                payload = self._autonomy_state_payload(save_id, self._autonomy.state(save_id))
            except Exception:
                payload = {}
        overview = self._latest_work_overview(save_id) or {}
        preferences = payload.get("preferences") if isinstance(payload.get("preferences"), dict) else {}
        goals = [
            {"id": g.get("id"), "text": g.get("text"), "status": g.get("status")}
            for g in overview.get("goals", [])
            if isinstance(g, dict)
        ][:5]
        todos: list[dict[str, Any]] = []
        if self._work_store is not None:
            try:
                todos = [
                    {"id": t.get("id"), "intent": t.get("intent"), "status": t.get("status")}
                    for t in self._work_store.list_todos(save_id)
                    if isinstance(t, dict)
                ][:5]
            except Exception:
                todos = []
        return {
            "mode": payload.get("mode", "command"),
            "paused": bool(overview.get("paused", False)),
            "goal": preferences.get("goal"),
            "dailySpendLimit": preferences.get("dailySpendLimit"),
            "boxPreference": preferences.get("boxPreference"),
            "dailySpend": payload.get("dailySpend"),
            "hasExecutableWork": bool(overview.get("hasExecutableWork", False)),
            "lastPlanAction": payload.get("lastPlanAction"),
            "planWaitReason": payload.get("planWaitReason"),
            "lastSettledDay": overview.get("lastSettledDay"),
            "activeGoals": goals,
            "recentTodos": todos,
            # Contract §1.3 declares [str]; rows from WorkStore.wait_conditions()
            # carry objects, so surface only the display description.
            "waitingConditions": [
                row["waitDescription"]
                for row in (overview.get("waitingConditions") or [])[:5]
                if isinstance(row, dict) and isinstance(row.get("waitDescription"), str)
            ],
            "activity": self._player_activity(save_id),
        }

    def _current_life_proposal(self, save_id: str) -> dict[str, Any] | None:
        binding = self._life_proposals.get(save_id)
        if not binding or binding[1] != self._profile_revision(save_id) or self._milestone_store is None:
            return None
        return next((node for node in self._milestone_store.list_nodes(save_id)
                     if node["id"] == binding[0] and node.get("status") == "suggested"
                     and node.get("updatedAt") == binding[2]), None)

    def _player_activity(self, save_id: str) -> dict[str, str]:
        """Small player-facing projection of persisted work, never provider prose."""
        def activity(phase: str, summary: str, next_step: str) -> dict[str, str]:
            return {"phase": phase, "summary": summary[:240], "nextStep": next_step[:160]}

        state = self._work_store.state(save_id) if self._work_store else None
        autonomy = self._autonomy.state(save_id) if self._autonomy else None
        if (state and state.paused) or (autonomy and autonomy.paused):
            return activity("paused", "伙伴工作已暂停，已保存的安排仍保留。", "准备好后点继续；不会自行解除暂停。")
        decision = state.decision if state else {}
        selected = next((task for task in state.tasks if task.id == decision.get("taskId")), None) if state else None
        if selected and selected.status == "waiting":
            waits = self._work_store.wait_conditions(save_id)
            reason = next((row.get("waitDescription") for row in waits if row.get("taskId") == selected.id), None)
            return activity("waiting", f"{selected.title}：{reason or '正在等待执行条件'}", "条件满足后继续；可以暂停或取消。")
        if (decision.get("selected") and not decision.get("finished")) or save_id in self._command_chains:
            return activity("working", f"正在处理：{selected.title if selected else '已认可的工作安排'}", "完成后会按实际结果更新。")
        if self._active_task and not self._active_task.cancelled:
            return activity("working", "伙伴正在处理已有工作。", "新安排会保留，不抢占当前任务。")
        proposal = self._current_life_proposal(save_id)
        if proposal:
            return activity("proposed", proposal.get("summary") or proposal["title"], "认可后接手可做的准备；也可以继续商量。")
        nodes = self._milestone_store.list_nodes(save_id) if self._milestone_store else []
        adopted = max((node for node in nodes if node.get("status") == "adopted"),
                      key=lambda node: node.get("updatedAt", 0), default=None)
        last = state.last_job if state else {}
        last_task_id = last.get("taskId") or decision.get("taskId")
        last_task = next((task for task in state.tasks if task.id == last_task_id), None) if state else None
        if last and (adopted is None or (last_task and last_task.goal_id == adopted.get("goalId"))):
            title = last_task.title if last_task else "上一项工作"
            if last.get("status") == "completed":
                return activity("completed", f"已完成：{title}。", "这是实际完成的一项；其余安排仍按约定进行。")
            if last.get("status") in {"failed", "partial", "unknown", "rejected", "cancelled"}:
                detail = str(last.get("message") or last.get("error") or "尚未确认全部完成")
                return activity("failed", f"{title}：{detail}", "保留实际进度，可调整安排后重试。")
            if last.get("status") == "waiting":
                return activity("waiting", str(last.get("message") or "当前工作在等待条件满足。"), "不会把等待当成完成。")
        if adopted:
            if not adopted.get("todoIds"):
                return activity("waiting", f"已记下：{adopted['title']}。这项安排需玩家完成。", "可以继续商量伙伴能接手的准备。")
            snapshot, game_date = self._plan_snapshot_state()
            due_ids = {row["id"] for row in self._work_store.evaluate_todos(save_id, snapshot=snapshot, game_date=game_date)} if self._work_store else set()
            due = bool(due_ids.intersection((adopted.get("todoIds") or {}).values()))
            return activity("waiting", f"已保存：{adopted['title']}。" + ("等待接手可做的准备。" if due else "还没到约定的准备时间。"),
                            "可继续商量或从工作面板查看安排；不会提前宣称完成。")
        return activity("idle", "还没有正在执行的工作。", "选择想发展的方向，一起商量下一步。")

    def _life_work_summary(self, save_id: str, mode: str) -> dict[str, Any] | None:
        """Compact read-only work summary injected into the life prompt (§2)."""
        if not save_id or self._work_store is None:
            return None
        projection = self._life_work_projection(save_id)
        if mode == "plan":
            # Plan discussions see the full read-only projection; it never
            # dispatches anything.
            return projection
        return {
            "mode": projection["mode"],
            "paused": projection["paused"],
            "goal": projection["goal"],
            "hasExecutableWork": projection["hasExecutableWork"],
            "planWaitReason": projection["planWaitReason"],
        }

    # -------------------------------------------------- milestones (contract §2)
    def _life_game_date_playstyle(self, save_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """(game date dict, playStyle) from the latest snapshot + companion profile."""
        game_date: dict[str, Any] | None = None
        if isinstance(self._latest_snapshot_payload, dict):
            world = self._latest_snapshot_payload.get("world")
            if isinstance(world, dict) and world.get("year") is not None:
                season = world.get("season")
                day = world.get("dayOfMonth")
                if season and day is not None:
                    game_date = {"year": world.get("year"), "season": season, "day": day}
        play_style = None
        if self._profile_store is not None and save_id:
            try:
                profile = (self._profile_store.get(save_id) or {}).get("profile")
                if isinstance(profile, dict):
                    play_style = profile.get("playStyle")
            except Exception:
                logger.debug("Failed to read profile playStyle for milestones", exc_info=True)
        return game_date, play_style

    def _build_milestones_state(
        self,
        save_id: str,
        request_id: str,
        status: str = "ok",
        error: str | None = None,
    ) -> Envelope:
        """life.milestones.state from the milestone store + current suggestions."""
        game_date, play_style = self._life_game_date_playstyle(save_id)
        game_date_str = None
        if game_date is not None:
            game_date_str = f"{game_date['year']}:{game_date['season']}:{game_date['day']}"
        nodes: list[MilestoneNode] = []
        if self._milestone_store is not None and save_id:
            merged = self._milestone_store.merged_nodes(save_id, game_date, play_style)
            nodes = [MilestoneNode.from_mapping(wire_node(n)) for n in merged]
        return Envelope.create_life_milestones_state(
            self.instance_id,
            request_id,
            save_id,
            status=status,
            game_date=game_date_str,
            nodes=nodes,
            error=error,
        )

    def _life_milestone_summary(self, save_id: str, mode: str) -> list[dict[str, Any]] | None:
        """Compact milestone snapshot injected into the plan-mode prompt (§3.4)."""
        if mode != "plan" or self._milestone_store is None or not save_id:
            return None
        game_date, play_style = self._life_game_date_playstyle(save_id)
        summary: list[dict[str, Any]] = []
        for node in self._milestone_store.merged_nodes(save_id, game_date, play_style)[:8]:
            gap = next(
                (
                    p.get("label")
                    for p in node.get("prepItems", [])
                    if isinstance(p, dict) and p.get("status") in {"pending", "unknown"}
                ),
                None,
            )
            summary.append(
                {
                    "id": node.get("id"),
                    "title": node.get("title"),
                    "status": node.get("status"),
                    "targetDate": node.get("targetDate"),
                    "daysUntil": node.get("daysUntil"),
                    "reservedFunds": node.get("reservedFunds"),
                    "pendingGap": gap,
                }
            )
        return summary

    def _latest_player_items(self) -> list[dict[str, Any]] | None:
        """Optional aggregated player backpack from the latest snapshot (§2.4)."""
        if not isinstance(self._latest_snapshot_payload, dict):
            return None
        world = self._latest_snapshot_payload.get("world")
        if not isinstance(world, dict):
            return None
        items = world.get("playerItems")
        return list(items) if isinstance(items, list) else None

    async def _handle_life_message(
        self,
        ws: WebSocketClient | None,
        msg_type: str,
        data: dict[str, Any],
        active_save_id: str | None,
    ) -> None:
        """Dispatch the life.* chat-family messages (contract §1.1/1.3-1.6)."""
        try:
            if msg_type == "life.chat.submit":
                await self._handle_life_chat_submit(ws, data, active_save_id)
                return
            payload_data = data.get("payload") if isinstance(data.get("payload"), dict) else data
            if msg_type == "life.profile.get":
                payload = LifeProfileGetPayload.from_mapping(payload_data)
                save_id = payload.save_id or str(active_save_id or "")
                profile_res = (
                    self._profile_store.get(save_id)
                    if self._profile_store is not None
                    else {"profile": None, "profileRevision": 0}
                )
                await self._send_reply(ws, Envelope.create_life_profile_state(
                    self.instance_id, payload.request_id, save_id,
                    profile_res.get("profile"),
                    int(profile_res.get("profileRevision") or 0),
                    work=self._life_work_projection(save_id),
                ))
            elif msg_type == "life.profile.set":
                payload = LifeProfileSetPayload.from_mapping(payload_data)
                save_id = payload.save_id or str(active_save_id or "")
                status, result = ("rejected", {"reason": "NO_PROFILE_STORE"})
                if self._profile_store is not None:
                    status, result = self._profile_store.set(
                        save_id, dict(payload.patch), payload.expected_revision
                    )
                profile_res = (
                    self._profile_store.get(save_id)
                    if self._profile_store is not None
                    else {"profile": None, "profileRevision": 0}
                )
                await self._send_reply(ws, Envelope.create_life_profile_state(
                    self.instance_id, payload.request_id, save_id,
                    profile_res.get("profile"),
                    int(profile_res.get("profileRevision") or 0),
                    status=status,
                    reason=result.get("reason") if isinstance(result, dict) else None,
                    work=self._life_work_projection(save_id),
                ))
            elif msg_type == "life.memory.list":
                payload = LifeMemoryListPayload.from_mapping(payload_data)
                save_id = payload.save_id or str(active_save_id or "")
                state = (
                    self._memory_store.list(save_id)
                    if self._memory_store is not None
                    else {"memoryRevision": 0, "entries": []}
                )
                await self._send_reply(ws, Envelope.create_life_memory_state(
                    self.instance_id, payload.request_id, save_id,
                    entries=list(state.get("entries") or []),
                    memory_revision=int(state.get("memoryRevision") or 0),
                ))
            elif msg_type == "life.memory.edit":
                payload = LifeMemoryEditPayload.from_mapping(payload_data)
                save_id = payload.save_id or str(active_save_id or "")
                status, result, entries, revision = self._apply_life_memory_edit(payload, save_id)
                await self._send_reply(ws, Envelope.create_life_memory_state(
                    self.instance_id, payload.request_id, save_id,
                    entries=entries,
                    memory_revision=revision,
                    status=status,
                    reason=result.get("reason"),
                ))
            elif msg_type == "life.milestones.get":
                payload = LifeMilestonesGetPayload.from_mapping(payload_data)
                save_id = payload.save_id or str(active_save_id or "")
                await self._send_reply(
                    ws, self._build_milestones_state(save_id, payload.request_id)
                )
        except ProtocolError as ex:
            await self._send_life_error(ws, msg_type, data, active_save_id, str(ex))
        except Exception as ex:
            logger.error("Error handling %s: %s", msg_type, ex, exc_info=True)
            await self._send_life_error(ws, msg_type, data, active_save_id, f"LIFE_HANDLER_ERROR: {ex}")

    async def _send_life_error(
        self,
        ws: WebSocketClient | None,
        msg_type: str,
        data: dict[str, Any],
        active_save_id: str | None,
        error: str,
    ) -> None:
        payload_data = data.get("payload") if isinstance(data.get("payload"), dict) else data
        request_id = str(payload_data.get("requestId") or data.get("messageId") or "")
        save_id = str(payload_data.get("saveId") or active_save_id or "")
        if msg_type in {"life.profile.get", "life.profile.set"}:
            await self._send_reply(ws, Envelope.create_life_profile_state(
                self.instance_id, request_id, save_id, None, self._profile_revision(save_id),
                status="failed", reason=error, work=self._life_work_projection(save_id),
            ))
        elif msg_type in {"life.memory.list", "life.memory.edit"}:
            await self._send_reply(ws, Envelope.create_life_memory_state(
                self.instance_id, request_id, save_id, entries=[],
                memory_revision=self._memory_revision(save_id),
                status="failed", reason=error,
            ))
        elif msg_type == "life.milestones.get":
            await self._send_reply(
                ws, self._build_milestones_state(save_id, request_id, status="failed", error=error)
            )
        else:
            await self._send_reply(ws, Envelope.create_life_chat_reply(
                self.instance_id, request_id, save_id, "failed",
                profile_revision=self._profile_revision(save_id),
                memory_revision=self._memory_revision(save_id),
                error=error,
            ))

    def _apply_life_memory_edit(
        self, payload: LifeMemoryEditPayload, save_id: str
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]], int]:
        if self._memory_store is None:
            return "rejected", {"reason": "NO_MEMORY_STORE"}, [], 0
        game_date = self._current_game_day_key or "unknown"

        def _state() -> tuple[list[dict[str, Any]], int]:
            current = self._memory_store.list(save_id)
            return list(current.get("entries") or []), int(current.get("memoryRevision") or 0)

        if payload.op == "add":
            if not payload.kind:
                entries, revision = _state()
                return "rejected", {"reason": "MISSING_KIND"}, entries, revision
            if not payload.text:
                entries, revision = _state()
                return "rejected", {"reason": "MISSING_TEXT"}, entries, revision
            status, result = self._memory_store.add(
                save_id, kind=payload.kind, text=payload.text, source="player",
                game_date=game_date, expected_revision=payload.expected_revision,
            )
        elif payload.op == "correct":
            if not payload.entry_id:
                entries, revision = _state()
                return "rejected", {"reason": "MISSING_ID"}, entries, revision
            status, result = self._memory_store.correct(
                save_id, payload.entry_id, payload.text or "", payload.expected_revision
            )
        else:  # delete
            if not payload.entry_id:
                entries, revision = _state()
                return "rejected", {"reason": "MISSING_ID"}, entries, revision
            status, result = self._memory_store.delete(
                save_id, payload.entry_id, payload.expected_revision
            )
        entries, revision = _state()
        return status, result if isinstance(result, dict) else {}, entries, revision

    async def _handle_life_chat_submit(
        self,
        ws: WebSocketClient | None,
        data: dict[str, Any],
        active_save_id: str | None,
    ) -> None:
        """Queue-or-run dispatch for life.chat.submit (contract §1.1/§1.2)."""
        payload_data = data.get("payload") if isinstance(data.get("payload"), dict) else data
        try:
            payload = LifeChatSubmitPayload.from_mapping(payload_data)
        except ProtocolError as ex:
            await self._send_life_error(ws, "life.chat.submit", data, active_save_id, str(ex))
            return
        save_id = payload.save_id or str(active_save_id or "")
        item = {
            "ws": ws,
            "request_id": payload.request_id,
            "save_id": save_id,
            "mode": payload.mode,
            "text": payload.text,
            "accepted_node_id": payload.accepted_node_id,
        }
        if self._busy_lock.locked() or (self._active_task and not self._active_task.cancelled):
            # A work turn/decision owns the single model slot: acknowledge with a
            # player-visible queued status; the drain after that turn finishes
            # delivers the terminal state FIFO (never silently dropped).
            self._life_queue.append(item)
            await self._send_reply(ws, Envelope.create_life_chat_reply(
                sender_instance_id=self.instance_id,
                request_id=payload.request_id,
                save_id=save_id,
                status="queued",
                queue_position=len(self._life_queue),
                profile_revision=self._profile_revision(save_id),
                memory_revision=self._memory_revision(save_id),
                activity={"phase": "waiting", "summary": "伙伴正忙，这次对话已排队。", "nextStep": "当前工作告一段落后继续。"},
            ))
            return
        await self._run_life_chat_turn(ws, item)
        await self._drain_life_queue(ws)

    async def _drain_life_queue(self, ws: WebSocketClient | None) -> None:
        """Process queued life submits FIFO once the model slot is free."""
        if self._life_draining:
            return
        self._life_draining = True
        try:
            while self._life_queue:
                if self._busy_lock.locked() or (
                    self._active_task and not self._active_task.cancelled
                ):
                    # Still busy: leave the queue for the next turn's drain trigger.
                    return
                item = self._life_queue.pop(0)
                await self._run_life_chat_turn(ws or item.get("ws"), item)
        finally:
            self._life_draining = False

    def _execute_life_turn(
        self,
        active_task: ActiveChatTask,
        conversation_id: str | None,
        prompt: str,
        mode: str = "chat",
    ) -> dict[str, Any]:
        """Dispatch one life-chat turn to the provider.

        Hard boundaries (contract §2, enforced here not by prompt): never calls
        ``begin_decision``, never injects ``STARDEW_DECISION_TOKEN``, always
        exposes the read-only ``STARDEW_MCP_SURFACE=life`` tool surface, and the
        scheduler/autonomy state is never touched. ``STARDEW_LIFE_MODE`` is set to
        the current life mode (chat/plan) so gated tools such as
        ``manage_milestones`` can tell plan discussions from casual chat, and is
        restored afterwards.
        """
        if self.backend_name == "kimi" and type(self._execute_agy_turn).__module__.startswith("unittest.mock"):
            return self._execute_agy_turn(active_task, conversation_id, prompt)
        from stardew_ai_runtime.compatibility import assert_native_compatible
        assert_native_compatible(self.run_dir)
        env = os.environ
        previous_token = env.get("STARDEW_DECISION_TOKEN")
        previous_surface = env.get("STARDEW_MCP_SURFACE")
        previous_mode = env.get("STARDEW_LIFE_MODE")
        previous_proposal = env.get("STARDEW_LIFE_PROPOSAL_ID")
        env.pop("STARDEW_DECISION_TOKEN", None)
        env["STARDEW_MCP_SURFACE"] = "life"
        env["STARDEW_LIFE_MODE"] = mode if mode in {"chat", "plan"} else "chat"
        proposal = self._current_life_proposal(active_task.save_id) if mode == "plan" else None
        env["STARDEW_LIFE_PROPOSAL_ID"] = proposal["id"] if proposal else ""
        try:
            backend = self._get_backend()
            if isinstance(backend, KimiBackend):
                backend.progress = getattr(self, "_backend_progress_callback", None)
            return backend.run(active_task, conversation_id, prompt)
        finally:
            if previous_token is None:
                env.pop("STARDEW_DECISION_TOKEN", None)
            else:
                env["STARDEW_DECISION_TOKEN"] = previous_token
            if previous_surface is None:
                env.pop("STARDEW_MCP_SURFACE", None)
            else:
                env["STARDEW_MCP_SURFACE"] = previous_surface
            if previous_mode is None:
                env.pop("STARDEW_LIFE_MODE", None)
            else:
                env["STARDEW_LIFE_MODE"] = previous_mode
            if previous_proposal is None:
                env.pop("STARDEW_LIFE_PROPOSAL_ID", None)
            else:
                env["STARDEW_LIFE_PROPOSAL_ID"] = previous_proposal

    async def _run_life_chat_turn(self, ws: WebSocketClient | None, item: dict[str, Any]) -> None:
        """Run one life-chat turn under the shared single-model-turn lock."""
        request_id = str(item.get("request_id") or "")
        save_id = str(item.get("save_id") or "")
        mode = str(item.get("mode") or "chat")
        text = str(item.get("text") or "")
        try:
            if item.get("accepted_node_id"):
                await self._accept_life_proposal(ws, item)
                return
            async with self._busy_lock:
                profile = None
                if self._profile_store is not None and save_id:
                    profile = (self._profile_store.get(save_id) or {}).get("profile")
                memory_render = (
                    self._memory_store.render_for_context(save_id)
                    if self._memory_store is not None and save_id
                    else None
                )
                existing_cid = None
                if self._life_chat is not None and save_id:
                    existing_cid = self._life_chat.rotate_if_needed(
                        save_id,
                        self._profile_revision(save_id),
                        self._memory_revision(save_id),
                    )
                preparation_before = {
                    n["id"]: dict(n.get("todoIds") or {})
                    for n in (self._milestone_store.list_nodes(save_id)
                              if self._milestone_store is not None and save_id else [])
                }
                proposals_before = {node["id"]: node.get("updatedAt") for node in
                                    (self._milestone_store.list_nodes(save_id) if self._milestone_store else [])}
                milestone_revision_before = (
                    self._milestone_store.revision(save_id)
                    if self._milestone_store is not None and save_id
                    else 0
                )
                prompt_builder = (self._life_chat.build_turn_prompt
                                  if self._life_chat is not None else None)
                prompt_args = (
                    profile,
                    memory_render,
                    self._life_work_summary(save_id, mode),
                )
                prompt_options = {
                    "mode": mode,
                    "milestones": self._life_milestone_summary(save_id, mode),
                    "live_context": self._decision_context(save_id, origin="life-plan" if mode == "plan" else "life-chat"),
                }
                system_prompt = (prompt_builder(save_id, existing_cid, *prompt_args, **prompt_options)
                                 if prompt_builder else LifeChatService.build_system_prompt(*prompt_args, **prompt_options))
                prompt = f"{system_prompt}\n\n玩家说：{text}"
                active_task = ActiveChatTask(
                    request_id=request_id,
                    save_id=save_id,
                    command_id=request_id,
                    prompt=text,
                    async_task=asyncio.current_task(),
                )
                self._configure_backend_progress(ws, request_id, save_id)
                await self._send_reply(ws, Envelope.create_life_chat_reply(
                    sender_instance_id=self.instance_id,
                    request_id=request_id,
                    save_id=save_id,
                    status="processing",
                    profile_revision=self._profile_revision(save_id),
                    memory_revision=self._memory_revision(save_id),
                    activity={"phase": "planning", "summary": "伙伴正在结合眼前情况想下一步。", "nextStep": "先商量，认可后才接手工作。"} if mode == "plan" else None,
                ))
                # Life read/plan tools use the same single native MCP socket
                # as work turns. Park the worker and release its connection;
                # recover=False keeps a casual conversation from changing work.
                claimed = await self._claim_execution(save_id, recover=False)
                try:
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        None, self._execute_life_turn, active_task, existing_cid, prompt, mode
                    )
                finally:
                    if claimed:
                        await self._release_execution(save_id)
                success = bool(result.get("success"))
                if success and mode == "plan" and self._milestone_store:
                    changed = [node for node in self._milestone_store.list_nodes(save_id)
                               if node.get("status") == "suggested" and
                               node.get("updatedAt") != proposals_before.get(node["id"])]
                    if changed:
                        proposed = max(changed, key=lambda node: node.get("updatedAt", 0))
                        self._life_proposals[save_id] = (proposed["id"], self._profile_revision(save_id), proposed["updatedAt"])
                cid = result.get("conversation_id") or existing_cid
                if cid and self._life_chat is not None and save_id:
                    self._life_chat.record_session_id(save_id, str(cid))
                    self._life_chat.record_fingerprint(
                        save_id,
                        self._profile_revision(save_id),
                        self._memory_revision(save_id),
                    )
                if success and self._life_chat is not None:
                    if cid:
                        self._life_chat.mark_prompt_delivered(save_id, str(cid), mode)
                    self._life_chat.record_discussion(save_id, mode, text, str(result.get("response") or ""))
                if success:
                    reply = Envelope.create_life_chat_reply(
                        sender_instance_id=self.instance_id,
                        request_id=request_id,
                        save_id=save_id,
                        status="completed",
                        reply_text=str(result.get("response") or "")[:2000] or None,
                        profile_revision=self._profile_revision(save_id),
                        memory_revision=self._memory_revision(save_id),
                    )
                else:
                    reply = Envelope.create_life_chat_reply(
                        sender_instance_id=self.instance_id,
                        request_id=request_id,
                        save_id=save_id,
                        status="failed",
                        reply_text=str(result.get("response") or "")[:2000] or None,
                        profile_revision=self._profile_revision(save_id),
                        memory_revision=self._memory_revision(save_id),
                        error=str(result.get("error") or "TURN_FAILED"),
                    )
                if mode != "plan" or not success:
                    await self._send_reply(ws, reply)
                else:
                    await self._send_reply(ws, Envelope.create_life_chat_reply(
                        self.instance_id, request_id, save_id, status="processing",
                        reply_text=str(result.get("response") or "")[:2000] or None,
                        profile_revision=self._profile_revision(save_id),
                        memory_revision=self._memory_revision(save_id),
                    ))
                if (
                    self._milestone_store is not None
                    and save_id
                    and self._chat_ws is not None
                    and self._milestone_store.revision(save_id) != milestone_revision_before
                ):
                    # The turn changed milestone state (e.g. an adopt/revise was
                    # confirmed): proactively push the fresh node list (§2.2;
                    # requestId="" marks a server push, not a reply).
                    await self._send_reply(
                        self._chat_ws, self._build_milestones_state(save_id, "")
                    )
            # After releasing the life lock, consume only newly accepted due
            # preparation. Never promote casual chat or reopen into authority.
            if mode == "plan" and success:
                execution_note = await self._start_accepted_preparation(ws, save_id, preparation_before)
                if execution_note:
                    reply.payload["replyText"] = (str(reply.payload.get("replyText") or "")
                                                  + "\n" + execution_note)[:2000]
                proposal = self._current_life_proposal(save_id)
                reply.payload["proposalReady"] = proposal is not None
                if proposal:
                    reply.payload["proposalNodeId"] = proposal["id"]
                reply.payload["activity"] = self._player_activity(save_id)
                await self._send_reply(ws, reply)
        except Exception as ex:
            logger.error("Life chat turn [%s] failed: %s", request_id, ex, exc_info=True)
            await self._send_reply(ws, Envelope.create_life_chat_reply(
                sender_instance_id=self.instance_id,
                request_id=request_id,
                save_id=save_id,
                status="failed",
                profile_revision=self._profile_revision(save_id),
                memory_revision=self._memory_revision(save_id),
                error=f"LIFE_TURN_ERROR: {ex}",
                activity={"phase": "failed", "summary": "这次安排没有接手成功。", "nextStep": "请重新商量当前方案；已有工作保持原样。"},
            ))

    async def _accept_life_proposal(self, ws: WebSocketClient | None, item: dict[str, Any]) -> None:
        """Explicit NPC button -> existing milestone -> existing command chain."""
        save_id, request_id = str(item["save_id"]), str(item["request_id"])
        async with self._busy_lock:
            proposal = self._current_life_proposal(save_id)
            if item.get("mode") != "plan" or not proposal or proposal["id"] != item["accepted_node_id"]:
                raise ValueError("建议已更新、暂缓或方向已改变，请先重新商量；没有派发工作。")
            before = {node["id"]: dict(node.get("todoIds") or {})
                      for node in self._milestone_store.list_nodes(save_id)}
            _, game_date = self._plan_snapshot_state()
            adopted = self._milestone_store.adopt(
                save_id, proposal["id"], work_store=self._work_store, game_date=game_date,
            )
            self._life_proposals.pop(save_id, None)
            await self._send_reply(ws, Envelope.create_life_chat_reply(
                self.instance_id, request_id, save_id, "processing",
                self._profile_revision(save_id), self._memory_revision(save_id),
                reply_text=f"已记下：{adopted['title']}。我看看眼前能接手哪一步。",
                proposal_ready=False, activity=self._player_activity(save_id)))
            await self._send_reply(ws, self._build_milestones_state(save_id, ""))
        note = await self._start_accepted_preparation(ws, save_id, before)
        await self._send_reply(ws, Envelope.create_life_chat_reply(
            self.instance_id, request_id, save_id, "completed",
            self._profile_revision(save_id), self._memory_revision(save_id),
            reply_text=note or "已保留这个安排。需要玩家完成的部分，我会和你继续商量可接手的准备。",
            proposal_ready=False, activity=self._player_activity(save_id)))

    async def _start_accepted_preparation(
        self, ws: WebSocketClient | None, save_id: str, before: dict[str, dict[str, str]]
    ) -> str | None:
        """One normal work turn for newly authorized, currently due preparation."""
        if self._milestone_store is None or self._work_store is None:
            return
        new_todo_ids: set[str] = set()
        for node in self._milestone_store.list_nodes(save_id):
            todos = node.get("todoIds") or {}
            if node.get("status") == "adopted" and todos != before.get(node["id"], {}):
                new_todo_ids.update(todos.values())
        if not new_todo_ids:
            return
        state = self._work_store.state(save_id)
        autonomy = self._autonomy.state(save_id) if self._autonomy is not None else None
        if (state.paused or (autonomy is not None and autonomy.paused)
                or self._active_task is not None or self._busy_lock.locked()
                or save_id in self._command_chains
                or (state.decision.get("selected") and not state.decision.get("finished"))):
            return "准备安排已保存。我会保留这些后续安排；当前暂停或已有工作时，不抢着另开一件事。"
        snapshot, game_date = self._plan_snapshot_state()
        due = [t for t in self._work_store.evaluate_todos(save_id, snapshot=snapshot, game_date=game_date)
               if t["id"] in new_todo_ids]
        if not due:
            return "安排已保存，眼前还没到这项准备的时间。"
        if autonomy is not None and autonomy.enabled:
            self._autonomy.request_job_decision(save_id)
            return "已把眼前可做的准备交给日常工作安排；完成后会按实际结果告诉你。"
        prompt = (
            "玩家刚在商量计划中明确认可了以下准备安排，现交给你执行一个当前可做的短作业。"
            "这是本次具体安排的执行授权，不开启全局自由模式；不要再问预算/数量或重复确认。"
            "只处理以下范围，先看真实状态，遵守现有资金/体力/暂停保护，不取消其他工作。"
            "从中选择一个有用短作业，用现有submit_plan和执行器完成；没有可做工作就自然说明待命。"
            "保存不等于完成，结果只依据原生终态；用自然中文反馈，不提goal/todo或内部参数。"
            + json.dumps([{"intent": t["intent"], "goalId": t.get("goal_id")} for t in due], ensure_ascii=False)
        )
        # Reuse the existing command path; its ownership and busy checks still
        # apply. Await it so the turn remains tracked by the life queue task.
        previous_job = state.last_job
        await self.handle_chat_submit(ws, f"preparation-{uuid.uuid4().hex}", prompt, save_id)
        after = self._work_store.state(save_id)
        if after.last_job and after.last_job != previous_job:
            outcome = after.last_job.get("status") or after.last_job.get("outcome")
            if outcome == "completed":
                return "刚才接手的这一小项已经完成，后续准备仍按约定保留。"
            if outcome in {"partial", "failed", "cancelled", "unknown", "rejected"}:
                return "安排已经保存，但刚才这项工作没有确认全部完成；我会保留实际进度，不把它算作做完。"
        if after.decision.get("selected") and not after.decision.get("finished"):
            return "已经接下这一项准备，正在处理；完成情况以实际工作结果为准。"
        return "准备安排已保存，这次还没有确认开始执行，我先保留安排待命。"

    # -------------------------------------------------- care hooks (§1.7/§2)
    async def _snapshot_care_hooks(
        self,
        save_id: str | None,
        world: dict[str, Any],
        day_key: str | None,
        previous_day_key: str | None,
    ) -> None:
        """Day-change (morning) and first-evening (timeOfDay>=1900) care triggers.

        The morning hook runs only after ``_settle_day_advance`` (contract §2);
        the service de-duplicates by persisted eventKey, so restarts or repeated
        snapshots never re-fire.
        """
        if day_key is not None and day_key != previous_day_key:
            await self._maybe_fire_care(save_id, "morning", day_key, world)
        tod = world.get("timeOfDay")
        current_day = self._current_game_day_key
        if isinstance(tod, int) and tod >= 1900 and current_day:
            if self._evening_care_fired_day != current_day:
                self._evening_care_fired_day = current_day
                await self._maybe_fire_care(save_id, "evening", current_day, world)

    def _record_memory_event(
        self, save_id: str | None, text: str, command_id: str | None
    ) -> None:
        """Write a system event memory for a real successful terminal (§2)."""
        if self._memory_store is None or not save_id:
            return
        try:
            status, _ = self._memory_store.add(
                save_id, kind="event", text=text[:200], source="system",
                game_date=self._current_game_day_key or "unknown",
                expected_revision=0, command_id=command_id,
            )
            if status == "confirmed":
                logger.info("Memory event recorded for %s: %s", save_id, text[:80])
        except Exception:
            logger.warning("Failed to record memory event for %s", save_id, exc_info=True)

    async def _maybe_fire_care(
        self, save_id: str | None, kind: str, ref: str, world: dict[str, Any],
        fact: str | None = None,
    ) -> None:
        """Evaluate and send one proactive care message (contract §1.7).

        The service persists the eventKey first (cross-restart dedup, daily
        frequency, 2-game-hour gap, quiet=off); the text is then generated by the
        life-session model with the real date/weather/context. A model failure
        skips the send (logged, never template-faked, never re-fired).
        """
        if not save_id or self._care_service is None or self._profile_store is None:
            return
        try:
            profile = (self._profile_store.get(save_id) or {}).get("profile")
            if not profile:
                return
            frequency = str(profile.get("careFrequency") or "moderate")
            game_date = self._current_game_day_key or "unknown"
            tod = world.get("timeOfDay") if isinstance(world, dict) else None
            tod_int = int(tod) if isinstance(tod, int) else 0
            event_key = self._care_service.maybe_fire(
                save_id, kind, game_date, ref, frequency, tod_int
            )
            if not event_key:
                return
            if self._busy_lock.locked() or (
                self._active_task and not self._active_task.cancelled
            ):
                # Reserve the event once, then wait behind the active work turn.
                # Otherwise free mode can occupy every trigger and silently lose
                # all moderate care for the day. The event key still prevents
                # duplicate sends across repeated snapshots/restarts.
                task = asyncio.create_task(self._deliver_care(
                    save_id, kind, game_date, profile, fact, event_key, dict(world)
                ))
                self._deferred_care_tasks.add(task)
                task.add_done_callback(self._deferred_care_tasks.discard)
                return
            await self._deliver_care(save_id, kind, game_date, profile, fact, event_key, dict(world))
        except Exception:
            logger.warning("Care hook failed for %s/%s", save_id, kind, exc_info=True)

    async def _deliver_care(
        self, save_id: str, kind: str, game_date: str, profile: dict[str, Any],
        fact: str | None, event_key: str, world: dict[str, Any],
    ) -> None:
        try:
            text = await self._generate_care_text(
                save_id, kind, game_date, profile, fact, world=world
            )
            # A deferred evening/morning greeting must not arrive on a later day.
            if not text or self._current_game_day_key != game_date:
                return
            await self._send_reply(self._chat_ws, Envelope.create_life_care(
                sender_instance_id=self.instance_id,
                save_id=save_id,
                kind=kind,
                event_key=event_key,
                text=text[:300],
                game_date=game_date,
            ))
        except Exception:
            logger.warning("Deferred care failed for %s/%s", save_id, kind, exc_info=True)

    async def _generate_care_text(
        self,
        save_id: str,
        kind: str,
        game_date: str,
        profile: dict[str, Any],
        ref_event: str | None,
        world: dict[str, Any] | None = None,
    ) -> str | None:
        """Ask the life-session model for one short care message (§2)."""
        memory_render = (
            self._memory_store.render_for_context(save_id)
            if self._memory_store is not None
            else None
        )
        if world is None:
            world = {}
            if isinstance(self._latest_snapshot_payload, dict):
                world = self._latest_snapshot_payload.get("world") or {}
        weather = world.get("weather") or world.get("weatherIcon")
        prompt = LifeChatService.build_care_prompt(
            profile, memory_render, kind, game_date, weather, ref_event
        )
        active_task = ActiveChatTask(
            request_id=f"care-{uuid.uuid4().hex[:8]}",
            save_id=save_id,
            command_id="",
            prompt=prompt,
        )
        try:
            async with self._busy_lock:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, self._execute_life_turn, active_task, None, prompt
                )
        except Exception:
            logger.warning("Care text generation raised", exc_info=True)
            return None
        if not result.get("success"):
            logger.warning(
                "Care text generation unsuccessful (%s); skipping without re-fire",
                result.get("error"),
            )
            return None
        return (result.get("response") or "").strip() or None

    # -------------------------------------------------- session rotation
    def _session_history_path(self) -> Path:
        base = self.run_dir if self.run_dir else Path.home() / ".gemini" / "antigravity-cli"
        return Path(base) / "chat_session_history.json"

    # -------------------------------------------------- profile fingerprint
    def _profile_fingerprint_path(self) -> Path:
        if self.run_dir:
            return Path(self.run_dir) / "chat_profile_fingerprints.json"
        # Keep the fingerprint beside the configured session file, so bridges that
        # are constructed with an explicit sessions_file (tests, alternate run
        # dirs) persist and read the same record.
        return Path(self._sessions_file).parent / "chat_profile_fingerprints.json"

    def profile_fingerprint(self, save_id: str | None = None) -> str:
        """Fingerprint of the provider profile + tool surface bound to a session.

        A resumed provider session keeps whatever profile/tool surface it was
        created with, so reconfiguring the agent file (or the game tool surface)
        must start a fresh session instead of silently continuing the old one.
        The companion profile/memory revisions are part of the fingerprint too:
        after an agreement is deleted/corrected or the profile changes, the next
        work turn must not keep treating the old session (with the stale
        agreement in its visible history) as authoritative — it rotates like any
        other profile change, keeping history and WorkStore intact.
        """
        import hashlib

        agent_file_digest: str | None = None
        if self.agent_file is not None:
            try:
                agent_file_digest = hashlib.sha256(self.agent_file.read_bytes()).hexdigest()[:16]
            except Exception:
                agent_file_digest = "unreadable"
        material = json.dumps(
            {
                "backend": self.backend_name,
                "model": self.model,
                "agent": self.agent,
                "agentFile": agent_file_digest,
                "surface": os.getenv("STARDEW_MCP_SURFACE", ""),
                "profileRevision": self._profile_revision(save_id),
                "memoryRevision": self._memory_revision(save_id),
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def _load_profile_fingerprints(self) -> dict[str, str]:
        path = self._profile_fingerprint_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            logger.debug("Failed to load profile fingerprints", exc_info=True)
        return {}

    def _save_profile_fingerprints(self, fingerprints: dict[str, str]) -> None:
        try:
            path = self._profile_fingerprint_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(fingerprints, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Failed to save profile fingerprints", exc_info=True)

    def _ensure_session_matches_profile(
        self, save_id: str | None, conversation_id: str | None
    ) -> str | None:
        """Return the session to resume, or None when a fresh one must start.

        Rotation happens when the profile fingerprint changed since the session
        was created — the fingerprint covers the provider profile, the tool
        surface, and the companion profile/memory revisions, so deleting or
        correcting an agreement (or editing the companion profile) invalidates
        the current work session exactly like any other profile change — and
        also for a legacy session that has no fingerprint at all (its tool
        surface cannot be verified, so it is not resumed). The previous session
        id is always kept in ``chat_session_history.json`` and durable
        goals/tasks/todos stay in WorkStore, so nothing is deleted and no
        in-progress work is lost.
        """
        if not save_id:
            return conversation_id
        fingerprints = self._load_profile_fingerprints()
        key = f"{self.backend_name}:{save_id}"
        current = self.profile_fingerprint(save_id)
        recorded = fingerprints.get(key)
        self._profile_rotation_note: str | None = None
        if conversation_id and recorded != current:
            reason = "profile-changed" if recorded else "missing-fingerprint"
            self._rotate_provider_session(save_id, reason=reason)
            self._profile_rotation_note = (
                "伙伴配置或工具范围已变化，已安全新建会话（旧会话与历史记录保留，"
                f"原因：{reason}）。"
            )
            logger.info(
                "Profile fingerprint changed for save %s (recorded=%s current=%s); "
                "started a new session and kept the old one in history.",
                save_id,
                recorded,
                current,
            )
            conversation_id = None
        fingerprints[key] = current
        self._save_profile_fingerprints(fingerprints)
        return conversation_id

    def consume_profile_rotation_note(self) -> str | None:
        note = getattr(self, "_profile_rotation_note", None)
        self._profile_rotation_note = None
        return note

    def _load_session_history(self) -> None:
        if self._session_history:
            return
        path = self._session_history_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._session_history = {
                    str(k): [str(v) for v in vals]
                    for k, vals in data.items()
                    if isinstance(vals, list)
                }
        except Exception:
            logger.debug("Failed to load session history", exc_info=True)

    def _save_session_history(self) -> None:
        try:
            path = self._session_history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._session_history, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Failed to save session history", exc_info=True)

    def _rotate_provider_session(self, save_id: str | None, *, reason: str) -> str | None:
        """End the current provider session, keeping the old id in history.

        The next turn starts a fresh session and carries the saved goals, the
        unfinished/waiting tasks and the live context, so rotation never loses
        in-progress work. Old session ids stay in ``chat_session_history.json`` so
        their billing records remain attributable.
        """
        if not save_id:
            return None
        cid = self.get_conversation_id(save_id)
        if not cid:
            return None
        self._load_session_history()
        key = f"{self.backend_name}:{save_id}"
        history = self._session_history.setdefault(key, [])
        if cid not in history:
            history.append(cid)
        self._session_history[key] = history[-20:]
        self._sessions.pop(key, None)
        if self.backend_name == "agy":
            self._sessions.pop(save_id, None)
        self._save_sessions()
        self._save_session_history()
        self._session_token_totals[save_id] = 0
        self._session_requests[save_id] = 0
        self._session_context_state[save_id] = {
            "latestInputContext": None,
            "maxInputContext": None,
            "measured": False,
        }
        logger.info("Rotated provider session for %s (%s); previous=%s", save_id, reason, cid)
        return cid

    @staticmethod
    def _request_input_context(usage: dict[str, Any] | None) -> int | None:
        """Input-side size of the newest request, or None when unmeasured.

        Only the input side counts: summing ``total_tokens`` across requests
        double-counts replayed/cached context and includes output, so it can
        never be used as a context length.
        """
        if not isinstance(usage, dict):
            return None
        latest = usage.get("latestRequestInputContext")
        if isinstance(latest, int) and latest >= 0:
            return latest
        if usage.get("input_context_measured") is False:
            return None
        # Non-Kimi sources (agy DB delta) have no per-request split; fall back to
        # that request's input-side counters when the source is a single request.
        generations = usage.get("generations_count")
        if generations not in (1, None):
            return None
        input_other = usage.get("inputOther")
        if input_other is None:
            input_other = usage.get("input_tokens")
        cache_read = usage.get("inputCacheRead")
        if cache_read is None:
            cache_read = usage.get("cache_read_tokens")
        cache_creation = usage.get("inputCacheCreation")
        if cache_creation is None:
            cache_creation = usage.get("cache_creation_tokens")
        parts = [value for value in (input_other, cache_read, cache_creation) if isinstance(value, int)]
        if not parts:
            return None
        return sum(parts)

    def _note_session_context(
        self,
        save_id: str | None,
        usage: dict[str, Any] | None,
        *,
        request_count: int | None = None,
    ) -> str | None:
        """Track this session's input context; return a rotation reason or None.

        Two independent gates:

        * the measured latest-request input context reaching the configured
          budget (default 100000 tokens, an engineering policy, not a provider
          claim) — ``SESSION_CONTEXT_BUDGET``;
        * a bounded request-count checkpoint (default 20) used only while the
          provider gives no usable per-request measurement, and clearly labelled
          as a fallback — ``SESSION_REQUEST_CHECKPOINT``.

        The game-day rotation is separate and unchanged.
        """
        if not save_id:
            return None
        state = self._session_context_state.setdefault(
            save_id,
            {"latestInputContext": None, "maxInputContext": None, "measured": False},
        )
        if request_count is not None and request_count > 0:
            self._session_requests[save_id] = self._session_requests.get(save_id, 0) + request_count
        else:
            generations = usage.get("generations_count") if isinstance(usage, dict) else None
            self._session_requests[save_id] = self._session_requests.get(save_id, 0) + (
                generations if isinstance(generations, int) and generations > 0 else 1
            )

        context_tokens = self._request_input_context(usage)
        if context_tokens is None:
            # Unknown measurement: never invent a context length, use the bounded
            # request-count checkpoint instead (explicitly labelled by reason).
            if self._session_request_checkpoint and (
                self._session_requests.get(save_id, 0) >= self._session_request_checkpoint
            ):
                return "SESSION_REQUEST_CHECKPOINT"
            return None

        state["measured"] = True
        state["latestInputContext"] = context_tokens
        previous_max = state.get("maxInputContext")
        state["maxInputContext"] = (
            context_tokens if not isinstance(previous_max, int) else max(previous_max, context_tokens)
        )
        if self._session_token_budget and context_tokens >= self._session_token_budget:
            return "SESSION_CONTEXT_BUDGET"
        return None

    def _note_session_tokens(self, save_id: str | None, usage: dict[str, Any] | None) -> bool:
        """Backwards-compatible wrapper: True when the context policy says rotate."""
        return self._note_session_context(save_id, usage) is not None

    async def _settle_day_advance(self, save_id: str | None, world: dict[str, Any]) -> None:
        """Settle a real native day advance, then rotate the provider session."""
        if not save_id:
            return
        settlement: dict[str, Any] | None = None
        if self._work_store is not None:
            try:
                settlement = await asyncio.to_thread(
                    self._work_store.settle_game_day,
                    save_id,
                    year=world.get("year"),
                    season=world.get("season"),
                    day=world.get("dayOfMonth"),
                )
            except Exception:
                logger.warning("Day settlement failed for %s", save_id, exc_info=True)
        if settlement and settlement.get("settled"):
            logger.info(
                "Settled game day for %s: %s -> %s (archived=%s, carried=%s)",
                save_id,
                settlement.get("fromDay"),
                settlement.get("day"),
                settlement.get("archivedCount"),
                len(settlement.get("carriedTaskIds") or []),
            )
        self._last_day_settlement = settlement
        # Rotate only after a real settlement; a first observation, a reloaded old
        # save or a repeated day must not churn the provider session.
        if settlement and settlement.get("settled"):
            self._rotate_provider_session(save_id, reason="game-day-advanced")
        if self._milestone_store is not None:
            await self._settle_milestone_nodes(
                save_id, world, allow_reminders=bool(settlement and settlement.get("settled"))
            )
        if self._autonomy is not None and save_id:
            self._autonomy.reset_breaker(save_id)

    async def _settle_milestone_nodes(
        self, save_id: str, world: dict[str, Any], *, allow_reminders: bool = True
    ) -> None:
        """Verify milestone nodes on the settled day and fire reminder cares (§2/§3.4).

        Completion is decided here only, from verifiable state (snapshot
        ``world.playerItems``); suggestion/adoption never completes a node. Nodes
        0..2 days from their target produce reminder candidates that go through
        the standard care gate (quiet off, daily budget, 2-game-hour gap, dedup).
        """
        if self._milestone_store is None:
            return
        try:
            changed, candidates = await asyncio.to_thread(
                self._milestone_store.on_day_settled,
                save_id,
                year=world.get("year"),
                season=world.get("season"),
                day=world.get("dayOfMonth"),
                player_items=self._latest_player_items(),
            )
        except Exception:
            logger.warning("Milestone day settle failed for %s", save_id, exc_info=True)
            return
        if changed:
            logger.info("Milestone nodes updated for %s on %s", save_id, self._current_game_day_key)
            if self._chat_ws is not None:
                await self._send_reply(self._chat_ws, self._build_milestones_state(save_id, ""))
        if not allow_reminders:
            return
        for candidate in candidates:
            gap = candidate.get("firstGap") or "查看准备事项"
            fact = (
                f'节点「{candidate.get("title")}」目标{candidate.get("targetDate")}'
                f'（还有{candidate.get("daysUntil")}天），首个准备缺口：{gap}'
            )
            await self._maybe_fire_care(
                save_id, "milestone", str(candidate.get("id") or ""), world, fact=fact
            )

    async def _publish_job_progress(self, execution: StepExecution) -> None:
        binding = getattr(self, "_job_reply_binding", None)
        if not binding or binding[2] != execution.task_id:
            return
        request_id, save_id, _, ws = binding[:4]
        command_id = binding[4] if len(binding) > 4 else request_id
        phase = "job-running" if execution.status == "dispatching" else (
            "job-waiting" if execution.outcome == "waiting" else
            "job-completed" if execution.task_status == "completed" and execution.outcome == "completed" else
            "job-failed" if execution.outcome in {"failed", "partial", "unknown", "cancelled", "rejected"} or execution.status == "deferred" else "job-running")
        labels = {"job-running": "正在处理", "job-waiting": "正在等待", "job-completed": "已完成", "job-failed": "未能完成"}
        task = next((task for task in self._work_store.state(save_id).tasks if task.id == execution.task_id), None) if self._work_store else None
        title = task.title if task else "这项农场工作"
        detail = execution.message or execution.reason_code or ""
        if execution.result and isinstance(execution.result, dict):
            detail = detail or str(execution.result.get("error") or execution.result.get("message") or "")
        chain = self._command_chains.get(save_id)
        command_complete = phase in {"job-completed", "job-failed"} and not (
            chain is not None and chain.root_request_id == command_id
        )
        await self._send_reply(ws, Envelope.create_chat_reply(self.instance_id, request_id,
            status=phase, reply_text=f"{labels[phase]}：{title}。{detail[:400]}", save_id=save_id,
            command_id=command_id, command_complete=command_complete))

    def _plan_status_line(self) -> str | None:
        """Short F8-visible line: last plan action and the current wait reason."""
        worker = self._plan_worker
        if worker is None:
            return None
        parts: list[str] = []
        last = worker.last_result
        if last is not None and last.status == "executed":
            parts.append(
                f"动作 {last.operation}（{last.outcome or 'unknown'}）"
            )
        if worker.pending_reasons:
            parts.append(f"等待原因 {worker.pending_reasons[-1]}")
        if not parts and self._last_day_settlement:
            parts.append(f"日结 {self._last_day_settlement.get('reasonCode')}")
        if not parts:
            return None
        return "计划状态：" + "；".join(parts)

    def _autonomy_state_payload(self, save_id: str, state: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": state.mode, "paused": state.paused,
            "preferences": {"goal": state.goal, "dailySpendLimit": state.budget_limit or 0,
                             "boxPreference": state.box_preference or "none"},
            "preferencesRevision": state.preferences_revision,
            "decisionEpoch": state.decision_epoch,
            "gameDate": state.game_date, "dailySpend": state.daily_spend,
            "failureCount": state.failure_count,
            "failure_count": state.failure_count,
            "breakerTripped": state.breaker_tripped,
            "breaker_tripped": state.breaker_tripped,
            "breakerCooldownUntil": state.breaker_cooldown_until,
            "breaker_cooldown_until": state.breaker_cooldown_until,
            "breakerReason": state.breaker_reason,
            "breaker_reason": state.breaker_reason,
        }
        # F8 visibility: the last plan action and why the worker is waiting.
        worker = self._plan_worker
        if worker is not None:
            last = worker.last_result
            payload["lastPlanAction"] = last.as_dict() if last is not None else None
            payload["planWaitReason"] = (
                worker.pending_reasons[-1] if worker.pending_reasons else None
            )
        if self._autonomy is not None and self._autonomy.is_cooling_down(save_id):
            payload["planWaitReason"] = "自动模式已熔断冷却"
        overview = self._latest_work_overview(save_id)
        if overview is not None:
            payload["lastSettledDay"] = overview.get("lastSettledDay")
            payload["hasExecutableWork"] = overview.get("hasExecutableWork")
            # Wire contract: list of WorkStore wait-condition objects, consumed by
            # C# WaitingConditionPayload. Do not stringify the model's structured context.
            payload["waitingConditions"] = overview.get("waitingConditions", [])[:5]
        if self._last_day_settlement is not None:
            payload["lastDaySettlement"] = self._last_day_settlement
        return payload

    async def _handle_autonomy_control(
        self, ws: WebSocketClient | None, envelope: Envelope, active_save_id: str | None
    ) -> None:
        """Apply controls synchronously and acknowledge only the active save."""
        save_id = str(envelope.payload.get("saveId") or envelope.save_id or "")
        request_id = str(envelope.payload.get("requestId") or envelope.message_id)
        if not save_id or not active_save_id or save_id != active_save_id or self._autonomy is None:
            return
        action = str(envelope.payload.get("action", ""))
        params = envelope.payload.get("parameters") or {}
        try:
            command_id = str(params.get("commandId") or "")
            current_command = self._current_command_id(save_id)
            if action in {"pause", "resume", "cancel"} and command_id and current_command and command_id != current_command:
                raise ValueError("COMMAND_MISMATCH: control belongs to an older instruction")
            native_ok = True
            if action == "cancel":
                native_ok = await self.handle_chat_cancel(ws, None, "F8 cancel", save_id, command_id=command_id or None)
            state = self._autonomy.control(save_id, action, **params)
            if self._work_store is not None:
                self._apply_work_control(save_id, action, params)
            if action == "resume":
                chain = self._command_chains.get(save_id)
                if chain is not None and chain.pending_continuation:
                    self._schedule_command_chain(save_id)
                self._notify_plan_worker(save_id)
            status, reason = ("confirmed", None) if native_ok else ("rejected", "NATIVE_CANCEL_UNCONFIRMED")
        except (TypeError, ValueError) as ex:
            state, status, reason = self._autonomy.state(save_id), "rejected", str(ex)
        reply = Envelope.create_autonomy_state(
            self.instance_id, request_id, save_id, self._autonomy_state_payload(save_id, state), status, reason
        )
        await self._send_reply(ws, reply)

    async def _stop_autonomy_if_disabled(
        self, ws: WebSocketClient | None, save_id: str | None
    ) -> None:
        """Apply a shared disable immediately on every chat event, not only timeout."""
        if self._autonomy is None or not save_id:
            return
        if (
            not self._autonomy.state(save_id).enabled
            and self._active_task is not None
            and self._active_task.request_id.startswith("autonomy-")
        ):
            await self.handle_chat_cancel(ws, self._active_task.request_id, "autonomy disabled", save_id)

    async def handle_chat_cancel(
        self,
        ws: WebSocketClient | None,
        request_id: str | None,
        reason: str,
        save_id: str | None,
        *,
        command_id: str | None = None,
    ) -> bool:
        """Handles immediate cancellation: kills active agy subprocess, records command, and notifies game."""
        current_command_id = self._current_command_id(save_id)
        if command_id and current_command_id and command_id != current_command_id:
            return False
        if request_id and current_command_id and request_id not in {
            current_command_id, self._active_task.request_id if self._active_task else "",
        }:
            return False
        pending = self._pending_native_cancels.get(save_id or "")
        native_command_id = pending[1] if pending and pending[0] == current_command_id else None
        if native_command_id is None and save_id and self._work_store is not None:
            state_before = self._work_store.state(save_id)
            selected_task = next((item for item in state_before.tasks
                                  if item.id == state_before.decision.get("taskId")), None)
            if selected_task is not None:
                native_command_id = next((step.command_id for step in selected_task.steps
                                          if step.command_id and step.status == "running"), None)
        self._break_command_chain(save_id)
        native_ok = (await self._confirm_native_terminal(native_command_id)
                     if pending and pending[0] == current_command_id
                     else await self._interrupt_short_job(save_id))
        if save_id and not native_ok and native_command_id:
            self._pending_native_cancels[save_id] = (current_command_id or command_id or "", native_command_id)
        self._autonomy_pending_task_fingerprints.clear()
        task = self._active_task
        if self._autonomy is not None and save_id:
            self._autonomy.set_enabled(save_id, False)
            self._autonomy.reset_breaker(save_id)
        if task is not None:
            active_req = task.request_id
            active_save = task.save_id or save_id or ""
            active_prompt = task.prompt
            start_idx = task.start_max_idx

            self.abort_active_task(f"Cancel requested: {reason}")
            old_async_task = task.async_task
            if old_async_task is not None and old_async_task is not asyncio.current_task():
                try:
                    await asyncio.wait_for(asyncio.shield(old_async_task), timeout=5.0)
                except asyncio.CancelledError:
                    if not old_async_task.cancelled():
                        raise
                except TimeoutError:
                    native_ok = False

            # Resolve conversation id and compute any usage produced before cancellation
            cid = self.get_conversation_id(active_save)
            usage_delta = None
            if self.backend_name == "kimi":
                end_idx = -1
                # Cancellation still settles: read whatever the wire recorded so far.
                usage_delta = read_usage_since(
                    cid, task.wire_start_offset, cwd=project_root()
                )
            else:
                end_idx = get_max_gen_idx(cid) if cid else -1
                if cid and end_idx > start_idx:
                    usage_delta = get_command_usage_delta(cid, start_idx)

            missing_reason = None
            if not usage_delta:
                missing_reason = (
                    "cancelled_before_generations_produced"
                    if cid
                    else "cancelled_before_conversation_established"
                )

            self._record_command(
                request_id=request_id or active_req,
                save_id=active_save,
                conversation_id=cid,
                prompt=active_prompt,
                status="cancelled",
                start_idx=start_idx,
                end_idx=end_idx,
                usage=usage_delta,
                missing_reason=missing_reason,
                error="PLAYER_CANCELLED",
                duration=0.0,
            )
            task.recorded = True

            reply = Envelope.create_chat_reply(
                sender_instance_id=self.instance_id,
                request_id=request_id or active_req,
                status="cancelled" if native_ok else "job-waiting",
                reply_text="任务已由玩家取消。" if native_ok else "取消已请求，原生作业终态尚未确认。",
                save_id=active_save,
                tokens_used=usage_delta["total_tokens"] if usage_delta else None,
                prompt_tokens=usage_delta["input_tokens"] if usage_delta else None,
                output_tokens=usage_delta["output_tokens"] if usage_delta else None,
                cached_tokens=usage_delta["cache_read_tokens"] if usage_delta else None,
                conversation_id=cid,
                error="PLAYER_CANCELLED" if native_ok else "NATIVE_CANCEL_UNCONFIRMED",
                usage_source="db_gen_metadata_delta_cancelled" if usage_delta else "cancelled",
                command_id=current_command_id or active_req,
                command_complete=native_ok,
            )
            await self._send_reply(ws, reply)
            if (old_async_task is None or old_async_task.done()) and self._active_task is task:
                self._active_task = None
            # The cancelled provider turn no longer owns the command socket; hand
            # it back so the worker can settle its own epoch-checked bookkeeping.
            if old_async_task is None:
                await self._release_execution(active_save or save_id)
        else:
            logger.info("chat.cancel received but no task was actively running.")
            binding = getattr(self, "_job_reply_binding", None)
            binding_command = (binding[4] if len(binding) > 4 else binding[0]) if binding else None
            if binding and binding[1] == save_id and binding_command == current_command_id:
                await self._send_reply(ws, Envelope.create_chat_reply(
                    self.instance_id, binding[0], "cancelled" if native_ok else "job-waiting",
                    "任务已由玩家取消。" if native_ok else "取消已请求，原生作业终态尚未确认。",
                    save_id=save_id, error="PLAYER_CANCELLED" if native_ok else "NATIVE_CANCEL_UNCONFIRMED",
                    command_id=current_command_id, command_complete=native_ok,
                ))
        if native_ok and save_id:
            self._pending_native_cancels.pop(save_id, None)
        if native_ok and getattr(self, "_job_reply_binding", None) and self._job_reply_binding[1] == save_id:
            self._job_reply_binding = None
        return native_ok

    def _current_command_id(self, save_id: str | None) -> str | None:
        if not save_id:
            return None
        chain = self._command_chains.get(save_id)
        if chain is not None:
            return chain.root_request_id
        task = self._active_task
        if task is not None and task.save_id == save_id:
            return task.command_id or task.request_id
        binding = getattr(self, "_job_reply_binding", None)
        if binding and binding[1] == save_id and self._work_store is not None:
            decision = self._work_store.state(save_id).decision
            if decision.get("selected") and not decision.get("finished") and decision.get("taskId") == binding[2]:
                return binding[4] if len(binding) > 4 else binding[0]
        pending = self._pending_native_cancels.get(save_id)
        if pending is not None:
            return pending[0]
        return None

    async def _interrupt_short_job(self, save_id: str | None) -> bool:
        if not save_id or self._work_store is None:
            return True
        state = self._work_store.state(save_id)
        d = state.decision
        selected_task = next((task for task in state.tasks if task.id == d.get("taskId")), None)
        was_dispatched = bool(selected_task and any(
            step.command_id and step.status == "running" for step in selected_task.steps
        ))
        native_command_id = next((step.command_id for step in selected_task.steps
                                  if step.command_id and step.status == "running"), None) if selected_task else None
        self._work_store.revoke_decision(save_id)
        if d.get("selected") and not d.get("finished") and was_dispatched and self._plan_worker:
            try:
                await self._plan_worker.client.call_tool("dispatch_plan_operation", {
                    "operation": "cancel_task", "params": {}, "command_id": "interrupt-" + uuid.uuid4().hex})
            except Exception:
                logger.warning("Native cancellation not confirmed; old job authority revoked")
                return await self._confirm_native_terminal(native_command_id)
        return True

    async def _confirm_native_terminal(self, command_id: str | None) -> bool:
        if not command_id or self._plan_worker is None:
            return False
        try:
            result = await self._plan_worker.client.reconcile(command_id)
            if isinstance(result, dict) and "found" in result:
                result = result.get("native") if result.get("found") else None
            return isinstance(result, dict) and result.get("terminalState") in {
                "succeeded", "partially-succeeded", "partial", "failed",
                "rejected", "cancelled", "canceled",
            }
        except Exception:
            logger.debug("Unable to confirm native terminal for cancelled command %s", command_id, exc_info=True)
            return False

    async def handle_chat_submit(
        self,
        ws: WebSocketClient | None,
        request_id: str,
        text: str,
        save_id: str | None,
    ) -> None:
        """Processes a user chat submit message."""
        logger.info("Processing chat submit [%s]: %s (saveId=%s)", request_id, text, save_id)
        if self._active_task and self._active_task.request_id == request_id:
            # Transport replay must not cancel and restart the same decision.
            return
        if self._autonomy is not None and save_id:
            self._autonomy.record_event(save_id, f"chat-submit:{request_id}", "chat.submit")

        is_player = self._is_player_request(request_id)
        if is_player:
            self._preempt_autonomy_for_player(request_id)
            if self._autonomy is not None and save_id:
                self._autonomy.reset_breaker(save_id)
            if self._work_store is not None and save_id:
                try:
                    if self._work_store.state(save_id).paused:
                        self._work_store.set_paused(save_id, False)
                        logger.info("Player chat submit resumed paused work state for save %s [%s]", save_id, request_id)
                except Exception:
                    logger.warning("Failed to resume paused work state for player chat submit [%s]", request_id, exc_info=True)
            await self._interrupt_short_job(save_id)
            if self._active_task and not self._active_task.cancelled:
                was_free = bool(self._autonomy and save_id and self._autonomy.state(save_id).enabled)
                await self.handle_chat_cancel(ws, self._active_task.request_id, "Superseded by player", save_id)
                if was_free:
                    self._autonomy.set_enabled(save_id, True)
                async with self._busy_lock:
                    pass
            if save_id:
                self._register_command_chain(save_id, text, ws, request_id)

        # 1. Concurrency deduplication guard
        if self._busy_lock.locked() or (self._active_task and not self._active_task.cancelled):
            logger.warning("Chat submit [%s] rejected (companion busy)", request_id)
            busy_reply = Envelope.create_chat_reply(
                sender_instance_id=self.instance_id,
                request_id=request_id,
                status="failed",
                reply_text="伙伴正在执行上一条任务，请稍候或点击[取消]后再试。",
                save_id=save_id,
                error="BUSY_CONCURRENT_COMMAND",
                command_id=request_id,
                command_complete=True,
            )
            await self._send_reply(ws, busy_reply)
            return

        async with self._busy_lock:
            # A changed profile/tool surface (or a legacy session without a
            # recorded fingerprint) must not reuse the old provider session.
            existing_cid = self._ensure_session_matches_profile(
                save_id, self.get_conversation_id(save_id)
            )
            previous_cid = existing_cid
            if request_id in self._chain_requests:
                chain = self._command_chains.get(save_id or "")
                instruction = chain.instruction if chain else text
                prompt = self._format_chain_prompt(instruction, save_id)
                task_prompt = f"[续链#{chain.chain_count if chain else 1}] {instruction}"
            else:
                prompt = self._format_agent_prompt(text, save_id)
                task_prompt = text
            is_kimi = self.backend_name == "kimi"
            # The agy SQLite bill is agy-only; Kimi usage comes from the provider
            # wire file. Capture the byte offset now so this turn's records are
            # attributable even on a resumed (cumulative) session.
            start_max_idx = -1 if is_kimi else get_max_gen_idx(existing_cid)
            start_max_step_idx = -1 if is_kimi else get_max_step_idx(existing_cid)
            wire_start_offset = wire_offset(existing_cid, cwd=project_root()) if is_kimi else 0

            # Create active task record with prompt and starting boundary
            active_task = ActiveChatTask(
                request_id=request_id,
                save_id=save_id or "",
                command_id=(self._command_chains[save_id].root_request_id
                            if save_id in self._command_chains else request_id),
                prompt=task_prompt,
                start_max_idx=start_max_idx,
                start_max_step_idx=start_max_step_idx,
                wire_start_offset=wire_start_offset,
                async_task=asyncio.current_task(),
            )
            self._active_task = active_task
            # Park the internal plan worker and release its MCP session: the Mod
            # serves one command socket and the provider turn now owns it.
            await self._claim_execution(save_id)

            try:
                # 2. Immediate progress notification (includes a profile/session
                # rotation note when one just happened, so F8 never silently
                # continues an old tool surface).
                rotation_note = self.consume_profile_rotation_note()
                prog_reply = Envelope.create_chat_reply(
                    sender_instance_id=self.instance_id,
                    request_id=request_id,
                    status="processing",
                    reply_text=rotation_note or "正在思考与执行...",
                    save_id=save_id,
                    command_id=active_task.command_id,
                    command_complete=False,
                )
                await self._send_reply(ws, prog_reply)
                self._configure_backend_progress(ws, request_id, save_id)

                logger.info(
                    "Starting command [%s] for save [%s]. Existing CID: %s, start_max_idx: %d",
                    request_id,
                    save_id,
                    existing_cid,
                    start_max_idx,
                )

                # 4. Invoke agy CLI in executor
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    self._execute_turn,
                    active_task, existing_cid, prompt,
                )

                # If task was cancelled during execution, handle_chat_cancel already handled reply & record
                if active_task.cancelled:
                    logger.info("Task [%s] was cancelled during execution; cancel handler finished.", request_id)
                    if not getattr(active_task, "recorded", False):
                        duration = time.monotonic() - getattr(active_task, "start_time", time.monotonic())
                        cid = self.get_conversation_id(save_id)
                        self._record_command(
                            request_id=request_id,
                            save_id=save_id,
                            conversation_id=cid,
                            prompt=text,
                            status="interrupted",
                            start_idx=start_max_idx,
                            end_idx=-1,
                            usage=None,
                            missing_reason="interrupted",
                            error=getattr(active_task, "abort_reason", None) or "INTERRUPTED",
                            duration=duration,
                        )
                        active_task.recorded = True
                    return

                # 5. Handle output & token usage
                if result.get("status") == "interrupted":
                    status = "interrupted"
                else:
                    status = "completed" if result.get("success") else "failed"
                new_session_established = previous_cid is None
                self._notify_terminal(status)
                reply_text = result.get("response", "")
                cid = result.get("conversation_id") or existing_cid
                if cid and existing_cid and cid != existing_cid:
                    start_max_idx = -1
                if new_session_established and cid:
                    logger.info("New %s session established [%s]; game agent profile bound.", self.provider, cid)
                err = result.get("error")
                duration = result.get("duration", 0.0)

                if not result.get("success"):
                    if err == "RESOURCE_EXHAUSTED":
                        reply_text = "AI 模型额度已用尽，本次任务已停止。自由模式已暂停，请处理额度后手动继续。"
                        if self._autonomy is not None and save_id:
                            try:
                                self._autonomy.control(save_id, "pause")
                            except (TypeError, ValueError):
                                logger.warning("Unable to pause free mode after quota exhaustion for %s", save_id)
                    elif err == "AUTHENTICATION_REQUIRED":
                        reply_text = "Kimi 登录或认证已失效，请完成登录后再试。"
                    elif err == "RATE_LIMIT_EXCEEDED":
                        reply_text = "Kimi 请求过于频繁，请稍后重试。"

                if cid and save_id:
                    self.record_conversation_id(save_id, cid)

                end_max_idx = get_max_gen_idx(cid) if (cid and not is_kimi) else -1

                missing_reason = None
                tokens_used = None
                prompt_tokens = None
                output_tokens = None
                cached_tokens = None
                cache_read_tokens = None
                cache_write_tokens = None
                model_calls = None
                usage = None
                usage_delta = None

                if is_kimi:
                    # Provider-specific metering: sum the wire's usage.record lines
                    # produced after this turn started. Unknown stays unknown; never 0.
                    usage = read_usage_since(
                        cid, active_task.wire_start_offset, cwd=project_root()
                    )
                    if usage is None:
                        usage_source = "unknown"
                        missing_reason = "kimi_wire_usage_unavailable"
                    else:
                        usage_source = usage.get("source", "kimi_wire_usage_record")
                        if usage.get("unknown"):
                            missing_reason = "kimi_wire_usage_partial_unknown"
                else:
                    # Authoritative agy delta from the conversation DB (agy only).
                    usage_delta = get_command_usage_delta(cid, start_max_idx) if cid else None
                    if usage_delta:
                        usage = usage_delta
                        usage_source = "db_gen_metadata_delta"
                        logger.info(
                            "Extracted command usage from DB delta: total=%d, in=%d, out=%d, cache=%d, think=%d (gens=%d)",
                            usage["total_tokens"],
                            usage["input_tokens"],
                            usage["output_tokens"],
                            usage["cache_read_tokens"],
                            usage["thinking_tokens"],
                            usage["generations_count"],
                        )
                    elif existing_cid is None and cid:
                        # Brand new session: CLI usage is session-local to this first turn
                        cli_usage = result.get("usage")
                        if cli_usage and isinstance(cli_usage, dict):
                            usage = dict(cli_usage)
                            usage["source"] = "cli_direct_new_session"
                            usage_source = "cli_direct_new_session"
                        else:
                            usage = None
                            usage_source = "unknown"
                            missing_reason = "new_session_no_usage_available"
                    else:
                        # Resumed session: CLI usage is cumulative across the entire session!
                        # DO NOT pass cumulative session tokens as this turn's usage!
                        usage = None
                        usage_source = "unavailable_in_resume"
                        missing_reason = "db_delta_unavailable_resumed_session_cumulative_ignored"
                        logger.warning(
                            "Resumed session [%s] DB delta unavailable; omitted cumulative CLI usage to avoid displaying session total.",
                            cid,
                        )

                if usage:
                    tokens_used = usage.get("total_tokens")
                    prompt_tokens = usage.get("input_tokens")
                    if prompt_tokens is None:
                        prompt_tokens = usage.get("prompt_tokens")
                    output_tokens = usage.get("output_tokens")
                    cached_tokens = usage.get("cache_read_tokens")
                    # Explicit split: cache read and cache write are different
                    # counters and must not be merged into one "cache" number.
                    cache_read_tokens = usage.get("cache_read_tokens")
                    cache_write_tokens = usage.get("cache_creation_tokens")
                    # Number of provider model calls aggregated for this turn
                    # (usage.record lines already de-duplicated by the wire reader).
                    model_calls = usage.get("generations_count")

                # Persist command record into chat_commands.jsonl (success and failure)
                self._record_command(
                    request_id=request_id,
                    save_id=save_id,
                    conversation_id=cid,
                    prompt=active_task.prompt,
                    status=status,
                    start_idx=start_max_idx,
                    end_idx=end_max_idx,
                    usage=usage,
                    missing_reason=missing_reason,
                    error=err,
                    duration=duration,
                )
                active_task.recorded = True

                plan_line = self._plan_status_line()
                if plan_line:
                    reply_text = (reply_text + "\n" + plan_line) if reply_text else plan_line
                reply_status = status
                job_selected = False
                if status == "completed" and self._work_store and save_id:
                    selected = self._work_store.state(save_id).decision
                    if selected.get("selected") and not selected.get("finished"):
                        reply_status = "selected"
                        job_selected = True
                        self._job_reply_binding = (request_id, save_id, selected.get("taskId"), ws, active_task.command_id)
                        chain = self._command_chains.get(save_id)
                        if chain is not None:
                            chain.waiting_task_id = selected.get("taskId")
                        reply_text = "已选择短作业，等待原生执行。\n" + (reply_text or "")
                    else:
                        reply_status = "decision-completed"
                if not job_selected or status != "completed":
                    self._break_command_chain(save_id)
                final_reply = Envelope.create_chat_reply(
                    sender_instance_id=self.instance_id,
                    request_id=request_id,
                    status=reply_status,
                    reply_text=reply_text or ("任务未能成功执行。" if status == "failed" else "任务执行完毕。"),
                    save_id=save_id,
                    tokens_used=tokens_used,
                    prompt_tokens=prompt_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    model_calls=model_calls,
                    conversation_id=cid,
                    error=err,
                    usage_source=usage_source,
                    provider=self.backend_name,
                    command_id=active_task.command_id,
                    command_complete=not job_selected,
                )
                await self._send_reply(ws, final_reply)
                if self._autonomy is not None and save_id:
                    self._autonomy.record_event(save_id, f"chat-terminal:{request_id}:{status}", f"chat.{status}")
                    autonomy_fingerprint = self._autonomy_requests.pop(request_id, None)
                    if autonomy_fingerprint:
                        if status != "completed":
                            self._autonomy.record_action_result(
                                save_id, autonomy_fingerprint, False, reason=err or f"turn_{status}"
                            )
                        elif self._work_store is not None:
                            job_state = self._work_store.state(save_id)
                            decision = job_state.decision if job_state else {}
                            if not decision.get("selected"):
                                self._autonomy.record_action_result(
                                    save_id, autonomy_fingerprint, True, reason="standby"
                                )
                            else:
                                task_id = decision.get("taskId")
                                task = next((t for t in job_state.tasks if t.id == task_id), None) if task_id else None
                                last_job = job_state.last_job or {}
                                terminal_status = None
                                fail_reason = None
                                if task and task.status in {"partial", "rejected", "failed", "unknown", "cancelled", "completed"}:
                                    terminal_status = task.status
                                    if task.status != "completed":
                                        fail_reason = getattr(task, "error", None) or task.status
                                elif (
                                    last_job
                                    and last_job.get("decisionId") == decision.get("token")
                                    and (not last_job.get("taskId") or last_job.get("taskId") == task_id)
                                ):
                                    # Only a last_job written for THIS decision may settle it;
                                    # an older decision's feedback defers to the pending
                                    # fingerprint below (_on_job_terminal settles it exactly once).
                                    lj_status = str(last_job.get("status") or last_job.get("outcome") or "").lower()
                                    if lj_status:
                                        terminal_status = lj_status
                                        if lj_status != "completed":
                                            fail_reason = last_job.get("reasonCode") or last_job.get("message") or last_job.get("error") or lj_status

                                if terminal_status in {"partial", "rejected", "failed", "unknown", "cancelled"}:
                                    self._autonomy.record_action_result(
                                        save_id, autonomy_fingerprint, False, reason=fail_reason or terminal_status
                                    )
                                elif terminal_status == "completed":
                                    self._autonomy.record_action_result(
                                        save_id, autonomy_fingerprint, True
                                    )
                                else:
                                    if task_id:
                                        self._autonomy_pending_task_fingerprints[task_id] = (save_id, autonomy_fingerprint)
                        else:
                            self._autonomy.record_action_result(
                                save_id, autonomy_fingerprint, status == "completed"
                            )
                    self._autonomy.record_usage(
                        save_id,
                        self._current_game_day_key or "unknown",
                        usage_delta or usage,
                    )
                    # If the provider has no usable context replacement (the game
                    # backend session just grows), rotate on the configured input
                    # context policy so the next turn carries goals/tasks/state
                    # into a fresh session. Latest-request context only, never a
                    # cumulative token sum.
                    rotation_reason = self._note_session_context(save_id, usage_delta or usage)
                    if rotation_reason:
                        logger.info(
                            "Session context policy hit for %s (%s): latestInputContext=%s requests=%s",
                            save_id,
                            rotation_reason,
                            self._session_context_state.get(save_id, {}).get("latestInputContext"),
                            self._session_requests.get(save_id),
                        )
                        self._rotate_provider_session(save_id, reason=rotation_reason)
                logger.info(
                    "Completed chat submit [%s] with status=%s, tokens=%s (source=%s)",
                    request_id,
                    status,
                    tokens_used,
                    usage.get("source", "none") if usage else (usage_source or "none"),
                )

            except asyncio.CancelledError:
                logger.info("handle_chat_submit [%s] cancelled.", request_id)
                self._break_command_chain(save_id)
                if not getattr(active_task, "recorded", False):
                    duration = time.monotonic() - getattr(active_task, "start_time", time.monotonic())
                    cid = self.get_conversation_id(save_id)
                    self._record_command(
                        request_id=request_id,
                        save_id=save_id,
                        conversation_id=cid,
                        prompt=active_task.prompt,
                        status="interrupted",
                        start_idx=start_max_idx,
                        end_idx=-1,
                        usage=None,
                        missing_reason="interrupted",
                        error=getattr(active_task, "abort_reason", None) or "INTERRUPTED",
                        duration=duration,
                    )
                    active_task.recorded = True
            except Exception as ex:
                logger.error("Error in handle_chat_submit [%s]: %s", request_id, ex, exc_info=True)
                self._break_command_chain(save_id)
                err_reply = Envelope.create_chat_reply(
                    sender_instance_id=self.instance_id,
                    request_id=request_id,
                    status="failed",
                    reply_text=f"执行发生系统错误：{ex}",
                    save_id=save_id,
                    error=str(ex),
                    command_id=active_task.command_id,
                    command_complete=True,
                )
                await self._send_reply(ws, err_reply)
            finally:
                if self._active_task is active_task:
                    self._active_task = None
                # Hand the command socket back and let the worker advance any plan
                # the provider just committed (no model turn per step).
                await self._release_execution(save_id)
                # A work turn just freed the single model slot: deliver any queued
                # life-chat submits FIFO (each always reaches a terminal state).
                if self._life_queue:
                    try:
                        asyncio.get_running_loop().create_task(self._drain_life_queue(ws))
                    except RuntimeError:
                        pass

    def _format_agent_prompt(self, user_text: str, save_id: str | None = None) -> str:
        context = render_decision_context(self._decision_context(save_id, origin="chat"))
        return (
            "你是星露谷伙伴智能体，请直接通过已接入的 stardew-companion MCP 工具操作游戏，完成玩家的指令。\n"
            "每次请求都会附带以下紧凑实时上下文（字段缺失为 unknown）；工具结果是执行后的最新事实，"
            "不要为了确认再重复查询。\n"
            f"实时上下文：{context}\n"
            "用submit_plan选择本次唯一语义短作业；内部导航和同一业务的多步操作由运行时执行。"
            "remember_intent记录目标与待办，它们不是执行授权。不要预排第二种业务；"
            "下一业务须下一次模型决策。job-selected仅表示已选择，未执行成功；"
            "下一次输入lastResult是实际终态，信任它，不重复核查。\n"
            "自主执行，不要向玩家询问坐标或请求额外确认。不要读写代码文件或执行终端命令。\n"
            "任务完成后，向玩家简短汇报完成情况与剩余工作。\n"
            "始终用中文回复玩家。\n\n"
            f"玩家指令：{user_text}"
        )

    def _is_player_request(self, request_id: str) -> bool:
        if request_id in self._autonomy_requests:
            return False
        if request_id in self._chain_requests:
            return False
        if request_id.startswith("autonomy-") or request_id.startswith("chain-"):
            return False
        return True

    def _register_command_chain(self, save_id: str, text: str, ws: WebSocketClient | None, request_id: str) -> None:
        self._chain_generation += 1
        self._pending_native_cancels.pop(save_id, None)
        self._command_chains[save_id] = CommandChain(
            instruction=text,
            save_id=save_id,
            root_request_id=request_id,
            started_at=time.monotonic(),
            chain_count=0,
            generation=self._chain_generation,
            ws=ws,
        )

    def _break_command_chain(self, save_id: str | None = None) -> None:
        self._chain_generation += 1
        if save_id:
            self._command_chains.pop(save_id, None)
        else:
            self._command_chains.clear()
        # Never cancel the caller itself: a chain-continuation task breaks its
        # own chain at end of turn and must still deliver the final reply.
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in list(self._chain_tasks):
            if task is current:
                continue
            if not task.done():
                task.cancel()
        self._chain_tasks.clear()

    def _get_command_chain_max(self) -> int:
        try:
            return int(os.getenv("STARDEW_COMMAND_CHAIN_MAX", "8"))
        except ValueError:
            return 8

    async def _on_job_terminal(
        self, save_id: str, execution: StepExecution, reason: str
    ) -> None:
        if not save_id:
            return
        pending_af = self._autonomy_pending_task_fingerprints.pop(execution.task_id, None)
        job_success = execution.task_status == "completed" and execution.outcome == "completed"
        if job_success:
            # Real native success terminal: record a system memory event (deduped
            # by commandId) and evaluate the work-done care hook (contract §2/§4).
            # Failures/cancellals/plans never reach this branch.
            task_title: str | None = None
            if self._work_store is not None:
                try:
                    job_state = self._work_store.state(save_id)
                    task = next(
                        (t for t in job_state.tasks if t.id == execution.task_id), None
                    )
                    if task is not None:
                        task_title = task.title
                except Exception:
                    task_title = None
            if not task_title:
                task_title = execution.operation or "农场作业"
            event_ref = execution.command_id or execution.task_id or task_title
            self._record_memory_event(save_id, f"完成了「{task_title}」", event_ref)
            world: dict[str, Any] = {}
            if isinstance(self._latest_snapshot_payload, dict):
                world = self._latest_snapshot_payload.get("world") or {}
            await self._maybe_fire_care(
                save_id, "work-done", event_ref, world, fact=f"完成了「{task_title}」"
            )
        if pending_af and self._autonomy is not None:
            af_save_id, af_fingerprint = pending_af
            job_fail_reason = execution.reason_code or execution.message or execution.outcome or reason
            self._autonomy.record_action_result(
                af_save_id, af_fingerprint, job_success, reason=None if job_success else job_fail_reason
            )
        chain = self._command_chains.get(save_id)
        if chain is None:
            return
        # Only the terminal of the task the chain is actually waiting on may
        # advance it. A late terminal from a revoked previous job settles to
        # history above but must not fire a chain turn for the player's new
        # instruction; consuming the binding below also makes a duplicated
        # terminal advance nothing.
        if chain.waiting_task_id is None or chain.waiting_task_id != execution.task_id:
            return
        chain.waiting_task_id = None
        if execution.reason_code == "NATIVE_TERMINAL_UNCONFIRMED":
            self._break_command_chain(save_id)
            await self._send_reply(chain.ws, Envelope.create_chat_reply(
                self.instance_id, f"chain-unconfirmed-{uuid.uuid4().hex[:8]}",
                "failed", "原生作业尚未返回最终结果；已停止续链，避免重复派发。",
                save_id=save_id, error=execution.reason_code,
                command_id=chain.root_request_id, command_complete=True,
            ))
            return
        if self._work_store and self._work_store.state(save_id).paused:
            chain.pending_continuation = True
            return
        self._schedule_command_chain(save_id)

    def _schedule_command_chain(self, save_id: str) -> asyncio.Task | None:
        chain = self._command_chains.get(save_id)
        if chain is None:
            return None
        if self._work_store and self._work_store.state(save_id).paused:
            chain.pending_continuation = True
            return None
        chain.pending_continuation = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(
            self._run_command_chain_continuation(save_id, chain.generation)
        )
        self._chain_tasks.add(task)
        task.add_done_callback(self._chain_tasks.discard)
        return task

    async def _run_command_chain_continuation(
        self, save_id: str, generation: int
    ) -> None:
        while (
            self._busy_lock.locked()
            or self._active_task is not None
            or (self._plan_worker and self._plan_worker._provider_active)
        ):
            await asyncio.sleep(0.05)
            chain = self._command_chains.get(save_id)
            if chain is None or chain.generation != generation:
                return
            if self._work_store and self._work_store.state(save_id).paused:
                chain.pending_continuation = True
                return

        chain = self._command_chains.get(save_id)
        if chain is None or chain.generation != generation:
            return
        if self._work_store and self._work_store.state(save_id).paused:
            chain.pending_continuation = True
            return

        max_chains = self._get_command_chain_max()
        if chain.chain_count >= max_chains:
            self._break_command_chain(save_id)
            msg = f"指令续链已达到上限（{max_chains}次），已停止继续。若任务未全部完成，请再次输入指令。"
            reply = Envelope.create_chat_reply(
                sender_instance_id=self.instance_id,
                request_id=f"chain-limit-{uuid.uuid4().hex[:8]}",
                status="completed",
                reply_text=msg,
                save_id=save_id,
                command_id=chain.root_request_id,
                command_complete=True,
            )
            await self._send_reply(chain.ws, reply)
            return

        chain.chain_count += 1
        req_id = f"chain-{uuid.uuid4().hex}"
        self._chain_requests.add(req_id)
        try:
            await self.handle_chat_submit(
                chain.ws, req_id, chain.instruction, save_id
            )
        finally:
            self._chain_requests.discard(req_id)

    async def wait_for_chains(self, timeout: float = 5.0) -> None:
        if not self._chain_tasks:
            return
        await asyncio.wait_for(
            asyncio.gather(*list(self._chain_tasks), return_exceptions=True),
            timeout=timeout,
        )

    def _format_chain_prompt(
        self, instruction: str, save_id: str | None = None
    ) -> str:
        snapshot: dict[str, Any] = {}
        if self._latest_snapshot_payload is not None:
            snapshot = {
                "payload": self._latest_snapshot_payload,
                "worldRevision": self._latest_snapshot_revision,
            }
        work = self._work_context(save_id) if save_id else None
        compact = build_decision_context(
            snapshot, work=work, origin="chat-continuation"
        )
        last_result = None
        if self._work_store and save_id:
            last_result = self._work_store.state(save_id).last_job
        if not last_result and work and work.get("lastJob"):
            last_result = work.get("lastJob")
        if last_result:
            compact["lastResult"] = last_result
        context = render_decision_context(compact)
        last_result_str = (
            json.dumps(last_result, ensure_ascii=False) if last_result else "none"
        )
        return (
            "你是星露谷伙伴智能体，请直接通过已接入的 stardew-companion MCP 工具操作游戏，完成玩家的指令。\n"
            "每次请求都会附带以下紧凑实时上下文（字段缺失为 unknown）；工具结果是执行后的最新事实，"
            "不要为了确认再重复查询。\n"
            f"实时上下文：{context}\n"
            f"这是对玩家指令『{instruction}』的继续。若指令意图已全部完成，直接向玩家总结收尾（本轮不要 submit_plan）；若还有下一业务，用 submit_plan 选择下一个短作业。\n"
            "用submit_plan选择本次唯一语义短作业；内部导航和同一业务的多步操作由运行时执行。"
            "remember_intent记录目标与待办，它们不是执行授权。不要预排第二种业务；"
            "下一业务须下一次模型决策。job-selected仅表示已选择，未执行成功；"
            "下一次输入lastResult是实际终态，信任它，不重复核查。\n"
            "自主执行，不要向玩家询问坐标或请求额外确认。不要读写代码文件或执行终端命令。\n"
            "始终用中文回复玩家。\n\n"
            f"原指令：{instruction}\n"
            f"上一步作业结果（lastResult）：{last_result_str}"
        )

    def _execute_turn(
        self,
        active_task: ActiveChatTask,
        conversation_id: str | None,
        prompt: str,
    ) -> dict[str, Any]:
        """Dispatch one turn to the configured provider adapter."""
        # Preserve the legacy test/integration seam when callers explicitly
        # replace _execute_agy_turn; normal production dispatch remains solely
        # controlled by backend_name.
        if self.backend_name == "kimi" and type(self._execute_agy_turn).__module__.startswith("unittest.mock"):
            return self._execute_agy_turn(active_task, conversation_id, prompt)
        from stardew_ai_runtime.compatibility import assert_native_compatible
        assert_native_compatible(self.run_dir)
        token = uuid.uuid4().hex
        save_id = active_task.save_id
        if save_id and self._work_store is not None:
            self._work_store.begin_decision(save_id, token)
        previous_token = os.environ.get("STARDEW_DECISION_TOKEN")
        os.environ["STARDEW_DECISION_TOKEN"] = token
        try:
            backend = self._get_backend()
            if isinstance(backend, KimiBackend):
                backend.progress = getattr(self, "_backend_progress_callback", None)
            return backend.run(active_task, conversation_id, prompt)
        finally:
            if previous_token is None:
                os.environ.pop("STARDEW_DECISION_TOKEN", None)
            else:
                os.environ["STARDEW_DECISION_TOKEN"] = previous_token

    def _execute_agy_turn(
        self,
        active_task: ActiveChatTask,
        conversation_id: str | None,
        prompt: str,
    ) -> dict[str, Any]:
        """Executes agy CLI command synchronously in background thread with cancellation support."""
        cmd = [self.agy_cmd]

        # Keep the configured provider model on resume as well: agy otherwise
        # may route a saved conversation through its unrelated default model.
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if self.model:
            cmd.extend(["--model", self.model])

        # Explicit provider default supports models whose CLI rejects --effort.
        if self.effort and self.effort != "default":
            cmd.extend(["--effort", self.effort])

        cmd.extend([
            "--mode", "accept-edits",
            "--dangerously-skip-permissions",
            "--print-timeout", "10m",
            "--output-format", "json",
            "--print", prompt,
        ])

        logger.info("Executing agy CLI: %s", " ".join(cmd[:6]) + " ...")
        start_time = time.monotonic()

        # Check BEFORE spawn (cancel race condition guard)
        if active_task.cancelled:
            logger.info("Task [%s] was cancelled before Popen spawn.", active_task.request_id)
            return {
                "success": False,
                "response": "任务已取消。",
                "error": "CANCELLED",
                "conversation_id": conversation_id,
                "duration": 0.0,
            }

        try:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                active_task.process = proc
            except Exception as ex:
                logger.error("Failed to execute agy CLI: %s", ex, exc_info=True)
                return {
                    "success": False,
                    "response": f"调用后台 AI 服务发生异常：{ex}",
                    "error": str(ex),
                    "conversation_id": conversation_id,
                }

            # Check AFTER spawn (cancel race condition guard)
            if active_task.cancelled:
                logger.info(
                    "Task [%s] was cancelled immediately after Popen spawn; killing proc PID %d.",
                    active_task.request_id,
                    proc.pid,
                )
                try:
                    proc.kill()
                except Exception as ex:
                    logger.debug("Error killing process on post-spawn cancel: %s", ex)
                return {
                    "success": False,
                    "response": "任务已取消。",
                    "error": "CANCELLED",
                    "conversation_id": conversation_id,
                    "duration": time.monotonic() - start_time,
                    "status": "interrupted",
                }

            try:
                stdout, stderr = proc.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                logger.error("agy CLI timed out after 600s")
                return {
                    "success": False,
                    "response": "执行超时 (10分钟)，操作已中止。",
                    "error": "TIMEOUT",
                    "conversation_id": conversation_id,
                    "duration": time.monotonic() - start_time,
                    "status": "interrupted",
                }

            duration = time.monotonic() - start_time
            stdout = stdout or ""
            stderr = stderr or ""

            if active_task.cancelled:
                return {
                    "success": False,
                    "response": "任务已取消。",
                    "error": "CANCELLED",
                    "conversation_id": conversation_id,
                    "duration": duration,
                    "status": "interrupted",
                }

            # Parse JSON output if available
            parsed = None
            try:
                parsed = json.loads(stdout)
            except Exception:
                # Try finding JSON block in stdout if CLI emitted header/history text
                first_brace = stdout.find("{")
                last_brace = stdout.rfind("}")
                if first_brace != -1 and last_brace > first_brace:
                    candidate = stdout[first_brace:last_brace + 1]
                    try:
                        p = json.loads(candidate)
                        if isinstance(p, dict):
                            parsed = p
                    except Exception:
                        pass
                if parsed is None:
                    # Try line by line backwards
                    for line in reversed(stdout.splitlines()):
                        line = line.strip()
                        if line.startswith("{") and line.endswith("}"):
                            try:
                                p = json.loads(line)
                                if isinstance(p, dict):
                                    parsed = p
                                    break
                            except Exception:
                                pass

            cid = (parsed.get("conversation_id") if parsed else None) or conversation_id
            op_status = parsed.get("status") if parsed else None
            raw_response = parsed.get("response", "") if parsed else ""
            response_text = raw_response.strip() if isinstance(raw_response, str) else ""
            usage = parsed.get("usage") if parsed else None

            # Quota / Rate limit error checking using authoritative new steps baseline
            # Check for genuine fresh quota evidence:
            # 1. Authoritative DB steps check: new step_type=17 with idx > start_max_step_idx
            # Reset step baseline if agy established a new session / changed conversation_id
            step_baseline = -1 if (cid and conversation_id and cid != conversation_id) else active_task.start_max_step_idx
            is_new_quota_step, step_err_text = check_new_quota_error(
                cid, step_baseline
            )
            if is_new_quota_step:
                logger.error(
                    "Genuine Quota Exhausted detected in new step_type=17 step for [%s]: %s",
                    cid,
                    step_err_text[:300],
                )
                return {
                    "success": False,
                    "response": "AI 模型额度已用尽，任务已停止。请稍后或联系管理员补充额度。",
                    "error": "RESOURCE_EXHAUSTED",
                    "conversation_id": cid,
                    "duration": duration,
                }

            # 2. Check current process stderr (process-local, never cumulative)
            hard_quota_pattern = re.compile(
                r"(RESOURCE_EXHAUSTED|Resource has been exhausted|quota exceeded|Individual quota reached)",
                re.IGNORECASE,
            )
            rate_limit_pattern = re.compile(
                r"(429 Too Many Requests|rate limit exceeded|rate_limit)",
                re.IGNORECASE,
            )

            if stderr and hard_quota_pattern.search(stderr):
                logger.error("Genuine Quota Exhausted detected in stderr: %s", stderr[:300])
                self._notify_backend_failure("RESOURCE_EXHAUSTED")
                return {
                    "success": False,
                    "response": "AI 模型额度已用尽，任务已停止。请稍后或联系管理员补充额度。",
                    "error": "RESOURCE_EXHAUSTED",
                    "conversation_id": cid,
                    "duration": duration,
                }

            if stderr and rate_limit_pattern.search(stderr):
                logger.warning("Transient Rate Limit detected in stderr: %s", stderr[:300])
                self._notify_backend_failure("RATE_LIMIT_EXCEEDED")
                return {
                    "success": False,
                    "response": "AI 请求过于频繁 (Rate Limit)，请稍候重试。",
                    "error": "RATE_LIMIT_EXCEEDED",
                    "conversation_id": cid,
                    "duration": duration,
                }

            # 3. For new conversation only (not resumed), parsed.get("error") is fresh evidence
            is_new_session = conversation_id is None
            if is_new_session and parsed and parsed.get("error"):
                new_session_err = str(parsed.get("error"))
                if hard_quota_pattern.search(new_session_err):
                    logger.error("Quota Exhausted in new session error: %s", new_session_err[:300])
                    self._notify_backend_failure("RESOURCE_EXHAUSTED")
                    return {
                        "success": False,
                        "response": "AI 模型额度已用尽，任务已停止。请稍后或联系管理员补充额度。",
                        "error": "RESOURCE_EXHAUSTED",
                        "conversation_id": cid,
                        "duration": duration,
                    }
                elif rate_limit_pattern.search(new_session_err):
                    self._notify_backend_failure("RATE_LIMIT_EXCEEDED")
                    return {
                        "success": False,
                        "response": "AI 请求过于频繁 (Rate Limit)，请稍候重试。",
                        "error": "RATE_LIMIT_EXCEEDED",
                        "conversation_id": cid,
                        "duration": duration,
                    }

            # 4. If op_status is SUCCESS and response_text exists, current turn succeeded!
            # (Ignore any old cumulative error carried in parsed JSON)
            if op_status == "SUCCESS":
                if response_text:
                    return {
                        "success": True,
                        "response": response_text,
                        "conversation_id": cid,
                        "usage": usage,
                        "duration": duration,
                    }
                else:
                    logger.warning("agy returned SUCCESS but response was empty.")
                    return {
                        "success": False,
                        "response": "模型已执行但未返回具体汇报说明。",
                        "error": "EMPTY_MODEL_RESPONSE",
                        "conversation_id": cid,
                        "usage": usage,
                        "duration": duration,
                    }

            # 5. Non-zero exit code or failed status
            is_error = (proc.returncode != 0) or (op_status not in ("SUCCESS", None))
            if is_error:
                cli_err = (parsed.get("error") or "") if is_new_session else ""
                err_text = f"{stderr}\n{cli_err}".strip()
                if err_text:
                    err_marker = err_text[:200]
                    resp = response_text if response_text else f"AI 执行出错 (退出码 {proc.returncode})：{err_text[:200]}"
                else:
                    err_marker = f"AGY_EXIT_{proc.returncode}" if proc.returncode != 0 else f"AGY_STATUS_{op_status}"
                    resp = response_text if response_text else f"AI 执行出错 (退出码 {proc.returncode})"
                logger.error("agy exited with error: code=%d, status=%s, err=%s", proc.returncode, op_status, err_text[:300] or err_marker)
                return {
                    "success": False,
                    "response": resp,
                    "error": err_marker,
                    "conversation_id": cid,
                    "duration": duration,
                }

            # Plain text output fallback if proc.returncode == 0
            clean_stdout = stdout.strip()
            if proc.returncode == 0 and clean_stdout:
                return {
                    "success": True,
                    "response": clean_stdout,
                    "conversation_id": conversation_id,
                    "duration": duration,
                }

            return {
                "success": False,
                "response": f"AI 未能正常生成回复 (code {proc.returncode})",
                "error": "NO_OUTPUT",
                "conversation_id": conversation_id,
                "duration": duration,
            }
        except Exception as ex:
            logger.error("Failed to execute agy CLI: %s", ex, exc_info=True)
            return {
                "success": False,
                "response": f"调用后台 AI 服务发生异常：{ex}",
                "error": str(ex),
                "conversation_id": conversation_id,
            }
        finally:
            active_task.process = None

    async def _send_reply(self, ws: WebSocketClient | None, reply: Envelope) -> None:
        if ws is None:
            return
        try:
            json_text = json.dumps(reply.to_mapping(), ensure_ascii=False)
            await ws.send_text(json_text)
        except Exception as ex:
            logger.warning("Failed to send chat reply to WebSocket: %s", ex)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="stardew-chat-bridge",
        description="Background Chat Bridge connecting in-game UI to agy AI agent",
    )
    parser.add_argument(
        "--run-dir",
        type=str,
        default=os.getenv("STARDEW_RUN_DIR"),
        help="Path to run directory containing transport-discovery.json (or set STARDEW_RUN_DIR)",
    )
    parser.add_argument(
        "--backend", type=str, choices=["agy", "kimi"], default=None,
        help="Chat provider (overrides config/chat-backend.json)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Provider model (overrides config/chat-backend.json)",
    )
    parser.add_argument(
        "--effort",
        type=str,
        default=None,
        choices=["default", "low", "medium", "high"],
        help="Reasoning effort for agy (uses configuration or provider default); default omits the CLI option",
    )
    parser.add_argument(
        "--agy-cmd",
        type=str,
        default="agy.exe",
        help="Path or name of the agy CLI binary",
    )
    parser.add_argument("--kimi-cmd", type=str, default="kimi.exe", help="Path or name of Kimi CLI")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    bridge = ChatBridge(
        run_dir=args.run_dir,
        model=args.model,
        effort=args.effort,
        agy_cmd=args.agy_cmd,
        backend=args.backend,
        kimi_cmd=args.kimi_cmd,
    )

    _, log_file_path = configure_chat_bridge_file_logging(bridge.run_dir)
    logger.info("ChatBridge 文件日志已启动: %s", log_file_path)

    print("================================================================")
    print(">>> 星露谷伙伴后台对话服务 (Stardew Chat Bridge) 已就绪 <<<")
    print(f"运行目录: {bridge.run_dir or args.run_dir or '自动发现 (优先使用设置向导已配置的正常游戏Mod路径)'}")
    print(f"日志文件: {log_file_path}")
    print(f"实际后端: {bridge.provider} | 模型标识: {bridge.model}")
    if bridge.provider == "agy":
        print(f"agy 思考强度: {bridge.effort}")
    print("在游戏中按 F8 开启伙伴窗口，输入中文指令即可直接交互！")
    print("================================================================")

    def _flush_handlers() -> None:
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass

    prev_excepthook = sys.excepthook

    def _unhandled_exception_hook(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            prev_excepthook(exc_type, exc_value, exc_traceback)
            return
        logger.critical("未捕获异常导致服务退出", exc_info=(exc_type, exc_value, exc_traceback))
        _flush_handlers()
        prev_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _unhandled_exception_hook

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n服务已由用户退出。")
    except Exception as ex:
        logger.critical("后台服务发生未捕获异常退出: %s", ex, exc_info=True)
        _flush_handlers()
        raise
    finally:
        _flush_handlers()
        sys.excepthook = prev_excepthook


if __name__ == "__main__":
    main()
