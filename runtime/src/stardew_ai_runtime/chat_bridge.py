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

from stardew_ai_runtime.agent_backends import (
    AgyBackend,
    CodexBackend,
    DshBackend,
    KimiBackend,
    McodeBackend,
)
from stardew_ai_runtime.agent_instructions import instructions_revision, load_instruction_bundle
from stardew_ai_runtime.agy_process import run_agy_process, terminate_agy_process
from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.chat_backend_config import load_chat_backend_config, project_root
from stardew_ai_runtime.codex_game_profile import CODEX_GAME_TOOL_PROFILE_VERSION
from stardew_ai_runtime.companion_care import CompanionCareService
from stardew_ai_runtime.companion_memory import CompanionMemoryStore
from stardew_ai_runtime.companion_milestones import (
    CompanionMilestoneStore,
    wire_node,
)
from stardew_ai_runtime.companion_profile import CompanionProfileStore
from stardew_ai_runtime.decision_context import (
    build_decision_context,
    goal_context,
    model_decision_context,
    objective_scope,
    render_decision_context,
)
from stardew_ai_runtime.decision_policy import decision_policy
from stardew_ai_runtime.job_feedback import (
    compact_job_feedback,
    compact_task_feedback,
    project_task_feedback_state,
)
from stardew_ai_runtime.kimi_wire_usage import read_usage_since, wire_offset
from stardew_ai_runtime.life_chat import LifeChatService
from stardew_ai_runtime.owner_process import OwnerProcessGuard
from stardew_ai_runtime.plan_executor import (
    DispatchDeferred,
    PlanExecutor,
    StepExecution,
    merge_reconciled_operation_result,
    normalise_native_result,
)
from stardew_ai_runtime.prompt_delivery import PromptDelivery
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
from stardew_ai_runtime.snapshot_cache import cache_lock, write_cache
from stardew_ai_runtime.usage_display import UsageDisplay
from stardew_ai_runtime.usage_meter import normalize_usage
from stardew_ai_runtime.websocket_client import (
    ConnectionClosed,
    WebSocketClient,
    WebSocketError,
)
from stardew_ai_runtime.work_controls import explicit_cancel_scope
from stardew_ai_runtime.work_state import (
    WorkStateError,
    WorkStore,
    bind_execution_scope_params,
)

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
    usage["input_context_measured"] = any(key in u for key in (2, 5))
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

        latest = _parse_usage_from_gen_metadata(rows[-1][1])
        measured = latest.get("input_context_measured", False)
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
            "input_includes_cache": False,
            "input_context_measured": measured,
            "latestRequestInputContext": latest["input_tokens"] + latest["cache_read_tokens"] if measured else None,
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
        self._close_lock = asyncio.Lock()

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
        # An idle worker may be closing the old session while a player control
        # needs a new one. Never let that close clear the new session's state.
        async with self._close_lock:
            await self._open_session()

    async def _open_session(self) -> None:
        """Open while holding the lifecycle lock, including native control calls."""
        if self._task is None or self._task.done():
            self._loop = asyncio.get_running_loop()
            self._requests = asyncio.Queue()
            self._ready = self._loop.create_future()
            self._closed = False
            self._task = self._loop.create_task(self._session_loop())
        await asyncio.wait_for(asyncio.shield(self._ready), timeout=self.timeout_seconds)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        # Native controls must reach the existing scheduler while a long native
        # operation is awaiting its terminal result. The read that determines
        # whether cancellation is needed must not queue behind that job either.
        if name in {"cancel_plan_command", "reconcile_plan_command"} or name == "dispatch_plan_operation" and (arguments or {}).get("operation") in {"cancel_task", "pause_task", "resume_task"}:
            # Receiving a terminal can make the worker idle while this read is
            # still pending. Keep that idle close from interrupting the reply.
            async with self._close_lock:
                await self._open_session()
                result = await asyncio.wait_for(self._live_session.call_tool(name, arguments or {}), timeout=10)
                if getattr(result, "isError", False):
                    raise RuntimeError(f"Native control rejected: {_tool_result_payload(result)}")
                return _tool_result_payload(result)
        await self.open()
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
        async with self._close_lock:
            if self._task is None:
                return
            try:
                if not self._task.done():
                    await asyncio.wait_for(self._command(("close", None, None)), timeout=5.0)
            except Exception:
                logger.debug("Internal MCP client close command failed", exc_info=True)
            task, self._task = self._task, None
            if not task.done():
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task
                except Exception:
                    logger.debug("Internal MCP session task ended with error", exc_info=True)
            self._requests = None
            self._ready = None


    # ------------------------------------------------------------- internals
    async def _command(self, command: tuple[str, Any, Any]) -> Any:
        if self._requests is None or self._loop is None or self._closed:
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
        finally:
            self._closed = True
            self._fail_queued_requests()

    def _fail_queued_requests(self) -> None:
        queue = self._requests
        if queue is None:
            return
        while not queue.empty():
            _, future = queue.get_nowait()
            if not future.done():
                future.set_exception(RuntimeError("Internal MCP session closed before request completed; native outcome is unknown."))

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
            except BaseException as ex:  # noqa: BLE001 - release caller even on cancellation
                if not future.done():
                    future.set_exception(
                        RuntimeError("Internal MCP session interrupted; native outcome is unknown.")
                        if isinstance(ex, (asyncio.CancelledError, GeneratorExit)) else ex
                    )
                if not isinstance(ex, Exception):
                    raise
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
        self._executing = False
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
        self.business_gate: Any | None = None

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
        """Own the native socket only for this execution batch, never while idle."""
        self._executing = True
        try:
            return await self._evaluate_ready_steps()
        finally:
            # External Codex/observation turns do not pass through the bridge's
            # _claim_execution. Release here after native reconciliation settles,
            # so a finished worker cannot monopolize the sole command socket.
            try:
                await self._release_idle_session()
            finally:
                self._executing = False

    async def _release_idle_session(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close) and getattr(self.client, "connected", True):
            try:
                await close()
            except Exception:
                logger.warning("Plan worker could not release its idle MCP session", exc_info=True)

    async def _evaluate_ready_steps(self) -> list[StepExecution]:
        """Run ready steps until none remain (or a deviation needs the model)."""
        self._dirty = False
        executions: list[StepExecution] = []
        for _ in range(self.step_limit):
            if self._provider_active:
                break
            save_id = await self._resolve_save_id()
            if not save_id:
                break
            # A day rollover can close the native socket after it accepts a
            # command but before the worker receives its terminal.  An expired
            # running lease is otherwise invisible to has_ready_step forever.
            if await self._recover_expired_step(save_id):
                break
            # Receipt reconciliation is read-only and must remain possible under
            # a disconnect hold or human pause. The gate still blocks dispatch;
            # otherwise a stale running lease can prevent the recovery decision
            # that would release a program hold from ever becoming eligible.
            if self.business_gate is not None and not self.business_gate(save_id):
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
                feedback = compact_task_feedback(completed_task,
                    {**(execution.result or {}), "effects": execution.effects,
                     "reasonCode": execution.reason_code, "commandId": execution.command_id,
                     "message": execution.message},
                    operation=execution.operation or "", status=execution.outcome)
                feedback["effectCount"] = sum(len(step.effects) for step in completed_task.steps) if completed_task else len(execution.effects)
                # Publish finished only after releasing the native owner: an
                # external client may immediately observe upon seeing this flag.
                await self._release_idle_session()
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

    async def _recover_expired_step(self, save_id: str) -> bool:
        now = time.monotonic()
        state = self.store.state(save_id)
        for task in state.tasks:
            for step in task.steps:
                if step.status != "running" or step.lease_until > now:
                    continue
                native = None
                if step.command_id:
                    try:
                        native = await asyncio.wait_for(self._reconcile(step.command_id), timeout=10.0)
                    except Exception:
                        logger.warning("Could not reconcile expired native command %s", step.command_id, exc_info=True)
                goal = next((goal for goal in state.goals if goal.id == task.goal_id), None)
                native = merge_reconciled_operation_result(step.operation, step.feedback, native,
                    params=bind_execution_scope_params(step.operation, step.params, goal.constraints if goal else {}))
                outcome, reason, effects, revision = normalise_native_result(native)
                if outcome is None:
                    # The existing store recovery records an explicit unknown
                    # once; it never re-dispatches the unconfirmed command.
                    decisions = self.store.recover(save_id, now=now)
                    if not any(item.get("stepId") == step.id for item in decisions):
                        return True
                    outcome, reason, effects = "unknown", "NATIVE_TERMINAL_UNCONFIRMED", []
                else:
                    self.store.commit_step_result(
                        save_id, task_id=task.id, step_id=step.id,
                        outcome=outcome, effects=effects, reason_code=reason,
                        snapshot_revision=revision, command_id=step.command_id,
                        game_date=self._fresh_state()[1],
                        feedback=compact_job_feedback(native, operation=step.operation, status=outcome, params=step.params),
                    )
                recovered_task = next((item for item in self.store.state(save_id).tasks if item.id == task.id), None)
                execution = StepExecution(
                    status="executed", task_id=task.id, goal_id=task.goal_id,
                    step_id=step.id, operation=step.operation,
                    command_id=step.command_id, outcome=outcome,
                    task_status=recovered_task.status if recovered_task else "unknown",
                    reason_code=reason, effects=effects, result=native,
                )
                if outcome == "completed" and execution.task_status == "pending":
                    # This receipt closes one step; the same decision still owns
                    # the remaining serial work. Do not send a job terminal.
                    self._dirty = True
                    return True
                feedback = compact_task_feedback(recovered_task,
                    {**(native if isinstance(native, dict) else {}), "commandId": step.command_id, "reasonCode": reason,
                     "terminalState": native.get("terminalState", "unknown") if isinstance(native, dict) else "unknown",
                     "effects": effects}, operation=step.operation, status=outcome,
                )
                await self._release_idle_session()
                self.store.finish_job(save_id, feedback, task_id=task.id)
                self.pending_reasons.append(reason or "NATIVE_RECOVERED")
                if self.terminal_callback:
                    try:
                        callback = self.terminal_callback(save_id, execution, reason or "NATIVE_RECOVERED")
                        if asyncio.iscoroutine(callback):
                            await callback
                    except Exception:
                        logger.warning("Plan worker recovery callback failed", exc_info=True)
                return True
        return False

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
        snapshot, _ = self.snapshot_provider() if self.snapshot_provider else (None, None)
        companion = (snapshot or {}).get("companion") or {}
        world = (snapshot or {}).get("world") or {}
        bedtime = companion.get("bedtime", 2400)
        clock = world.get("timeOfDay", 600)
        if (snapshot or {}).get("actorMode") == "player":
            # Player mode: no companion profile bedtime. Late gates only steer
            # new dispatches toward native sleep so the player is not idled
            # into collapse (≥00:30 non-sleep defers, ≥01:30 sleep only).
            gate = ChatBridge._player_mode_clock_gate(operation, clock, params)
            if gate:
                # No native send has occurred. Park this exact unfinished step
                # until the next real day and return the model slot for sleep.
                # DispatchDeferred is for human/revoked authority only: retrying
                # a clock-gated selection each snapshot deadlocks recovery.
                year, season, day = world.get("year"), world.get("season"), world.get("dayOfMonth")
                seasons = ("spring", "summer", "fall", "winter")
                if type(year) is not int or type(day) is not int or season not in seasons or not 1 <= day <= 28:
                    raise RuntimeError("Late action deferred; native calendar unavailable. Re-observe before selecting recovery.")
                if day < 28:
                    next_date = {"year": year, "season": season, "day": day + 1}
                else:
                    index = seasons.index(season)
                    next_date = {"year": year + (index == 3), "season": seasons[(index + 1) % 4], "day": 1}
                return {"status": "waiting", "commandId": command_id,
                        "reasonCode": "PLAYER_LATE_ACTION_DEFERRED", "message": gate,
                        "nativeDispatched": False,
                        "waitCondition": {"type": "gameDay", "params": next_date,
                                          "reasonCode": "PLAYER_LATE_ACTION_DEFERRED"}, "effects": []}
        else:
            resting = companion.get("restState") in {"winding-down", "returning-home", "waiting-for-bed", "sleeping"}
            if resting or (isinstance(clock, int) and isinstance(bedtime, int)
                    and clock // 100 * 60 + clock % 100 >= bedtime // 100 * 60 + bedtime % 100 - 60):
                raise DispatchDeferred("bedtime: keep this step for tomorrow")
        if (state.paused or task is None or state.decision.get("taskId") != task.id
                or state.decision.get("finished")):
            logger.warning("Native dispatch authority deferred: save=%s command=%s task=%s decisionTask=%s paused=%s finished=%s",
                           save_id, command_id, task.id if task else None,
                           state.decision.get("taskId"), state.paused, state.decision.get("finished"))
            raise DispatchDeferred("paused or decision revoked before native send")
        try:
            return await self.client.call_tool(
                "dispatch_plan_operation",
                {"operation": operation, "params": dict(params), "command_id": command_id},
            )
        except Exception as ex:
            if "UNAUTHORIZED_JOB_COMMAND" in str(ex):
                # A rejected persisted claim is a real fault, not an idle wait.
                # Commit it so the model can recover instead of silently
                # reattempting the same unauthorized command forever.
                raise RuntimeError(str(ex)) from ex
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
    decision_token: str | None = None
    decision_day: str | None = None
    provider_conversation_id: str | None = None
    instructions_text: str | None = None
    # Full original context is retained only for a provider's fresh-session recovery.
    provider_bootstrap_prompt: str | None = None
    read_only: bool = False
    # In-game read-only turns retain conversation history while the life tool
    # surface independently removes execution authority.
    preserve_session: bool = False
    goal_scope: str | None = None
    goal_default: str | None = None
    work_request_ids: list[str] = field(default_factory=list)


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
        mcode_cmd: str | None = None,
        internal_plan_client: Any | None = None,
        enable_plan_worker: bool = True,
    ):
        self.run_dir = Path(run_dir) if run_dir else None
        configured = load_chat_backend_config()
        self.backend_name = backend or configured["backend"]
        self.model = backend_model or model or (configured["model"] if self.backend_name == configured["backend"] else ("kimi-code/k3" if self.backend_name == "kimi" else "" if self.backend_name in {"codex", "mcode"} else "deepseek-flash" if self.backend_name == "dsh" else "gemini-3.8-flash"))
        self.effort = effort or configured.get("effort") or ("low" if self.backend_name in {"codex", "dsh"} else "default")
        self.instance_id = instance_id or f"chat-bridge-{uuid.uuid4().hex[:8]}"
        self.agy_cmd = agy_cmd or "agy.exe"
        self.kimi_cmd = kimi_cmd or "kimi.exe"
        self.mcode_cmd = mcode_cmd or "mcode"
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
        self._usage_display = UsageDisplay(self._commands_file, project_root() / "config" / "usage-prices.json",
                                           self._commands_file.parent / "data" / "model-usage.jsonl")
        if self.backend_name == "dsh":
            from stardew_ai_runtime.usage_meter import recover_dsh_receipts
            recover_dsh_receipts(self._commands_file.parent, self._commands_file)
        elif self.backend_name == "agy":
            from stardew_ai_runtime.usage_meter import recover_agy_receipts
            recover_agy_receipts(self._commands_file.parent)
        self._sessions: dict[str, str] = self._load_sessions()
        self._autonomy: AutonomyController | None = None
        self._autonomy_requests: dict[str, str] = {}
        self._current_game_day_key: str | None = None
        # Latest native snapshot the chat channel received; the compact decision
        # context is rebuilt from it for every model decision.
        self._latest_snapshot_payload: dict[str, Any] | None = None
        self._latest_snapshot_observed_at: float | None = None
        self._latest_snapshot_revision: int | None = None
        # Keep recovery history and usage diagnostics; the harness owns compaction.
        self._session_history: dict[str, list[str]] = {}
        self._session_requests: dict[str, int] = {}
        self._session_context_state: dict[str, dict[str, Any]] = {}
        self._last_day_settlement: dict[str, Any] | None = None
        self._last_native_ui_recovery_signal: tuple[Any, ...] | None = None
        self._autonomy_generation = 0
        self._autonomy_last_snapshot_at: dict[str, float] = {}
        self._autonomy_pending_snapshot: dict[str, tuple[Envelope, set[asyncio.Task], WebSocketClient | None]] = {}
        self._autonomy_debounce_tasks: dict[str, asyncio.Task] = {}
        self._autonomy_wakeup_tasks: dict[str, asyncio.Task] = {}
        self._autonomy_wakeup_snapshots: dict[str, tuple[Envelope, set[asyncio.Task], WebSocketClient | None]] = {}
        self._autonomy_pending_task_fingerprints: dict[str, tuple[str, str]] = {}
        self._bind_autonomy_store()
        # Durable per-save goals/tasks/todos; bound once a run dir is known
        # (explicit --run-dir or discovered from the running Mod).
        self._work_store: WorkStore | None = None
        self._plan_worker: PlanWorker | None = None
        self._last_work_projection: dict[str, str] = {}
        self._work_projection_times: dict[str, tuple[str, str]] = {}
        # One unresolved native cancellation per save; cleared on confirmation.
        self._pending_native_cancels: dict[str, tuple[str, str]] = {}
        # Latest accepted player root request per save (UI command attribution).
        self._active_player_requests: dict[str, str] = {}
        # Independent autostart test runs, keyed by save id. Each entry tracks the
        # stable preset request id, the bound goal id and whether the terminal
        # completion was already reported back to the Mod (exactly-once done signal).
        self._preset_runs: dict[str, dict[str, Any]] = {}
        from .preset_day_boundary import PresetDayBoundary
        self._preset_day_boundary = PresetDayBoundary(self.run_dir)
        # Persistent-outbox delivery: one delivery task per save, plus one
        # completion future per in-flight delivery request id.
        self._delivery_tasks: dict[str, asyncio.Task] = {}
        self._delivery_pending: dict[str, asyncio.Future] = {}
        self._life_drain_tasks: set[asyncio.Task] = set()
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
        self._pending_life_preparation: dict[str, dict[str, dict[str, Any]]] = {}
        self._life_draining = False
        self._life_control_generation: dict[str, int] = {}
        self._evening_care_fired_day: str | None = None
        self._deferred_care_tasks: set[asyncio.Task[None]] = set()
        self._chat_ws: WebSocketClient | None = None
        # Player-mode control state. The /chat channel never receives
        # mod.welcome, so these facts are learned from the additive
        # world.snapshot fields (actorMode/controlOwner). A mod that never
        # sends them behaves exactly like today's companion mode.
        self.actor_mode: str = "companion"
        self.control_owner: str | None = None
        self.supports_thinking_pause: bool | None = None
        self._player_claim_attempted = False
        # Explicit F9 human takeover per save: while set, a (re)connect never
        # auto-claims player control; only an explicit re-handoff (the mod
        # reporting owner=agent again) clears it. In-memory on purpose: a full
        # game+bridge restart is a fresh launcher session whose bootstrap claim
        # is the designed entry point.
        self._manual_takeover: dict[str, bool] = {}
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
            elif self.backend_name == "dsh":
                if self.run_dir is None:
                    raise ValueError("dsh backend requires a game run directory")
                self._backend = DshBackend(self.run_dir, model=self.model or "deepseek-flash", effort=self.effort,
                    progress=getattr(self, "_backend_progress_callback", None))
            elif self.backend_name == "codex":
                if self.run_dir is None:
                    raise ValueError("Codex backend requires a game run directory")
                self._backend = CodexBackend(
                    run_dir=self.run_dir, model=self.model or "",
                    effort=self.effort,
                    http_provider=os.environ.get("STARDEW_CODEX_HTTP_PROVIDER") or load_chat_backend_config().get("httpProvider") or None,
                    progress=getattr(self, "_backend_progress_callback", None),
                )
            elif self.backend_name == "mcode":
                if self.run_dir is None:
                    raise ValueError("mcode backend requires a game run directory")
                self._backend = McodeBackend(
                    run_dir=self.run_dir, model=self.model or "",
                    command=self.mcode_cmd,
                    effort=self.effort,
                    progress=getattr(self, "_backend_progress_callback", None),
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
            self._plan_worker.business_gate = self._plan_dispatch_allowed
            self._plan_worker.snapshot_provider = self._plan_snapshot_state
            self._plan_worker.terminal_callback = self._on_job_terminal
            return
        client = self._internal_plan_client_override
        if client is None:
            client = InternalMcpPlanClient(self.run_dir)
        self._plan_worker = PlanWorker(store, client)
        self._plan_worker.progress_callback = self._publish_job_progress
        self._plan_worker.business_gate = self._plan_dispatch_allowed
        self._plan_worker.snapshot_provider = self._plan_snapshot_state
        self._plan_worker.terminal_callback = self._on_job_terminal

    def _plan_dispatch_allowed(self, save_id: str) -> bool:
        # Finish/reconcile an already dispatched native step, but do not start
        # another step from the old plan while a player message is waiting to
        # revise it. Life chat uses cached observations and needs no native slot.
        return (not self._preset_business_blocked(save_id)
                and not self._busy_lock.locked()
                and not self._life_queue and not self._life_draining)

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
            "timeOfDay": world.get("timeOfDay"),
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
        acquired = False
        try:
            await self._execution_lock.acquire()
            acquired = True
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
        except BaseException:
            # Startup preset can supersede autonomy while MCP close is awaiting.
            # A cancelled claim must unwind its own partially acquired lease.
            if acquired:
                self._execution_lock.release()
            worker.set_provider_active(False)
            raise
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
        if not self._preset_business_blocked(save_id):
            worker.notify()

    def _restore_accepted_preset_scope(self, save_id: str) -> tuple[str, str] | None:
        """Recover an accepted goal-less preset, including a first wait-only turn.

        Use only its exact persisted player request and configured native marker.
        Never revive a cancelled preset or change human pause/control state.
        """
        policy = getattr(self, "_preset_day_boundary", None)
        if not policy or not policy.enabled or not self._work_store or not self._autonomy:
            return None
        preset = json.loads(Path(policy.preset_path).read_text(encoding="utf-8-sig"))
        import hashlib
        rid = "preset-" + hashlib.sha256((str(preset["runId"]) + "\n" + save_id).encode()).hexdigest()[:12]
        marker_path = Path(self.run_dir) / "data/preset-goal-state.json"
        if not marker_path.is_file():
            return None
        marker = json.loads(marker_path.read_text(encoding="utf-8-sig")).get(rid, {})
        if not marker or marker.get("completed") or marker.get("cancelled"):
            return None
        root = next((r for r in self._work_store.state(save_id).requests if r.get("id") == rid), None)
        if not root or root.get("goalId") or root.get("workStopped") or root.get("text") != preset.get("goalText"):
            return None
        self._work_store.reconcile_verified_preset_history(save_id, policy.native_day(self._latest_snapshot_payload))
        bound = self._work_store.ensure_preset_main_goal(save_id, rid, str(preset["goalText"]))
        self._autonomy.set_goal_scope(save_id, bound.id)
        self._autonomy.request_job_decision(save_id)
        self._preset_runs[save_id] = {"request_id": rid, "goal_id": bound.id, "notified": False}
        logger.info("Recovered accepted wait-only preset [%s] into main goal %s", rid, bound.id)
        return rid, bound.id

    def _preset_business_blocked(self, save_id: str | None) -> bool:
        policy = getattr(self, "_preset_day_boundary", None)
        return bool(policy and policy.blocked(save_id, self._work_store, self._latest_snapshot_payload))

    def _notify_plan_worker(self, save_id: str | None = None) -> None:
        if self._preset_business_blocked(save_id):
            return
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

    @staticmethod
    def _codex_turn_usage(result: dict[str, Any]) -> dict[str, Any] | None:
        raw = result.get("usage")
        latest = result.get("usage_latest_request_input_context")
        measured = isinstance(latest, int) and not isinstance(latest, bool) and latest >= 0
        if not isinstance(raw, dict) and not measured:
            return None
        context = {"latestRequestInputContext": latest} if measured else {}
        return {**(raw if isinstance(raw, dict) else {}), **context,
                "input_context_measured": measured,
                "source": result.get("usage_source", "unknown"),
                "status": result.get("usage_status", "unknown"),
                "requestCount": result.get("usage_request_count"),
                "limits": result.get("usage_limits")}

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
            "channel": "autonomy" if request_id.startswith("autonomy-") else "conversation",
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
        # Provider-qualified keys prevent sessions from crossing CLI backends.
        # The unqualified key is retained solely for legacy agy files.
        if self.backend_name != "agy":
            key = f"{self.backend_name}:{save_id}"
            if key not in self._sessions and self._sessions.get(f"life:{key}"):
                self._sessions[key] = self._sessions[f"life:{key}"]
                self._save_sessions()
            return self._sessions.get(key)
        return (self._sessions.get(f"agy:{save_id}") or self._sessions.get(save_id)
                or self._sessions.get(f"life:agy:{save_id}"))

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
                    if self.backend_name == "agy":
                        terminate_agy_process(proc)
                    elif callable(terminate):
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
                    await self._prepare_game_load(save_id, disc.get("gameSessionId"))
                    # A stale discovery file is not a live game session. Only
                    # invalidate decisions and wake work after authentication
                    # succeeds, so offline retries cannot mutate saved work.
                    if self._plan_worker is not None:
                        # _prepare_game_load handles real native reloads. A
                        # bridge reconnect in the same game session retains its
                        # persisted authorized selection and reconciles claims.
                        self._plan_worker.supplied_save_id = str(save_id) if save_id else None
                        self._plan_worker.start()
                        self._notify_plan_worker(str(save_id) if save_id else None)
                    await self._receive_loop(ws, save_id, stop_event)
                finally:
                    self.abort_active_task("WebSocket disconnected")
                    await ws.close()

            except (WebSocketError, ConnectionClosed, OSError) as ex:
                logger.warning("Chat WebSocket connection lost (%s): %s. Reconnecting in 2s...", type(ex).__name__, ex)
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

    async def _prepare_game_load(self, save_id: str | None, session_id: str | None) -> None:
        from .game_reload import (
            mark_game_load_applied,
            pending_game_load,
            restore_game_load,
            rotate_provider_sessions,
        )

        if self.run_dir is None or not save_id:
            return
        request = pending_game_load(self.run_dir, save_id, session_id)
        if request is None:
            return
        self.abort_active_task("Game save reloaded")
        self._autonomy_generation += 1
        self._active_player_requests.pop(save_id, None)
        delivery = self._delivery_tasks.pop(save_id, None)
        if delivery is not None and not delivery.done():
            delivery.cancel()
        wakeup = self._autonomy_wakeup_tasks.pop(save_id, None)
        if wakeup is not None and wakeup is not asyncio.current_task():
            wakeup.cancel()
            await asyncio.gather(wakeup, return_exceptions=True)
        self._autonomy_wakeup_snapshots.pop(save_id, None)
        if self._plan_worker is not None:
            await self._plan_worker.stop()
        active = self._active_task.async_task if self._active_task else None
        if active is not None and active is not asyncio.current_task():
            await asyncio.gather(active, return_exceptions=True)
        restore_game_load(self.run_dir, request)
        rotate_provider_sessions(self.run_dir, request, self._sessions, self._sessions_file)
        self._latest_snapshot_payload = None
        self._latest_snapshot_observed_at = None
        self._latest_snapshot_revision = None
        self._current_game_day_key = None
        self._last_day_settlement = None
        self._last_native_ui_recovery_signal = None
        self._evening_care_fired_day = None
        for mapping in (self._last_work_projection, self._work_projection_times,
                        self._pending_native_cancels, self._autonomy_last_snapshot_at,
                        self._autonomy_pending_snapshot, self._autonomy_pending_task_fingerprints):
            mapping.pop(save_id, None)
        self._life_proposals.pop(save_id, None)
        self._bind_autonomy_store()
        self._bind_work_store()
        self._bind_companion_stores()
        if self._life_chat is not None:
            self._life_chat.record_fingerprint(save_id, self._profile_revision(save_id), self._memory_instruction_revision(save_id))
        mark_game_load_applied(self.run_dir, request)
        logger.info("Restored game-save facts with retained player-intent summary for %s", save_id)

    async def _receive_loop(
        self,
        ws: WebSocketClient | None,
        active_save_id: str | None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        tracked_tasks: set[asyncio.Task] = set()
        latest_snapshot: Envelope | None = None
        last_work_push_at = float("-inf")
        self._chat_ws = ws
        self._player_claim_attempted = False
        # Reconnect must first learn the current native actor and authority.
        # Delivery is scheduled after a snapshot/claim acknowledgement, never
        # under the constructor's companion defaults.
        self._latest_snapshot_payload = None

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
                    self._maybe_deliver_outbox(active_save_id)
                    self._schedule_life_drain(ws)
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
                if msg_type == "chat.delivery.ack":
                    payload = data.get("payload") or {}
                    if self._work_store and payload.get("saveId") == active_save_id:
                        self._work_store.acknowledge_outbox_notification(
                            active_save_id, str(payload.get("requestId") or ""),
                            payload.get("outboxIds") or [])
                    continue
                await self._stop_autonomy_if_disabled(ws, active_save_id)
                if msg_type == "world.snapshot":
                    envelope = Envelope.from_mapping(data)
                    if envelope.save_id and active_save_id and envelope.save_id != active_save_id:
                        logger.warning("Chat snapshot save boundary changed: active=%s received=%s",
                                       active_save_id, envelope.save_id)
                        self.abort_active_task("Save switched while chat connection was open")
                        break
                    latest_snapshot = envelope
                    self._player_session_id = envelope.game_session_id
                    self._latest_snapshot_payload = (
                        envelope.payload if isinstance(envelope.payload, dict) else None
                    )
                    self._latest_snapshot_revision = envelope.world_revision
                    self._latest_snapshot_observed_at = (
                        self._latest_snapshot_payload.get("observedAt", envelope.sent_at.timestamp())
                        if self._latest_snapshot_payload is not None else None
                    )
                    self._track_actor_mode(envelope.payload)
                    if self.actor_mode == "player" and not self._player_claim_attempted:
                        await self._maybe_claim_player_control(ws, active_save_id)
                    try:
                        self._write_life_snapshot(active_save_id)
                    except OSError as ex:
                        # A failed local publication leaves the previous cache
                        # available with its original age. It must not tear down
                        # the socket, cancel the model, or drop player controls.
                        logger.warning("Life snapshot cache publication failed (%s): %s", type(ex).__name__, ex)
                    world = envelope.payload.get("world") if isinstance(envelope.payload.get("world"), dict) else {}
                    native = envelope.payload.get("native") or world.get("native")
                    if self.actor_mode == "player" and self._work_store and isinstance(native, dict):
                        signal = (envelope.game_session_id, native.get("eventUp"), native.get("eventIsFestival"),
                                  native.get("activeMenu"), native.get("dialogueUp"), native.get("isQuestionDialogue"))
                        if signal != self._last_native_ui_recovery_signal:
                            self._work_store.observe_native_ui_recovery(active_save_id, native,
                                world_revision=envelope.world_revision, game_session_id=envelope.game_session_id)
                            self._last_native_ui_recovery_signal = signal
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
                    # Care can also generate a model reply. Keep the wire reader
                    # draining snapshots and controls while that reply is pending.
                    task = asyncio.create_task(self._snapshot_care_hooks(
                        active_save_id, world, day_key, previous_day_key,
                    ))
                    tracked_tasks.add(task)
                    task.add_done_callback(tracked_tasks.discard)
                    # A fresh native snapshot is also the trigger for the plan
                    # worker to advance ready steps (and to re-evaluate waiting
                    # todos) without any model turn.
                    self._notify_plan_worker(active_save_id)
                    self._schedule_life_drain(ws)
                    await self._maybe_schedule_autonomy(envelope, active_save_id, tracked_tasks, ws)
                    if active_save_id:
                        # Native observations arrive twice per second. Keep
                        # draining every observation/control, but do not rebuild
                        # the ledger-heavy UI projection for every packet.
                        if time.monotonic() - last_work_push_at >= 2.0:
                            await self._push_work_state(ws, active_save_id)
                            last_work_push_at = time.monotonic()
                        await self._maybe_notify_preset_terminal(ws, active_save_id)
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

                if msg_type == "player.authority":
                    await self._handle_player_authority(
                        ws, data.get("payload") or {}, active_save_id)
                    continue

                if msg_type == "life.chat.submit":
                    # The single-model lock and FIFO queue remain in the handler;
                    # awaiting its whole model turn here stalls the WebSocket.
                    task = asyncio.create_task(
                        self._handle_life_message(ws, msg_type, data, active_save_id)
                    )
                    tracked_tasks.add(task)
                    task.add_done_callback(tracked_tasks.discard)
                    continue

                if msg_type in {
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
                    task.add_done_callback(tracked_tasks.discard)
        finally:
            self.abort_active_task("Chat channel stopped")
            delivery_cleanup = set(self._delivery_tasks.values())
            for delivery in delivery_cleanup:
                if not delivery.done() and not delivery.cancelling():
                    delivery.cancel()
            self._delivery_tasks.clear()
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
            self._pending_life_preparation.clear()
            self._life_draining = False
            for debounce in list(self._autonomy_debounce_tasks.values()):
                debounce.cancel()
            self._autonomy_debounce_tasks.clear()
            self._autonomy_pending_snapshot.clear()
            self._autonomy_wakeup_snapshots.clear()
            cleanup_tasks = tracked_tasks | self._deferred_care_tasks | self._life_drain_tasks | delivery_cleanup
            for task in list(cleanup_tasks):
                if task is not asyncio.current_task() and not task.done() and not task.cancelling():
                    task.cancel()
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            self._autonomy_wakeup_tasks.clear()
            if self._chat_ws is ws:
                self._chat_ws = None

    async def _maybe_schedule_autonomy(
        self, envelope: Envelope, save_id: str | None, tracked_tasks: set[asyncio.Task], ws: WebSocketClient | None,
        *, force: bool = False
    ) -> None:
        """Evaluate the latest native snapshot through the chat channel only."""
        if not save_id or self._autonomy is None or envelope.message_type != "world.snapshot":
            return
        restored = self._restore_accepted_preset_scope(save_id)
        if restored:
            rid, goal_id = restored
            reply = Envelope.create_chat_reply(self.instance_id, rid, "selected", "继续已授权的预设目标。",
                save_id=save_id, command_id=rid, command_complete=False)
            reply.payload["presetGoalId"] = goal_id
            await self._send_reply(ws, reply)
        if self._work_store is not None and ws is not None:
            notice = self._work_store.pending_player_notice(save_id)
            if notice:
                delivered = await self._send_reply(ws, Envelope.create_chat_reply(
                    self.instance_id, notice["id"], "player-decision", notice["message"], save_id=save_id))
                if delivered:
                    self._work_store.acknowledge_player_notice(save_id, notice["id"])
        if self._preset_business_blocked(save_id):
            await self._maybe_notify_preset_terminal(ws, save_id)
            return
        rest_state = ((envelope.payload or {}).get("companion") or {}).get("restState")
        self._autonomy.observe_rest_state(save_id, rest_state)
        self._maybe_arm_hold_recovery(save_id, envelope.payload)
        if self._life_queue or self._life_draining:
            # A player has already been promised a reply. The next snapshot can
            # re-evaluate autonomous work after their queued correction is read.
            return
        # A single queued wakeup owns the latest snapshot until the shared model
        # slot is available. In particular, care cannot consume the morning
        # decision merely by reaching the lock before a created submit task.
        if save_id in self._autonomy_wakeup_tasks:
            self._autonomy_wakeup_snapshots[save_id] = (envelope, tracked_tasks, ws)
            return
        state = self._autonomy.state(save_id)
        if ((not state.enabled and not state.goal_scope) or state.paused
                or self._autonomy.is_cooling_down(save_id)
                or rest_state == "resting"
                or self._bedtime_winding_down(save_id, envelope.payload)):
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
        self._autonomy_wakeup_snapshots[save_id] = (envelope, tracked_tasks, ws)
        generation = self._autonomy_generation

        async def dispatch() -> None:
            request_id = None
            consumed = None
            try:
                async with self._busy_lock:
                    if generation != self._autonomy_generation:
                        return
                    if self._life_queue or self._life_draining:
                        return
                    consumed = self._autonomy_wakeup_snapshots.pop(save_id, None)
                    if consumed is None:
                        return
                    current, _, current_ws = consumed
                    prepared = self._prepare_autonomy_decision(current, save_id)
                    if prepared is None:
                        return
                    fingerprint, prompt = prepared
                    if not self._autonomy.record_world_event(save_id, fingerprint, current.world_revision):
                        return
                    request_id = f"autonomy-{uuid.uuid4().hex}"
                    self._autonomy_requests[request_id] = fingerprint
                    # Keep the claimed slot through submit: queued life waiters
                    # cannot intervene after the wakeup fingerprint is consumed.
                    await self.handle_chat_submit(current_ws, request_id, prompt, save_id, _slot_owned=True)
            finally:
                if request_id:
                    self._autonomy_requests.pop(request_id, None)
                self._autonomy_wakeup_tasks.pop(save_id, None)
                pending = self._autonomy_wakeup_snapshots.pop(save_id, None)
                if pending and generation == self._autonomy_generation:
                    await self._maybe_schedule_autonomy(pending[0], save_id, pending[1], pending[2], force=True)

        task = asyncio.create_task(dispatch())
        self._autonomy_wakeup_tasks[save_id] = task
        tracked_tasks.add(task)
        task.add_done_callback(tracked_tasks.discard)

    def _rearm_after_standby_turn(self, save_id: str) -> None:
        """Re-arm one bounded follow-up decision after a no-dispatch autonomy turn.

        A player-mode turn that ends without selecting native work often carries an
        explicit "next cycle I will ..." intent (one semantic job per decision), so
        consuming the wakeup fingerprint without re-arming dead-ends the run: no
        world change means no new wakeup, and the unattended clock would only be
        stopped by the late-night passout guard. Bumping the decision epoch lets the
        next snapshot evaluation fire a fresh turn. The per-day streak bound stops a
        model that never dispatches from hot-looping; dispatched jobs, day changes,
        pause and breaker cooldown still gate every follow-up independently.
        """
        if getattr(self, "actor_mode", None) != "player" or self._autonomy is None:
            return
        # An explicit future todo is a deliberate world wait, not a planner
        # which forgot to dispatch. Respect it instead of consuming six extra
        # model turns while the decision pause prevents the clock reaching it.
        if self._work_store is not None:
            scoped = self._autonomy.state(save_id).goal_scope
            waiting = [t for t in self._work_store.state(save_id).todos
                       if t.status == "pending" and (not scoped or t.goal_id == scoped)]
            if waiting:
                snapshot, date = self._plan_snapshot_state()
                due = self._work_store.evaluate_todos(save_id, snapshot=snapshot, game_date=date)
                if not any(t.get("id") in {todo.id for todo in waiting} for t in due):
                    logger.info("Standby for %s parked until its native todo condition changes.", save_id)
                    return
        streaks = getattr(self, "_standby_rearm_streak", None)
        if streaks is None:
            streaks = self._standby_rearm_streak = {}
        day_key = self._current_game_day_key or "unknown"
        key = f"{save_id}:{day_key}"
        count = streaks.get(key, 0) + 1
        streaks[key] = count
        if count > 6:
            logger.info(
                "Standby follow-up streak exhausted for %s day %s (%d); waiting for world events.",
                save_id, day_key, count,
            )
            return
        self._autonomy.request_job_decision(save_id)

    @staticmethod
    def _native_recovery_fingerprint(payload: dict[str, Any]) -> str:
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        native = payload.get("native") or world.get("native") or {}
        actor = payload.get("companion") or {}
        return json.dumps({
            "revision": payload.get("capturedRevision"),
            "world": {k: world.get(k) for k in ("year", "season", "dayOfMonth", "timeOfDay", "currentLocation")},
            "actor": {k: actor.get(k) for k in ("locationId", "tileX", "tileY", "stamina", "currentStamina",
                "availableMoney", "waterCanLevel", "water", "waterLeft", "health")},
            "inventory": payload.get("inventory"),
            "ui": {k: native.get(k) for k in ("eventUp", "eventSkippable", "dialogueUp", "dialogueSpeaker",
                "dialogueText", "isQuestionDialogue", "questionResponses", "activeMenu", "endOfNight")},
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _maybe_arm_hold_recovery(self, save_id: str | None, payload: dict[str, Any] | None) -> None:
        """Bound protective recovery by native progress, independently of hold IDs."""
        if not save_id or self._autonomy is None or not isinstance(payload, dict):
            return
        if getattr(self, "actor_mode", None) != "player":
            return
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        native = payload.get("native") or world.get("native")
        hold = native.get("programHold") if isinstance(native, dict) else None
        if not isinstance(hold, dict) or not hold.get("active"):
            return
        hold_id = str(hold.get("holdId") or "")
        if not hold_id:
            return
        seen = getattr(self, "_program_hold_recovery_seen", None)
        if seen is None:
            seen = self._program_hold_recovery_seen = {}
        key = f"{save_id}:{hold_id}"
        if seen.get(key) == "armed":
            return
        if getattr(self, "control_owner", None) == "human":
            if key not in seen:
                seen[key] = "blocked"
                logger.info("Program hold %s for %s observed under human control; no recovery turn armed.", hold_id, save_id)
            return
        state = self._autonomy.state(save_id)
        if (not state.enabled and not state.goal_scope) or state.paused:
            if key not in seen:
                seen[key] = "blocked"
                logger.info("Program hold %s for %s observed while paused/disabled; no recovery turn armed.", hold_id, save_id)
            return
        if self._work_store is not None and self._work_store.state(save_id).paused:
            return
        # Fresh model turns clear a hold, and the night guard creates another.
        # A new hold ID therefore cannot be evidence of native progress. Ignore
        # thinking leases, timestamps, notes and request IDs in this budget.
        progress = self._native_recovery_fingerprint(payload)
        budgets = getattr(self, "_program_hold_progress", None)
        if budgets is None:
            budgets = self._program_hold_progress = {}
        prior = budgets.get(save_id)
        count = prior[1] if prior and prior[0] == progress else 0
        if count >= 3:
            seen[key] = "exhausted"
            self._pause_native_recovery(save_id, "NO_NATIVE_RECOVERY_PROGRESS")
            return
        budgets[save_id] = (progress, count + 1)
        if len(seen) >= 128:
            seen.clear()
        seen[key] = "armed"
        logger.info(
            "Program hold %s for %s active (%s); recovery %d/3 for this native state.",
            hold_id, save_id, hold.get("reason"), count + 1,
        )
        self._autonomy.request_job_decision(save_id)
        # Failed delivery leaves confirmed facts pending, which intentionally
        # blocks fresh autonomy. Recover that delivery first. Regular snapshots
        # can keep receive_text from ever timing out, so its timeout retry is
        # not a reliable recovery boundary. The hold-id gate bounds this retry.
        delivery = self._delivery_tasks.get(save_id)
        if delivery is not None and not delivery.done():
            # The native failure reply can arrive before its delivery coroutine
            # releases the slot. Retry after that coroutine's cleanup callback.
            delivery.add_done_callback(lambda _: self._maybe_deliver_outbox(save_id))
        else:
            self._maybe_deliver_outbox(save_id)

    def _pause_native_recovery(self, save_id: str, reason: str) -> None:
        """Persist a stop for executor faults or repeated recovery without progress."""
        if self._autonomy is not None:
            self._autonomy.control(save_id, "pause")
        if self._work_store is not None:
            self._work_store.set_paused(save_id, True)
            self._work_store.set_outbox_retry_block(save_id, reason)
        logger.error("Native recovery paused for %s: %s; no further automatic model retries.", save_id, reason)

    def _prepare_autonomy_decision(self, envelope: Envelope, save_id: str) -> tuple[str, str] | None:
        """Re-evaluate the latest wakeup while owning the single model slot."""
        state = self._autonomy.state(save_id)
        if (
            (not state.enabled and not state.goal_scope)
            or state.paused
            or self._active_task is not None
            or self._work_settling(save_id)
            or self._autonomy.is_cooling_down(save_id)
        ):
            return
        snapshot = envelope.payload or {}
        if (snapshot.get("companion") or {}).get("restState") == "resting":
            return None  # Daytime recovery wakes again when the native actor is awake.
        if self._bedtime_winding_down(save_id, snapshot):
            return
        if self._work_store is not None:
            job_state = self._work_store.state(save_id)
            if state.goal_scope and not any(g.id == state.goal_scope and g.status == "active" for g in job_state.goals):
                # Finished explicit work must not strand later accepted tasks or
                # prevent idle help from resuming when that preference is on.
                pending_goals = {todo.goal_id for todo in job_state.todos if todo.status in {"pending", "due"}}
                next_goal = next((goal.id for goal in job_state.goals
                                  if goal.status == "active" and goal.source == "user" and goal.id in pending_goals), None)
                self._autonomy.set_goal_scope(save_id, next_goal)
                state = self._autonomy.state(save_id)
                if not state.enabled and not state.goal_scope:
                    return
            decision = job_state.decision
            if decision.get("selected") and not decision.get("finished"):
                selected_task = next((t for t in job_state.tasks if t.id == decision.get("taskId")), None)
                never_dispatched = selected_task is not None and all(
                    s.attempts == 0 and not s.command_id and not s.effects for s in selected_task.steps)
                if decision.get("expires", 0) <= time.time() and never_dispatched:
                    # Expired, unexecuted post-save selections need a fresh choice.
                    # A dispatched/unknown command must retain its reconciliation gate.
                    self._work_store.revoke_decision(save_id)
                else:
                    return
            last_job = job_state.last_job
            # Keep confirmed empty selections parked until native resources,
            # world facts or the accepted objective change their fingerprint.
            if last_job.get("decisionId") and last_job.get("knownNoWork") is not True:
                self._autonomy.request_job_decision(save_id, str(last_job["decisionId"]))
        work_signal = ""
        if self._work_store is not None:
            world = snapshot.get("world") or {}
            due = self._work_store.evaluate_todos(save_id, snapshot=snapshot, game_date={
                "year": world.get("year"), "season": world.get("season"), "day": world.get("dayOfMonth")})
            work_signal = json.dumps({
                # Advisory project notes increment the store epoch but are not
                # new work. Wake only for changed objectives/authorization/state,
                # otherwise a blocked planner could wake itself by editing notes.
                "goals": [(g.id, g.text if g.source == "user" else None, g.priority, g.status,
                           {k: v for k, v in g.constraints.items() if k not in {"milestoneSpec", "objectiveScope"}},
                           objective_scope({"constraints": g.constraints}))
                          for g in job_state.goals if g.status == "active"
                          and (not state.goal_scope or g.id == state.goal_scope)],
                "due": sorted(t["id"] for t in due if not state.goal_scope or t.get("goal_id") == state.goal_scope),
                # Accepted player work needs bounded planning opportunities even
                # after a truthful NO_WORK result with no changed resources.
                # This wakes the model, never selects a business action.
                "planningWindow": (int(world.get("timeOfDay") or 0) // 200
                    if getattr(self, "actor_mode", None) == "player" and state.goal_scope else None),
            }, sort_keys=True)
        candidate = self._autonomy.next_candidate(save_id, snapshot, work_signal=work_signal)
        if candidate is None:
            return
        fingerprint = self._autonomy.fingerprint(save_id, snapshot, candidate, state)
        compact = build_decision_context(
            {"payload": snapshot, "worldRevision": envelope.world_revision},
            work=self._work_context(save_id),
            origin="free-mode" if state.enabled else "accepted-work",
            actor_mode=getattr(self, "actor_mode", None),
        )
        compact["boxRange"] = state.box_preference or "none"
        compact["idlePreference"] = state.idle_preference
        compact["idleHelpEnabled"] = state.enabled
        compact["autonomyMode"] = state.mode
        compact["lastResult"] = self._work_store.state(save_id).last_job if self._work_store else state.last_event_key or "none"
        compact["wakeReason"] = candidate.get("reason", "state-change")
        compact["objective"] = state.goal or "由当前状态决定"
        if state.goal_scope:
            compact["projectScope"] = state.goal_scope
            scoped = next((g for g in job_state.goals if g.id == state.goal_scope), None)
            if scoped:
                compact["goals"] = [goal_context({"id": scoped.id, "text": scoped.text, "source": scoped.source,
                                                  "constraints": scoped.constraints, "epoch": scoped.epoch,
                                                  "project": scoped.project})]
            accepted_node = next((node for node in self._milestone_store.list_nodes(save_id)
                                  if node.get("goalId") == state.goal_scope and node.get("status") == "adopted"), None) if self._milestone_store else None
            if accepted_node:
                compact["acceptedArrangement"] = {"nodeId": accepted_node["id"], "goalId": state.goal_scope,
                    "status": "adopted", "paused": state.paused}
        prompt = (
            "结合当前目标和游戏状态安排下一步。\n"
            f"实时上下文：{render_decision_context(model_decision_context(compact))}"
        )
        return fingerprint, prompt

    def _apply_work_control(self, save_id: str, action: str, params: dict[str, Any]) -> None:
        """Keep durable work scheduling in step with F8 pause/resume/cancel controls."""
        store = self._work_store
        if store is None:
            return
        try:
            if action == "cancel":
                self._active_player_requests.pop(save_id, None)
            if action == "pause":
                store.set_paused(save_id, True)
            elif action == "resume":
                getattr(self, "_program_hold_progress", {}).pop(save_id, None)
                getattr(self, "_preset_resume_progress", {}).pop(save_id, None)
                store.set_paused(save_id, False)
            elif action == "cancel":
                pass  # Cancel the arrangement; preserve the player's pause and mode.
            elif action == "set_mode":
                pass  # Idle initiative is independent of accepted work and pause.
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
            overview = self._latest_work_overview(save_id)
            if overview is None:
                return None
        except Exception:
            return None
        return {
            "goals": [
                {key: g.get(key) for key in ("id", "text", "source", "constraints", "project", "epoch")}
                for g in overview.get("goals", [])[:3]
            ],
            "duePreparation": [
                {"id": t["id"], "intent": t["intent"], "goalId": t.get("goal_id"), "trigger": t.get("trigger")}
                for t in reversed(self._work_store.evaluate_todos(save_id, snapshot=snapshot, game_date=game_date))
            ][:6],
            "nextStep": overview.get("nextStep"),
            "anomalies": overview.get("anomalies", [])[:3],
            "blockedBranches": overview.get("blockedBranches", [])[-5:],
            "tasks": [
                {"id": t.get("id"), "title": t.get("title"), "status": "waiting"}
                for t in overview.get("tasks", [])
                if isinstance(t, dict) and t.get("status") == "waiting"
            ][:3],
            "waitingConditions": overview.get("waitingConditions", [])[:3],
            "lastSettledDay": overview.get("lastSettledDay"),
            "lastJob": overview.get("lastJob"),
            "confirmedExecutionTotals": overview.get("confirmedExecutionTotals"),
            "confirmedRequestTotals": overview.get("confirmedRequestTotals"),
            "plantingEvidence": overview.get("plantingEvidence"),
            "paused": overview.get("paused", False),
            "decision": overview.get("decision", {}),
            "continuity": overview.get("continuity", {}),
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
                "capturedAt": self._latest_snapshot_observed_at,
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
        context = build_decision_context(
            snapshot, work=work, origin=origin, companion=companion, memory=memory,
            actor_mode=getattr(self, "actor_mode", None),
        )
        boundary=getattr(self,"_preset_day_boundary",None)
        if boundary and boundary.enabled and save_id:
            budget=boundary.record(save_id,self._work_store)
            if budget:
                context["presetBudget"]={
                    "runId":budget["runId"],"startDay":budget.get("startDay"),
                    "currentNativeDay":boundary.native_day(self._latest_snapshot_payload),
                    "maxGameDays":budget["maxGameDays"],"phase":budget.get("phase"),
                    "dayBudgetAllowsBusiness":not boundary.blocked(save_id,self._work_store,self._latest_snapshot_payload),
                    "meaning":"External test STOP boundary only, not a business strategy, crop quota or instruction to sleep early. Choose worthwhile daily activities from actual state. This persisted budget supersedes old inferred PRESET_DAY_LIMIT failures; native control and UI admission still apply."}
        from .game_reload import reload_context
        summary = (self._work_reload_context(save_id, self.get_conversation_id(save_id))
                   if self.actor_mode == "player" else reload_context(self.run_dir, save_id))
        if summary:
            context["reloadSummary"] = summary
        return context

    def _latest_work_overview(self, save_id: str | None) -> dict[str, Any] | None:
        if self._work_store is None or not save_id:
            return None
        try:
            snapshot, game_date = self._plan_snapshot_state()
            self._work_store.reconcile_closed_goal_children(save_id)
            overview = self._work_store.overview(save_id, snapshot=snapshot, game_date=game_date)
            state = self._work_store.state(save_id)
            public_intent = []
            if self.actor_mode == "player" and self.run_dir:
                # The game's own save-scoped public replies survive provider
                # rotation. Retain a small fallback even when the model did not
                # explicitly write a decision note; never treat narration as a
                # native receipt or as new player authorization.
                import hashlib
                history_path = self.run_dir / "data/conversations" / (
                    hashlib.sha256(save_id.encode("utf-8")).hexdigest().upper() + ".json")
                try:
                    if history_path.stat().st_size <= 2_000_000:
                        history = json.loads(history_path.read_text(encoding="utf-8"))
                        if history.get("SaveId") == save_id:
                            public_intent = [{"gameDate": row.get("GameDate"),
                                "text": str(row.get("Text") or "").split("\n计划状态：", 1)[0][:600],
                                "source": "public-model-reply", "isVerifiedFact": False}
                                for row in history.get("Entries", [])
                                if not row.get("IsPlayer") and not row.get("RolledBack")
                                and str(row.get("Id") or "").startswith("work:")][-6:]
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            # Provider rotation is a transport concern, not an erasure of intent.
            # Keep bounded source-labelled design/discovery notes independent of
            # active authorization; closed stage goals must not authorize work.
            overview["continuity"] = {
                "recentPublicIntent": public_intent,
                "historicalDesignNotes": [{"goalId": g.id, "parentId": g.parent_id,
                    "status": g.status, "source": g.source, "recordedAt": g.updated_at,
                    "text": g.text[:900], "requiresWorldRevalidation": True}
                    for g in state.goals if g.kind != "main"][-4:],
                "recentIntent": [{"goalId": g.id, "phase": g.project.get("phase"),
                    "nextAction": g.project.get("nextAction"),
                    "progress": (g.project.get("progressLog") or [])[-3:],
                    "decisionNotes": (g.project.get("decisionNotes") or [])[-8:]}
                    for g in state.goals if g.kind == "main" and g.status == "active"][:1],
                "failedBranches": overview.get("blockedBranches", [])[-6:],
                "meaning": "Retained notes are dated observations/design hypotheses, not active permission or proof of current world. Current native observations override them. Preserve useful siting/discoveries and avoid repeating a failed branch without new relevant evidence."
            }
            return overview
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

    def _memory_instruction_revision(self, save_id: str | None) -> int:
        """Only corrections to agreements/preferences invalidate provider history."""
        if self._memory_store is None or not save_id:
            return 0
        return self._memory_store.instruction_revision(save_id)

    def _life_work_projection(self, save_id: str) -> dict[str, Any]:
        """Read-only autonomy + WorkStore projection for life.profile.state (§1.3)."""
        payload: dict[str, Any] = {}
        overview = self._latest_work_overview(save_id) or {}
        if self._autonomy is not None:
            try:
                payload = self._autonomy_state_payload(save_id, self._autonomy.state(save_id), overview=overview)
            except Exception:
                payload = {}
        preferences = payload.get("preferences") if isinstance(payload.get("preferences"), dict) else {}
        autonomy_state = self._autonomy.state(save_id) if self._autonomy else None
        current_goal_id = autonomy_state.goal_scope if autonomy_state else None
        active_goals = [g for g in overview.get("goals", []) if isinstance(g, dict) and g.get("status") == "active"]
        if current_goal_id not in {g.get("id") for g in active_goals}:
            current_goal_id = (overview.get("decision") or {}).get("goalId")
        if current_goal_id not in {g.get("id") for g in active_goals}:
            current_goal_id = None
        active_goals.sort(key=lambda g: (
            g.get("id") == current_goal_id,
            bool(g.get("source") == "user" and g.get("project")),
            str(g.get("updated_at") or g.get("created_at") or "")), reverse=True)
        current_goal = next((g for g in active_goals if g.get("id") == current_goal_id), None)
        goals = [
            {"id": g.get("id"), "text": g.get("text"),
             "summary": (g.get("project") or {}).get("summary") if isinstance(g.get("project"), dict) else None,
             "status": g.get("status")}
            for g in active_goals
        ][:8]
        todos: list[dict[str, Any]] = []
        if self._work_store is not None:
            try:
                todos = [
                    {"id": t.get("id"), "intent": t.get("intent"), "status": t.get("status")}
                    for t in self._work_store.list_todos(save_id)
                    if isinstance(t, dict) and t.get("status") == "pending"
                ][:5]
            except Exception:
                todos = []
        pause_reason = None
        if autonomy_state and autonomy_state.breaker_tripped:
            code = autonomy_state.breaker_reason or "PROVIDER_FAILED"
            pause_reason = "模型服务连续失败，安排已保留；恢复服务后可继续。原因：" + str(code)
        elif overview.get("paused"):
            pause_reason = "工作已暂停，等待你继续。"
        elif payload.get("planWaitReason"):
            pause_reason = str(payload["planWaitReason"])
        projection = {
            "mode": payload.get("mode", "command"),
            "paused": bool(overview.get("paused", False)),
            "goal": current_goal.get("text") if current_goal else preferences.get("goal"),
            "currentGoalId": current_goal_id,
            "pauseReason": pause_reason,
            "boxPreference": preferences.get("boxPreference"),
            "dailySpend": payload.get("dailySpend"),
            "hasExecutableWork": bool(overview.get("hasExecutableWork", False)),
            "lastPlanAction": payload.get("lastPlanAction"),
            "planWaitReason": payload.get("planWaitReason"),
            "lastSettledDay": overview.get("lastSettledDay"),
            "activeGoals": goals,
            "recentTodos": todos,
            "recentExecutions": self._work_store.recent_execution_summary(save_id) if self._work_store else [],
            "playerDecisions": [{"id": n["id"], "message": n["message"], "goalId": n.get("goalId"),
                                 "choices": n.get("choices", []),
                                 "status": "answered" if n.get("answeredAt") else "dismissed" if n.get("dismissedAt") else "pending"}
                                for n in (self._work_store.state(save_id).notices if self._work_store else [])],
            # Contract §1.3 declares [str]; rows from WorkStore.wait_conditions()
            # carry objects, so surface only the display description.
            "waitingConditions": [
                str(row.get("waitDescription") or row.get("reasonCode") or "等待条件变化")
                for row in (overview.get("waitingConditions") or [])[:5]
                if isinstance(row, dict)
            ],
            "activity": self._player_activity(save_id),
        }
        signature = json.dumps(projection, sort_keys=True, ensure_ascii=False)
        previous = self._work_projection_times.get(save_id)
        if previous is None or previous[0] != signature:
            previous = (signature, datetime.datetime.now(datetime.UTC).isoformat())
            self._work_projection_times[save_id] = previous
        projection["updatedAt"] = previous[1]
        return projection

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
        def task_title(task_id: str | None) -> str | None:
            if not state or not task_id:
                return None
            current = next((task.title for task in state.tasks if task.id == task_id), None)
            if current:
                return current
            return next((entry["task"].get("title") for entry in reversed(state.archive)
                         if isinstance(entry.get("task"), dict) and entry["task"].get("id") == task_id), None)

        def saved_next_step() -> str:
            if not state:
                return "暂无可用的后续任务记录。"
            goals = [goal for goal in state.goals if goal.status == "active"
                     and (not autonomy or not autonomy.goal_scope or goal.id == autonomy.goal_scope)]
            last = state.last_job
            last_task = next((task for task in state.tasks if task.id == last.get("taskId")), None)
            last_record = next((entry["task"] for entry in reversed(state.archive)
                                if isinstance(entry.get("task"), dict) and entry["task"].get("id") == last.get("taskId")), {})
            last_goal = last_task.goal_id if last_task else last_record.get("goal_id")
            result_at = last_task.updated_at if last_task else last_record.get("updated_at")
            for goal in goals:
                next_action = goal.project.get("nextAction")
                # A pre-execution intention stops being a next step when its
                # job completes. Use it again only after a fresh plan revision.
                stale = (last.get("status") == "completed" and last_goal == goal.id
                         and result_at and goal.updated_at <= result_at)
                if not stale and isinstance(next_action, str) and next_action.strip():
                    return next_action.strip()[:160]
            pending = [todo for todo in state.todos if todo.status == "pending"
                       and (not autonomy or not autonomy.goal_scope or todo.goal_id == autonomy.goal_scope)]
            if not pending:
                return "依据这次实际结果安排下一步，新的动作确定后会在这里显示。"
            _, current_date = self._plan_snapshot_state()
            seasons = {"spring": 0, "summer": 1, "fall": 2, "winter": 3}
            def calendar_key(value: dict[str, Any] | None) -> tuple[int, int, int] | None:
                if isinstance(value, dict) and type(value.get("year")) is int and type(value.get("day")) is int and value.get("season") in seasons:
                    return value["year"], seasons[value["season"]], value["day"]
                return None
            today = calendar_key(current_date)
            # An old unfinished reminder is retained for the agent, but a
            # current/future appointment is more useful in the player's next step.
            dated = [(key, todo) for todo in pending if (key := calendar_key(todo.trigger)) is not None
                     and today is not None and key >= today]
            todo = min(dated, key=lambda row: row[0])[1] if dated else pending[0]
            trigger = todo.trigger
            date = ""
            if trigger.get("type") in {"calendar", "gameDay", "gameTime"}:
                season = {"spring": "春", "summer": "夏", "fall": "秋", "winter": "冬"}.get(trigger.get("season"), "")
                date = f"{season}{trigger.get('day')}日"
                # An unfinished old reminder stays a pending fact, but its
                # original date must not appear as an upcoming appointment.
                due_date = calendar_key(trigger)
                if due_date is not None and today is not None and due_date < today:
                    date = ""
            if "鸡舍" in todo.intent or "养鸡" in todo.intent:
                return f"{date or '接下来'}核对鸡舍进度，再继续养鸡安排。"
            if trigger.get("type") == "machineReady":
                return "加工完成后收取成品，继续已约定的入箱安排。"
            if any(word in todo.intent for word in ("种植", "播种", "浇水", "作物", "种子")):
                return f"{date + '：' if date else '接下来：'}照料菜地，继续未完成的种植安排。"
            intent = re.split(r"[。；;，,]", todo.intent, maxsplit=1)[0][:65]
            return f"{date + '：' if date else '接下来：'}{intent}。"

        if (state and state.paused) or (autonomy and autonomy.paused):
            return activity("paused", "伙伴工作已暂停，已保存的安排仍保留。", "准备好后点继续；不会自行解除暂停。")
        decision = state.decision if state else {}
        selected = next((task for task in state.tasks if task.id == decision.get("taskId")), None) if state else None
        if selected and selected.status == "waiting":
            waits = self._work_store.wait_conditions(save_id)
            reason = next((row.get("waitDescription") for row in waits if row.get("taskId") == selected.id), None)
            return activity("waiting", f"{selected.title}：{reason or '正在等待执行条件'}", "条件满足后继续；可以暂停或取消。")
        live_selected = selected if selected and selected.status in {"pending", "running", "waiting"} and not decision.get("finished") else None
        native_running = next((task for task in state.tasks if task.status == "running"
                               and any(step.command_id and step.status == "running" for step in task.steps)), None) if state else None
        current_work = native_running or live_selected
        if current_work:
            title = current_work.title
            if title:
                return activity("working", f"正在处理：{title}", saved_next_step())
        if decision.get("selected") and not decision.get("finished"):
            return activity("working", "已接手当前安排，暂无可用的任务名称记录。", saved_next_step())
        if self._active_task and self._active_task.save_id == save_id and not self._active_task.cancelled:
            return activity("planning", "正在根据农场情况安排下一步，暂未开始新的动作。", saved_next_step())
        proposal = self._current_life_proposal(save_id)
        if proposal:
            return activity("proposed", proposal.get("summary") or proposal["title"], "认可后接手可做的准备；也可以继续商量。")
        nodes = self._milestone_store.list_nodes(save_id) if self._milestone_store else []
        terminal_goals = {goal.id for goal in state.goals if goal.status in {"completed", "cancelled"}} if state else set()
        adopted = max((node for node in nodes if node.get("status") == "adopted"
                       and node.get("goalId") not in terminal_goals),
                      key=lambda node: node.get("updatedAt", 0), default=None)
        last = state.last_job if state else {}
        last_task_id = last.get("taskId")
        last_task = next((task for task in state.tasks if task.id == last_task_id), None) if state else None
        archived_task = next((entry["task"] for entry in reversed(state.archive)
                              if isinstance(entry.get("task"), dict) and entry["task"].get("id") == last_task_id), {}) if state else {}
        last_goal_id = last.get("goalId") or (last_task.goal_id if last_task else archived_task.get("goal_id"))
        if last and (adopted is None or last_goal_id == adopted.get("goalId")):
            title = last.get("taskTitle") or task_title(last_task_id)
            if not title:
                return activity("idle", "暂无可用的任务名称记录。", saved_next_step())
            if last.get("status") == "completed":
                effects = ([effect for step in last_task.steps for effect in step.effects] if last_task else
                           [effect for step in archived_task.get("steps", []) for effect in step.get("effects", [])]) or last.get("effects") or []
                result = self._work_store.effect_summary(effects) if effects else "已完成"
                return activity("completed", f"{title}：{result}。", saved_next_step())
            if last.get("status") in {"failed", "partial", "unknown", "rejected", "cancelled"}:
                reason_code = last.get("reasonCode")
                if not reason_code and state:
                    reason_code = next((entry.reason_code for entry in reversed(state.executions)
                                        if entry.task_id == last_task_id and entry.reason_code), None)
                detail = WorkStore.reason_summary(reason_code, last.get("message") or last.get("error"))
                return activity("failed", f"{title}：{detail}", f"重新核对「{title}」的执行条件，再决定是否重试。")
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
        if mode in {"chat", "plan"}:
            # Plan discussions see the full read-only projection; it never
            # dispatches anything.
            # Resolved notice history stays in the UI projection, while the
            # provider only needs still-pending questions in its live context.
            return {**projection, "playerDecisions": [n for n in projection.get("playerDecisions", []) if n["status"] == "pending"]}
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
        """Compact milestone snapshot shared by life conversation prompts."""
        if mode not in {"chat", "plan"} or self._milestone_store is None or not save_id:
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
                await self._push_usage_state(ws, save_id)
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
            # Layering fields arrive from the MCP/life wire when the caller
            # supplies them; absent means "default by kind" (event→fact,
            # agreement/preference→constraint).
            status, result = self._memory_store.add(
                save_id, kind=payload.kind, text=payload.text, source="player",
                game_date=game_date, expected_revision=payload.expected_revision,
                category=getattr(payload, "category", None),
                valid_from=getattr(payload, "valid_from", None),
                valid_to=getattr(payload, "valid_to", None),
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

    # Player-mode late-night dispatch policy (game clock, HHMM):
    # the companion profile bedtime does not apply to the native player; the
    # player ends the day natively with the sleep tool, so late gates only
    # steer new dispatches toward getting home instead of idling into collapse.
    PLAYER_NEAR_MIDNIGHT = 2330
    PLAYER_DEFER_NON_SLEEP = 2330
    PLAYER_SLEEP_ONLY = 2530

    @staticmethod
    def _player_mode_clock_gate(operation: str, clock: Any, params: dict[str, Any] | None = None) -> str | None:
        """Reserve the remaining native night for rest, never freeze an action."""
        if type(clock) is not int or clock < ChatBridge.PLAYER_DEFER_NON_SLEEP:
            return None
        if operation in {"sleep", "native_advance", "native_answer"} or operation.startswith(("observe_", "query_", "inspect_", "get_")):
            return None
        if operation == "navigate_to" and (params or {}).get("location_id") in {"Farm", "FarmHouse"}:
            return None
        return ("late-night: from 23:30 new unrelated work/travel waits until tomorrow. "
                "Use sleep for the native route to the player's own bed, confirmation and save; "
                "another NPC's bed/tent is not the player's sleep destination. "
                "Read-only observations and returning to Farm/FarmHouse remain available. "
                "Actions still consume normal game time; waiting until 01:30 can cause collapse en route.")

    def _bedtime_winding_down(self, save_id: str, snapshot: dict[str, Any] | None = None) -> bool:
        # Player mode has no companion profile bedtime (and the player-mode
        # snapshot omits restState/bedtime entirely): winding-down gates stay
        # companion-only so the agent can keep steering the player home.
        if getattr(self, "actor_mode", None) == "player":
            return False
        snapshot = snapshot or self._latest_snapshot_payload or {}
        companion = snapshot.get("companion") or {}
        if companion.get("restState") in {"winding-down", "returning-home", "waiting-for-bed", "sleeping"}:
            return True
        profile = self._profile_store.get(save_id).get("profile") if self._profile_store else None
        bedtime = (profile or {}).get("bedtime", 2400)
        now = (snapshot.get("world") or {}).get("timeOfDay", 600)
        # Reserve an hour for the current safe action and the actual route home.
        return isinstance(now, int) and now // 100 * 60 + now % 100 >= bedtime // 100 * 60 + bedtime % 100 - 60

    def _near_midnight_hint(self, snapshot: dict[str, Any] | None = None) -> str | None:
        """Player-mode 'approaching midnight' context note (≥ 23:30)."""
        if getattr(self, "actor_mode", None) != "player":
            return None
        snapshot = snapshot or self._latest_snapshot_payload or {}
        clock = (snapshot.get("world") or {}).get("timeOfDay")
        if isinstance(clock, int) and not isinstance(clock, bool) and clock >= self.PLAYER_NEAR_MIDNIGHT:
            return "临近午夜：优先安排回屋用 sleep 工具原生睡觉结束当天，避免在外昏倒。"
        return None

    def _write_life_snapshot(self, save_id: str | None) -> None:
        if self.run_dir is None or not save_id or self._latest_snapshot_payload is None:
            return
        path = self.run_dir / "data" / "life-snapshot.json"
        if self._latest_snapshot_payload.get("observedAt") is not None:
            self._latest_snapshot_observed_at = self._latest_snapshot_payload["observedAt"]
        elif self._latest_snapshot_observed_at is None:
            # Older native packages omit observedAt. Retain their first observed
            # time; ordinary cache writes cannot renew the observation's age.
            self._latest_snapshot_observed_at = self._latest_snapshot_payload.get("observedAt", time.time())
        write_cache(path, {"saveId": save_id, "capturedAt": self._latest_snapshot_observed_at,
            "worldRevision": self._latest_snapshot_revision,
            "payload": self._latest_snapshot_payload})

    def _life_turn_lease(self, request_id: str | None, save_id: str, *, player_text: str | None = None,
                         notice_id: str | None = None) -> None:
        if self.run_dir is None:
            return
        path = self.run_dir / "data" / "life-turn.json"
        write_cache(path, {"requestId": request_id, "saveId": save_id, "playerText": player_text, "noticeId": notice_id})

    async def _apply_life_intent(self, ws: WebSocketClient | None, request_id: str, save_id: str) -> None:
        if self.run_dir is None or not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
            return
        directory = self.run_dir / "data" / "life-controls"
        # Each tool commit owns a file; consuming one can never delete a newer
        # action. Nanosecond prefixes preserve the sequential MCP call order.
        paths = sorted(directory.glob(request_id + "--*.json"))
        legacy = directory / (request_id + ".json")
        if legacy.is_file():
            paths.insert(0, legacy)
        for path in paths:
            try:
                intent = json.loads(path.read_text(encoding="utf-8"))
                lease_path = self.run_dir / "data" / "life-turn.json"
                with cache_lock(lease_path):
                    lease = json.loads(lease_path.read_text(encoding="utf-8"))
                    if (intent.get("action") in {"pause", "cancel"} and intent.get("saveId") == save_id
                            and lease.get("saveId") == save_id and lease.get("requestId") == request_id):
                        lease["workBlocked"] = True
                        temp = lease_path.with_suffix(".tmp")
                        temp.write_text(json.dumps(lease), encoding="utf-8")
                        temp.replace(lease_path)
            finally:
                path.unlink(missing_ok=True)
            if (intent.get("saveId") != save_id or lease.get("requestId") != request_id
                    or lease.get("saveId") != save_id):
                continue  # A newer player control invalidated this delayed response.
            action = intent.get("action")
            if action == "bedtime" and self._profile_store is not None:
                self._profile_store.set(save_id, {"bedtime": intent.get("bedtime")}, self._profile_revision(save_id))
                profile = self._profile_store.get(save_id)
                await self._send_reply(ws, Envelope.create_life_profile_state(
                    self.instance_id, "", save_id, profile.get("profile"), profile["profileRevision"],
                    work=self._life_work_projection(save_id)))
            elif action in {"pause", "resume", "cancel"}:
                envelope = Envelope.create_autonomy_control(self.instance_id, request_id, save_id, action,
                    goalId=intent.get("goalId"), allArrangements=bool(intent.get("allArrangements")))
                await self._handle_autonomy_control(ws, envelope, save_id, preserve_life_request_id=request_id)
                # Own controls may be followed by another preference/control in
                # the same sentence; stopped work still cannot be re-adopted.
                # Sending the acknowledgement can yield to a newer player
                # control. Never resurrect that invalidated conversation lease.
                lease_path = self.run_dir / "data" / "life-turn.json"
                with cache_lock(lease_path):
                    lease = json.loads(lease_path.read_text(encoding="utf-8"))
                    if lease.get("requestId") == request_id and lease.get("saveId") == save_id:
                        lease["workBlocked"] = action != "resume"
                        temp = lease_path.with_suffix(".tmp")
                        temp.write_text(json.dumps(lease), encoding="utf-8")
                        temp.replace(lease_path)

    async def _replay_answered_decision(self, ws, save_id: str, notice_id: str | None,
                                        request_id: str, resolve: bool) -> bool:
        """A receipt retry must never re-enter controls or the model slot."""
        if not notice_id or not resolve or self._work_store is None:
            return False
        notice = next((n for n in self._work_store.state(save_id).notices
                       if n.get("id") == notice_id and n.get("answeredAt")), None)
        if notice is None:
            return False
        await self._send_reply(ws, Envelope.create_life_chat_reply(
            self.instance_id, request_id, save_id, status="completed", answered_notice_id=notice_id,
            reply_text="这件事已经确认，保留原来的选择：" + str(notice.get("answer", "")),
            profile_revision=self._profile_revision(save_id), memory_revision=self._memory_revision(save_id)))
        return True

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
        if active_save_id and save_id != active_save_id:
            await self._send_life_error(ws, "life.chat.submit", data, active_save_id, "SAVE_MISMATCH")
            return
        if payload.notice_action:
            try:
                if self._work_store is None:
                    raise ValueError("Work store is unavailable")
                self._work_store.set_notice_visibility(save_id, list(payload.notice_ids), payload.notice_action == "dismiss")
                await self._send_reply(ws, Envelope.create_life_chat_reply(
                    self.instance_id, payload.request_id, save_id, status="completed",
                    reply_text="已隐藏这些事项。" if payload.notice_action == "dismiss" else "已恢复这些事项。",
                    profile_revision=self._profile_revision(save_id), memory_revision=self._memory_revision(save_id)))
                await self._handle_life_message(ws, "life.profile.get", {"requestId": payload.request_id, "saveId": save_id}, save_id)
            except (ValueError, WorkStateError) as ex:
                await self._send_life_error(ws, "life.chat.submit", data, save_id, str(ex))
            return
        if await self._replay_answered_decision(ws, save_id, payload.reply_to_notice_id, payload.request_id, payload.resolve_notice):
            return
        if payload.reply_to_notice_id:
            pending = self._work_store.unresolved_player_notices(save_id) if self._work_store else []
            if not any(n.get("id") == payload.reply_to_notice_id for n in pending):
                await self._send_life_error(ws, "life.chat.submit", data, save_id, "DECISION_NO_LONGER_PENDING")
                return
        control = self._control_utterance(payload.text)
        if control:
            if self._work_store is not None:
                self._work_store.record_request(save_id, payload.request_id, payload.text, origin="player-midflight")
            params = {"allArrangements": True} if explicit_cancel_scope(payload.text) == "all" else {}
            envelope = Envelope.create_autonomy_control(self.instance_id, payload.request_id, save_id, control, **params)
            confirmed = await self._handle_autonomy_control(ws, envelope, save_id)
            if confirmed is False:
                await self._send_life_error(ws, "life.chat.submit", data, save_id, "CONTROL_NOT_CONFIRMED")
                return
            if payload.reply_to_notice_id and payload.resolve_notice and self._work_store:
                self._work_store.answer_player_notice(save_id, payload.reply_to_notice_id, payload.text, payload.request_id)
            await self._send_reply(ws, Envelope.create_life_chat_reply(
                self.instance_id, payload.request_id, save_id, status="completed",
                answered_notice_id=payload.reply_to_notice_id if payload.resolve_notice else None,
                reply_text={"resume": "好，接着做。", "pause": "好，先停下来。", "cancel": "好，取消这项安排。"}[control],
                profile_revision=self._profile_revision(save_id), memory_revision=self._memory_revision(save_id)))
            return
        # Record before a queued life turn can outlive the native job that was
        # in progress when the player spoke. Both UI and legacy chat use this ledger.
        midflight = self._job_in_flight(save_id)
        if self._work_store is not None and payload.text.strip():
            self._work_store.record_request(save_id, payload.request_id, payload.text,
                                            origin="player-midflight" if midflight else "player")
        item = {
            "ws": ws,
            "request_id": payload.request_id,
            "save_id": save_id,
            "mode": payload.mode,
            "text": payload.text,
            "accepted_node_id": payload.accepted_node_id,
            "midflight": midflight,
            "reply_to_notice_id": payload.reply_to_notice_id,
            "resolve_notice": payload.resolve_notice,
            "control_generation": self._life_control_generation.get(save_id, 0),
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

    def _schedule_life_drain(self, ws: WebSocketClient | None) -> None:
        """Wake deferred dialogue/work without tying up the snapshot reader."""
        if (not (self._life_queue or self._pending_life_preparation)
                or self._life_draining or self._busy_lock.locked() or self._active_task
                or any(not task.done() for task in self._life_drain_tasks)):
            return
        task = asyncio.create_task(self._drain_life_queue(ws))
        self._life_drain_tasks.add(task)
        task.add_done_callback(self._life_drain_tasks.discard)

    async def _drain_life_queue(self, ws: WebSocketClient | None) -> None:
        """Process queued life submits FIFO once the model slot is free."""
        if self._life_draining:
            return
        self._life_draining = True
        try:
            while self._life_queue or self._pending_life_preparation:
                if self._busy_lock.locked() or (
                    self._active_task and not self._active_task.cancelled
                ):
                    # Still busy: leave the queue for the next turn's drain trigger.
                    return
                if self._life_queue:
                    item = self._life_queue.pop(0)
                    await self._run_life_chat_turn(ws or item.get("ws"), item)
                else:
                    save_id = next(iter(self._pending_life_preparation))
                    before = self._pending_life_preparation.pop(save_id)
                    try:
                        await self._start_accepted_preparation(ws, save_id, before)
                        if save_id in self._pending_life_preparation and not self._life_queue:
                            # A native result, resume, or a new day will wake us.
                            # Release this gate so the worker/outbox can progress.
                            return
                    except Exception:
                        logger.exception("Accepted preparation failed for %s", save_id)
                        await self._send_reply(ws, Envelope.create_chat_reply(
                            self.instance_id, f"preparation-error-{uuid.uuid4().hex}", "failed",
                            "安排已保存，但准备工作时出错了，尚未确认开始执行。", save_id=save_id,
                            command_complete=True))
        finally:
            self._life_draining = False

    def _prepare_prompt_delivery(self, active_task: ActiveChatTask, conversation_id: str | None,
                                prompt: str, backend: Any) -> tuple[str, str, dict[str, Any]]:
        if not hasattr(self, "_prompt_delivery"):
            delivery_root = Path(self.run_dir) if self.run_dir else Path(self._sessions_file).parent
            self._prompt_delivery = PromptDelivery(delivery_root / "prompt-delivery.json")
        key = f"{self.backend_name}:{active_task.save_id}:{getattr(self, 'actor_mode', '')}:{(self._latest_snapshot_payload or {}).get('actorMultiplayerId', 'unknown')}"
        key += f":{getattr(self, '_player_session_id', None) or 'unknown-session'}"
        # All event sources share one state schema. The player suffix is opaque.
        marker = re.search(r"(?m)^(?:玩家说：|玩家指令：)", prompt)
        boundary = marker.start() if marker else len(prompt)
        prefix, player = prompt[:boundary], prompt[boundary:]
        context = model_decision_context(self._decision_context(active_task.save_id, origin="game"))
        context["workDispatchAvailable"] = not active_task.read_only
        if self._profile_store and active_task.save_id:
            context["profile"] = (self._profile_store.get(active_task.save_id) or {}).get("profile")
        context["milestones"] = self._life_milestone_summary(active_task.save_id, "chat")
        # Event metadata is not world state: retain it when canonicalizing snapshots.
        event = {}
        for match in re.finditer(r"(?m)^实时上下文：([^\n]*)", prefix):
            original = json.loads(match.group(1))
            for event_key in ("projectScope", "objective", "wakeReason", "acceptedArrangement",
                          "boxRange", "idlePreference", "idleHelpEnabled", "autonomyMode"):
                if event_key in original:
                    event[event_key] = original[event_key]
        decision = context.get("decision") or {}
        event.update(requestId=active_task.request_id, workDispatchAvailable=not active_task.read_only)
        event["goalId"] = decision.get("goalScope") or decision.get("goalId") or active_task.goal_scope
        # These are already represented in the current state; don't inject duplicates from reload.
        if isinstance(context.get("reloadSummary"), dict):
            context["reloadSummary"] = {k: v for k, v in context["reloadSummary"].items()
                                        if k not in {"goals", "agreementsAndPreferences", "decisions"}}
        replacement = "实时上下文：" + render_decision_context(context)
        prefix = re.sub(r"(?m)^实时上下文：[^\n]*", lambda _: replacement, prefix)
        prompt = prefix + "\n本轮任务：" + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" + player
        sent, pending = self._prompt_delivery.prepare(
            key, conversation_id, prompt,
            load_instruction_bundle(getattr(self, "actor_mode", None)).text)
        logger.info("Turn payload stateBytes=%s eventBytes=%s sentBytes=%s",
                    len(replacement.encode()), len(json.dumps(event, ensure_ascii=False).encode()), len(sent.encode()))
        return key, sent, pending

    def _acknowledge_prompt_delivery(self, key: str, pending: dict[str, Any],
                                     save_id: str, session: str) -> None:
        self._prompt_delivery.acknowledge(key, pending, session)

    def _execute_life_turn(
        self,
        active_task: ActiveChatTask,
        conversation_id: str | None,
        prompt: str,
        mode: str = "chat",
    ) -> dict[str, Any]:
        """Legacy UI adapter; conversation and work use the same model round."""
        keys = ("STARDEW_LIFE_SAVE_ID", "STARDEW_LIFE_TURN_ID", "STARDEW_LIFE_MODE")
        previous = {key: os.environ.get(key) for key in keys}
        os.environ.update(dict(zip(keys, (active_task.save_id or "", active_task.request_id, mode), strict=True)))
        start = get_max_gen_idx(conversation_id) if self.backend_name == "agy" else -1
        active_task.start_max_idx = start
        active_task.start_max_step_idx = get_max_step_idx(conversation_id) if self.backend_name == "agy" else -1
        offset = wire_offset(conversation_id, cwd=project_root()) if self.backend_name == "kimi" else 0
        try:
            result = self._execute_turn(active_task, conversation_id, prompt)
            cid = result.get("conversation_id") or conversation_id
            if self.backend_name == "agy":
                result["usage"] = (get_command_usage_delta(cid, start if cid == conversation_id else -1)
                                   if cid else None) or result.get("usage")
            elif self.backend_name == "kimi":
                result["usage"] = read_usage_since(cid, offset, cwd=project_root())
            return result
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    async def _join_interrupted_provider(self, active_task: ActiveChatTask, future: asyncio.Future) -> None:
        """Keep the model slot until its executor has restored turn-local state."""
        active_task.cancelled = True
        active_task.abort_reason = active_task.abort_reason or "Chat channel closed"
        proc = active_task.process
        if proc is not None and proc.poll() is None:
            try:
                terminate = getattr(self._backend, "terminate", None)
                if self.backend_name == "agy":
                    terminate_agy_process(proc)
                elif callable(terminate):
                    terminate(proc)
                else:
                    proc.kill()
            except Exception:
                logger.warning("Could not stop interrupted provider", exc_info=True)
        while True:
            try:
                await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                # Repeated cancel/disconnect requests still cannot abandon an
                # executor which owns process-global turn environment variables.
                if future.cancelled():
                    break
            except Exception:
                logger.debug("Interrupted provider exited with an error", exc_info=True)
                break

    async def _await_work_provider(
        self, active_task: ActiveChatTask, conversation_id: str | None, prompt: str,
    ) -> dict[str, Any]:
        future = asyncio.get_running_loop().run_in_executor(
            None, self._execute_turn, active_task, conversation_id, prompt)
        try:
            async with self._thinking_pause(active_task.request_id, active_task.save_id or None):
                return await asyncio.shield(future)
        except asyncio.CancelledError:
            await self._join_interrupted_provider(active_task, future)
            raise

    async def _await_life_provider(
        self, active_task: ActiveChatTask, conversation_id: str | None,
        prompt: str, mode: str | None = None,
    ) -> dict[str, Any]:
        """Keep provider ownership until its thread has restored turn-local state."""
        args = (active_task, conversation_id, prompt)
        if mode is not None:
            args += (mode,)
        future = asyncio.get_running_loop().run_in_executor(None, self._execute_life_turn, *args)
        try:
            async with self._thinking_pause(active_task.request_id, active_task.save_id or None):
                while not future.done():
                    await asyncio.wait({future}, timeout=0.1)
                    await self._apply_life_intent(self._chat_ws, active_task.request_id, active_task.save_id or "")
                return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Cancelling an executor await does not stop the executor thread.
            # Wait for this owned turn before releasing the model/native slot.
            await self._join_interrupted_provider(active_task, future)
            logger.info("Life provider [%s] interrupted by channel shutdown", active_task.request_id)
            raise

    async def _run_life_chat_turn(self, ws: WebSocketClient | None, item: dict[str, Any]) -> None:
        """Run one life-chat turn under the shared single-model-turn lock."""
        request_id = str(item.get("request_id") or "")
        save_id = str(item.get("save_id") or "")
        mode = str(item.get("mode") or "chat")
        text = str(item.get("text") or "")
        try:
            generation = item.get("control_generation", self._life_control_generation.get(save_id, 0))
            if generation != self._life_control_generation.get(save_id, 0):
                await self._send_reply(ws, Envelope.create_life_chat_reply(
                    self.instance_id, request_id, save_id, status="completed", reply_text="这条旧请求已被新的工作控制撤销。",
                    profile_revision=self._profile_revision(save_id), memory_revision=self._memory_revision(save_id)))
                return
            if item.get("accepted_node_id"):
                await self._accept_life_proposal(ws, item)
                return
            async with self._busy_lock:
                if generation != self._life_control_generation.get(save_id, 0):
                    await self._send_reply(ws, Envelope.create_life_chat_reply(
                        self.instance_id, request_id, save_id, status="completed", reply_text="这条旧请求已被新的工作控制撤销。",
                        profile_revision=self._profile_revision(save_id), memory_revision=self._memory_revision(save_id)))
                    return
                if await self._replay_answered_decision(ws, save_id, item.get("reply_to_notice_id"), request_id, bool(item.get("resolve_notice"))):
                    return
                profile = None
                if self._profile_store is not None and save_id:
                    profile = (self._profile_store.get(save_id) or {}).get("profile")
                memory_render = (
                    self._memory_store.render_for_context(save_id)
                    if self._memory_store is not None and save_id
                    else None
                )
                existing_cid = None
                session_profile_revision = self._profile_revision(save_id)
                session_instruction_revision = self._memory_instruction_revision(save_id)
                if self._life_chat is not None and save_id:
                    existing_cid = self._life_chat.rotate_if_needed(
                        save_id,
                        session_profile_revision,
                        session_instruction_revision,
                    )
                preparation_before = self._accepted_preparation_state(save_id)
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
                message_context = {"replyToDecision": None}
                notice_id = item.get("reply_to_notice_id")
                if notice_id:
                    notices = self._work_store.unresolved_player_notices(save_id) if self._work_store else []
                    notice = next((n for n in notices if n.get("id") == notice_id), None)
                    if notice is None:
                        await self._send_life_error(ws, "life.chat.submit", {"requestId": request_id}, save_id, "DECISION_NO_LONGER_PENDING")
                        return
                    # Factual linkage is JSON-escaped; question and player text
                    # remain content, never instructions or an execution grant.
                    message_context["replyToDecision"] = {
                        "noticeId": notice_id, "goalId": notice.get("goalId"),
                        "question": notice["message"],
                        "playerConfirmedDecision": bool(item.get("resolve_notice")),
                    }
                prompt = (system_prompt + "\n消息上下文：" + json.dumps(message_context, ensure_ascii=False)
                          + "\n\n玩家说：" + text)
                active_task = ActiveChatTask(
                    request_id=request_id,
                    save_id=save_id,
                    command_id=request_id,
                    prompt=text,
                    async_task=asyncio.current_task(),
                    read_only=self._delivery_read_only(save_id) or self._job_in_flight(save_id),
                )
                self._configure_backend_progress(ws, request_id, save_id)
                await self._send_reply(ws, Envelope.create_life_chat_reply(
                    sender_instance_id=self.instance_id,
                    request_id=request_id,
                    save_id=save_id,
                    status="processing",
                    profile_revision=self._profile_revision(save_id),
                    memory_revision=self._memory_revision(save_id),
                    activity={"phase": "planning", "summary": "伙伴正在结合眼前情况想下一步。", "nextStep": "已有工作照常进行。"} if mode == "plan" else None,
                ))
                self._write_life_snapshot(save_id)
                self._life_turn_lease(request_id, save_id, player_text=text, notice_id=notice_id)
                execution_claimed = False
                result = {}
                previous_active = self._active_task
                self._active_task = active_task
                try:
                    if not active_task.read_only:
                        execution_claimed = await self._claim_execution(save_id)
                        if self._work_store and text.strip():
                            self._work_store.record_request(save_id, request_id, text, origin="player")
                    result = await self._await_life_provider(active_task, existing_cid, prompt, mode)
                    if result.get("success") and not active_task.read_only and self._work_store:
                        selected = self._work_store.state(save_id).decision
                        if selected.get("selected") and not selected.get("finished"):
                            self._active_player_requests[save_id] = request_id
                            self._job_reply_binding = (request_id, save_id, selected.get("taskId"), ws, request_id)
                finally:
                    if (not result.get("success") or active_task.cancelled) and active_task.decision_token and self._work_store:
                        if self._work_store.state(save_id).decision.get("token") == active_task.decision_token:
                            self._work_store.revoke_decision(save_id)
                    if self._active_task is active_task:
                        self._active_task = previous_active
                    if execution_claimed:
                        await self._release_execution(save_id)
                if generation != self._life_control_generation.get(save_id, 0):
                    await self._send_reply(ws, Envelope.create_life_chat_reply(
                        self.instance_id, request_id, save_id, status="completed", reply_text="这条旧请求已被新的工作控制撤销。",
                        profile_revision=self._profile_revision(save_id), memory_revision=self._memory_revision(save_id)))
                    return
                if result.get("success"):
                    await self._apply_life_intent(ws, request_id, save_id)
                success = bool(result.get("success"))
                if success and notice_id and item.get("resolve_notice") and self._work_store is not None:
                    self._work_store.answer_player_notice(save_id, notice_id, text, request_id)
                resolved_notice = notice_id if success and notice_id and self._work_store and any(
                    n.get("id") == notice_id and n.get("answerRequestId") == request_id
                    for n in self._work_store.state(save_id).notices) else None
                self._record_command(
                    request_id=request_id, save_id=save_id,
                    conversation_id=result.get("conversation_id") or (None if result.get("session_retried") else existing_cid),
                    prompt=text, status="completed" if success else "failed",
                    start_idx=-1, end_idx=-1,
                    usage=self._codex_turn_usage(result) if self.backend_name == "codex" else result.get("usage"),
                    error=result.get("error"), duration=time.monotonic() - active_task.start_time,
                )
                if success and self._milestone_store:
                    changed_arrangements = [node for node in self._milestone_store.list_nodes(save_id)
                                            if node.get("status") in {"suggested", "adopted"}
                                            and node.get("updatedAt") != proposals_before.get(node["id"])]
                    if changed_arrangements and self._work_store is not None:
                        self._work_store.record_request(save_id, request_id, text)
                        for node in changed_arrangements:
                            self._milestone_store.bind_player_request(save_id, node["id"], request_id)
                            if node.get("goalId"):
                                self._work_store.bind_goal_request(save_id, node["goalId"], request_id)
                    changed = [node for node in self._milestone_store.list_nodes(save_id)
                               if node.get("status") == "suggested" and
                               node.get("updatedAt") != proposals_before.get(node["id"])]
                    if changed:
                        proposed = max(changed, key=lambda node: node.get("updatedAt", 0))
                        self._life_proposals[save_id] = (proposed["id"], self._profile_revision(save_id), proposed["updatedAt"])
                cid = result.get("conversation_id") or (None if result.get("session_retried") else existing_cid)
                if cid and self._life_chat is not None and save_id:
                    self._life_chat.record_session_id(save_id, str(cid))
                    self._life_chat.record_context(save_id, str(cid), self._request_input_context(result.get("usage")))
                    # rotate_if_needed bound the context actually delivered at
                    # turn start. Edits during generation must rotate next time.
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
                        reply_text=str(result.get("response") or "") or None,
                        answered_notice_id=resolved_notice,
                        profile_revision=self._profile_revision(save_id),
                        memory_revision=self._memory_revision(save_id),
                    )
                else:
                    reply = Envelope.create_life_chat_reply(
                        sender_instance_id=self.instance_id,
                        request_id=request_id,
                        save_id=save_id,
                        status="failed",
                        reply_text=str(result.get("response") or "") or None,
                        profile_revision=self._profile_revision(save_id),
                        memory_revision=self._memory_revision(save_id),
                        error=str(result.get("error") or "TURN_FAILED"),
                    )
                if not success:
                    await self._send_reply(ws, reply)
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
            # Deliver the conversation before another model turn prepares work.
            # The queue drains player messages first, then rechecks the saved
            # arrangements so new instructions can change or revoke that work.
            if success:
                if not await self._retire_invalidated_native_jobs(save_id):
                    raise RuntimeError("NATIVE_SCOPE_CHANGE_UNCONFIRMED: 旧动作尚未确认停止，保持暂停")
                proposal = self._current_life_proposal(save_id)
                reply.payload["proposalReady"] = proposal is not None
                if proposal:
                    reply.payload["proposalNodeId"] = proposal["id"]
                reply.payload["activity"] = self._player_activity(save_id)
                await self._send_reply(ws, reply)
                self._pending_life_preparation.setdefault(save_id, preparation_before)
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
            before = self._accepted_preparation_state(save_id)
            _, game_date = self._plan_snapshot_state()
            adopted = self._milestone_store.adopt(
                save_id, proposal["id"], work_store=self._work_store, game_date=game_date,
            )
            if adopted.get("playerRequestId"):
                self._work_store.bind_goal_request(save_id, adopted["goalId"], adopted["playerRequestId"])
            self._life_proposals.pop(save_id, None)
            await self._send_reply(ws, Envelope.create_life_chat_reply(
                self.instance_id, request_id, save_id, "processing",
                self._profile_revision(save_id), self._memory_revision(save_id),
                reply_text=f"已记下：{adopted['title']}。我看看眼前能接手哪一步。",
                proposal_ready=False, activity=self._player_activity(save_id)))
            await self._send_reply(ws, self._build_milestones_state(save_id, ""))
        await self._send_reply(ws, Envelope.create_life_chat_reply(
            self.instance_id, request_id, save_id, "completed",
            self._profile_revision(save_id), self._memory_revision(save_id),
            reply_text="安排已记好。",
            proposal_ready=False, activity=self._player_activity(save_id)))
        self._pending_life_preparation.setdefault(save_id, before)

    def _accepted_preparation_state(self, save_id: str) -> dict[str, dict[str, Any]]:
        if self._milestone_store is None or self._work_store is None:
            return {}
        goals = {goal.id: goal for goal in self._work_store.state(save_id).goals}
        return {node["id"]: {"todoIds": dict(node.get("todoIds") or {}),
                "scope": objective_scope({"constraints": goals[node["goalId"]].constraints})
                if node.get("goalId") in goals else None}
                for node in self._milestone_store.list_nodes(save_id)}

    async def _start_accepted_preparation(
        self, ws: WebSocketClient | None, save_id: str, before: dict[str, dict[str, Any]]
    ) -> str | None:
        # A queued autonomy turn can select work while this caller awaits the
        # model slot. Recheck under the same lock before starting preparation.
        async with self._busy_lock:
            return await self._start_accepted_preparation_locked(ws, save_id, before)

    async def _start_accepted_preparation_locked(
        self, ws: WebSocketClient | None, save_id: str, before: dict[str, dict[str, Any]]
    ) -> str | None:
        """One normal work turn for newly authorized, currently due preparation."""
        if self._life_queue:
            self._pending_life_preparation.setdefault(save_id, before)
            return
        if self._milestone_store is None or self._work_store is None:
            return
        new_todo_ids: set[str] = set()
        layout_project = False
        layout_goal_id = None
        current = self._accepted_preparation_state(save_id)
        for node in self._milestone_store.list_nodes(save_id):
            todos = node.get("todoIds") or {}
            previous = before.get(node["id"], {})
            previous_todos = previous.get("todoIds", previous)
            scope_changed = "scope" in previous and previous["scope"] != current[node["id"]]["scope"]
            if node.get("status") == "adopted" and (todos != previous_todos or scope_changed):
                new_todo_ids.update(todos.values())
                layout_project = layout_project or bool({"layout", "production"} & todos.keys())
                layout_goal_id = node.get("goalId")
        if not new_todo_ids:
            return
        state = self._work_store.state(save_id)
        autonomy = self._autonomy.state(save_id) if self._autonomy is not None else None
        if layout_goal_id and self._autonomy is not None:
            self._autonomy.set_goal_scope(save_id, layout_goal_id)
        if (self._bedtime_winding_down(save_id) or state.paused or (autonomy is not None and autonomy.paused)
                or self._active_task is not None
                or self._work_settling(save_id)):
            self._pending_life_preparation.setdefault(save_id, before)
            return f"准备安排已保存。{self._player_activity(save_id)['summary']}"
        snapshot, game_date = self._plan_snapshot_state()
        due = [t for t in self._work_store.evaluate_todos(save_id, snapshot=snapshot, game_date=game_date)
               if t["id"] in new_todo_ids]
        if not due:
            return "安排已保存，眼前还没到这项准备的时间。"
        if autonomy is not None and (autonomy.enabled or layout_project):
            self._autonomy.request_job_decision(save_id)
            return ("已接手这个持续项目。我会观察实际需要、保留安排并分批推进，跨天继续；常规进度留在日志，遇到需要你决定的事再告诉你。"
                    if layout_project else "已把眼前可做的准备交给日常工作安排；常规进度会记入日志。")
        # One short job belongs to one accepted goal. Other due preparations
        # remain saved for their own decisions.
        preparation_goal_id = due[0].get("goal_id")
        due = [todo for todo in due if todo.get("goal_id") == preparation_goal_id]
        prompt = (
            "推进以下已保存安排："
            + json.dumps([{"intent": t["intent"], "goalId": t.get("goal_id"),
                "objectiveScope": objective_scope({"constraints": next((g.constraints for g in state.goals if g.id == t.get("goal_id")), {})})}
                for t in due], ensure_ascii=False)
        )
        # Reuse the existing command path; its ownership and busy checks still
        # apply. Await it so the turn remains tracked by the life queue task.
        previous_job = state.last_job
        await self.handle_chat_submit(ws, f"preparation-{uuid.uuid4().hex}", prompt, save_id,
                                      _goal_scope=preparation_goal_id, _slot_owned=True)
        after = self._work_store.state(save_id)
        if after.last_job and after.last_job != previous_job:
            outcome = after.last_job.get("status") or after.last_job.get("outcome")
            if outcome == "completed":
                return f"这次执行的实际结果：{after.last_job.get('progressSummary') or after.last_job.get('actualSummary') or '执行器已结束，具体变化尚未确认'}。后续准备仍按约定保留。"
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
        """Write a system event memory for a real successful terminal (§2).

        System-recorded terminals are layer-``fact`` entries tagged with the
        current game date; they render under 【事实】 and are never user
        constraints.
        """
        if self._memory_store is None or not save_id:
            return
        try:
            status, _ = self._memory_store.add(
                save_id, kind="event", text=text[:200], source="system",
                game_date=self._current_game_day_key or "unknown",
                expected_revision=0, command_id=command_id,
                category="fact",
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
                text=text,
                game_date=game_date,
            ))
        except Exception:
            logger.warning("Deferred care failed for %s/%s", save_id, kind, exc_info=True)
        finally:
            # A player message can now queue while care is being generated.
            # Once the model slot is free it needs the same FIFO drain as work.
            if self._chat_ws is not None:
                await self._drain_life_queue(self._chat_ws)

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
        if world is None:
            world = {}
            if isinstance(self._latest_snapshot_payload, dict):
                world = self._latest_snapshot_payload.get("world") or {}
        weather = world.get("weather") or world.get("weatherIcon")
        try:
            # Snapshot work has priority even when its created task has not yet
            # reached the lock. Waiting is event driven, never a model poll.
            while (wakeup := self._autonomy_wakeup_tasks.get(save_id)) is not None:
                await asyncio.shield(wakeup)
            if self._life_queue or self._pending_life_preparation:
                await self._drain_life_queue(self._chat_ws)
            async with self._busy_lock:
                # Deferred care uses the current profile/memory after any player
                # edits, and skips a greeting queued for a now obsolete day.
                if self._current_game_day_key and self._current_game_day_key != game_date:
                    return None
                current_profile = self._profile_store.get(save_id).get("profile") if self._profile_store else None
                profile = current_profile or profile
                if profile.get("careFrequency") == "quiet":
                    return None
                memory_render = self._memory_store.render_for_context(save_id) if self._memory_store else None
                existing_cid = None
                session_profile_revision = self._profile_revision(save_id)
                session_instruction_revision = self._memory_instruction_revision(save_id)
                if self._life_chat:
                    existing_cid = self._life_chat.rotate_if_needed(
                        save_id, session_profile_revision, session_instruction_revision)
                    system_prompt = self._life_chat.build_turn_prompt(
                        save_id, existing_cid, profile, memory_render, self._life_work_summary(save_id, "chat"),
                        mode="chat", milestones=self._life_milestone_summary(save_id, "chat"),
                        live_context=self._decision_context(save_id, origin="care"))
                else:
                    system_prompt = LifeChatService.build_system_prompt(profile, memory_render, None)
                prompt = system_prompt + "\n\n" + LifeChatService.build_care_prompt(
                    profile, memory_render, kind, game_date, weather, ref_event)
                prompt += "\n触发来源：游戏事件。结合当前状态自然交流。"
                active_task = ActiveChatTask(
                    request_id=f"care-{uuid.uuid4().hex[:8]}", save_id=save_id,
                    command_id="", prompt=prompt, async_task=asyncio.current_task())
                active_task.read_only = True
                result = await self._await_life_provider(active_task, existing_cid, prompt)
                cid = result.get("conversation_id") or (None if result.get("session_retried") else existing_cid)
                if cid and self._life_chat:
                    self._life_chat.record_session_id(save_id, str(cid))
                    self._life_chat.record_context(save_id, str(cid), self._request_input_context(result.get("usage")))
                    if result.get("success"):
                        self._life_chat.mark_prompt_delivered(save_id, str(cid), "chat")
                self._record_command(
                    request_id=active_task.request_id, save_id=save_id, conversation_id=cid,
                    prompt=f"[主动关怀:{kind}] {game_date}",
                    status="completed" if result.get("success") else "failed", start_idx=-1, end_idx=-1,
                    usage=self._codex_turn_usage(result) if self.backend_name == "codex" else result.get("usage"),
                    error=result.get("error"), duration=time.monotonic() - active_task.start_time)
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

    def profile_fingerprint(self, save_id: str | None = None, *, instruction_revision: str | None = None) -> str:
        """Revision metadata for the instructions and current environment."""
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
                "instructionsRevision": instruction_revision or instructions_revision(),
                "gameToolProfile": CODEX_GAME_TOOL_PROFILE_VERSION,
                "actorMode": getattr(self, "actor_mode", None),
                "profileRevision": self._profile_revision(save_id),
                "memoryInstructionRevision": (
                    self._memory_store.instruction_revision(save_id)
                    if self._memory_store is not None and save_id else 0
                ),
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
        self, save_id: str | None, conversation_id: str | None, *, instruction_revision: str | None = None
    ) -> str | None:
        """Refresh configuration metadata without replacing the conversation.

        Current instructions and preferences travel with the next turn. Only an
        explicit provider resume failure requires a replacement session.
        """
        if save_id:
            fingerprints = self._load_profile_fingerprints()
            fingerprints[f"{self.backend_name}:{save_id}"] = self.profile_fingerprint(
                save_id, instruction_revision=instruction_revision)
            self._save_profile_fingerprints(fingerprints)
        return conversation_id

    def consume_profile_rotation_note(self) -> None:
        """Compatibility hook: preference changes no longer rotate sessions."""
        return None

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
        self._sessions.pop(f"life:{key}", None)
        if self.backend_name == "agy":
            self._sessions.pop(save_id, None)
        self._save_sessions()
        self._save_session_history()
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
        if isinstance(latest, int) and not isinstance(latest, bool) and latest >= 0:
            return latest
        if usage.get("input_context_measured") is False:
            return None
        if usage.get("input_includes_cache"):
            value = usage.get("input_tokens")
            return value if usage.get("generations_count", 1) == 1 and isinstance(value, int) else None
        # A legacy single-generation receipt can still supply its input-side
        # counters. Never interpret a multi-generation aggregate as context.
        generations = usage.get("generations_count", usage.get("requestCount"))
        if generations not in (1, None):
            return None
        input_other = usage.get("inputOther")
        if input_other is None:
            input_other = usage.get("input_tokens")
        cache_read = usage.get("inputCacheRead")
        if cache_read is None:
            cache_read = usage.get("cache_read_tokens", usage.get("cache_read_input_tokens", usage.get("cached_input_tokens")))
        cache_creation = usage.get("inputCacheCreation")
        if cache_creation is None:
            cache_creation = usage.get("cache_creation_tokens", usage.get("cache_write_input_tokens"))
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
        """Record measured context for diagnostics; let the harness maintain it."""
        if not save_id:
            return None
        context_tokens = self._request_input_context(usage)
        self._session_requests[save_id] = self._session_requests.get(save_id, 0) + (request_count or 1)
        state = self._session_context_state.setdefault(save_id, {})
        state["latestInputContext"] = context_tokens
        state["measured"] = context_tokens is not None
        state["unmeasuredRequests"] = 0 if context_tokens is not None else state.get("unmeasuredRequests", 0) + 1
        if context_tokens is not None:
            state["maxInputContext"] = max(state.get("maxInputContext") or 0, context_tokens)
        return None

    def _note_session_tokens(self, save_id: str | None, usage: dict[str, Any] | None) -> bool:
        """Compatibility diagnostic; the harness owns context maintenance."""
        return self._note_session_context(save_id, usage) is not None

    async def _settle_day_advance(self, save_id: str | None, world: dict[str, Any]) -> None:
        """Settle current game progress at the native day boundary."""
        if not save_id:
            return
        settlement: dict[str, Any] | None = None
        if self._work_store is not None:
            try:
                funds = None
                payload = self._latest_snapshot_payload
                companion_payload = payload.get("companion") if isinstance(payload, dict) else None
                if isinstance(companion_payload, dict):
                    funds = companion_payload.get("availableMoney")
                settlement = await asyncio.to_thread(
                    self._work_store.settle_game_day,
                    save_id,
                    year=world.get("year"),
                    season=world.get("season"),
                    day=world.get("dayOfMonth"),
                    funds=funds,
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
        if self._milestone_store is not None:
            await self._settle_milestone_nodes(
                save_id, world, allow_reminders=bool(settlement and settlement.get("settled"))
            )
        if self._autonomy is not None and save_id:
            self._autonomy.reset_breaker(save_id)
        if settlement and settlement.get("settled"):
            self._maybe_deliver_outbox(save_id)

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
        labels = {"job-running": "正在处理", "job-waiting": "正在等待", "job-completed": "本次执行结束", "job-failed": "本次执行中止"}
        task = next((task for task in self._work_store.state(save_id).tasks if task.id == execution.task_id), None) if self._work_store else None
        title = task.title if task else "这项农场工作"
        detail = execution.message or execution.reason_code or ""
        if execution.result and isinstance(execution.result, dict):
            detail = detail or str(execution.result.get("error") or execution.result.get("message") or "")
        if execution.status == "executed":
            feedback = compact_task_feedback(task,
                {**(execution.result or {}), "effects": execution.effects,
                 "reasonCode": execution.reason_code, "commandId": execution.command_id},
                operation=execution.operation or "", status=execution.outcome)
            detail = feedback["progressSummary"] + (f"；{detail}" if detail and not feedback.get("reason") else "")
        # Player-bound jobs are followed by an outbox delivery turn carrying
        # the confirmed facts, so progress never closes the player's command.
        command_complete = phase in {"job-completed", "job-failed"} and str(
            binding[0]).startswith("autonomy-")
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

    def _autonomy_state_payload(self, save_id: str, state: Any, *, overview: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": state.mode, "paused": state.paused,
            "preferences": {"goal": state.goal,
                             "boxPreference": state.box_preference or "none", "idlePreference": state.idle_preference},
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
            "usageTodayText": self._usage_display.today_text(),
            "usageSaveTodayText": self._usage_display.today_text(save_id=save_id),
            "usageSaveTotalText": self._usage_display.summary_text(save_id=save_id, today=False),
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
        if overview is None:
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

    async def _push_usage_state(self, ws: WebSocketClient | None, save_id: str) -> None:
        if ws is not None and save_id and self._autonomy is not None:
            await self._send_reply(ws, Envelope.create_autonomy_state(
                self.instance_id, "usage-update", save_id,
                self._autonomy_state_payload(save_id, self._autonomy.state(save_id)), "confirmed", None,
            ))

    async def _handle_autonomy_control(
        self, ws: WebSocketClient | None, envelope: Envelope, active_save_id: str | None,
        *, preserve_life_request_id: str | None = None,
    ) -> bool:
        """Apply controls synchronously and acknowledge only the active save."""
        save_id = str(envelope.payload.get("saveId") or envelope.save_id or "")
        request_id = str(envelope.payload.get("requestId") or envelope.message_id)
        if not save_id or not active_save_id or save_id != active_save_id or self._autonomy is None:
            return False
        action = str(envelope.payload.get("action", ""))
        params = envelope.payload.get("parameters") or {}
        try:
            command_id = str(params.get("commandId") or "")
            current_command = self._current_command_id(save_id)
            if action in {"pause", "resume", "cancel"} and command_id and current_command and command_id != current_command:
                raise ValueError("COMMAND_MISMATCH: control belongs to an older instruction")
            lease_path = self.run_dir / "data" / "life-turn.json" if self.run_dir else None
            with cache_lock(lease_path) if lease_path is not None else contextlib.nullcontext():
                target_goal_id = str(params.get("goalId") or "") if action == "cancel" else ""
                work = self._work_store.state(save_id) if self._work_store else None
                if target_goal_id and (work is None or not any(g.id == target_goal_id for g in work.goals)):
                    raise ValueError("UNKNOWN_GOAL: read the current arrangement before cancelling it")
                current_goal = (work.decision.get("goalId") if work else None) or self._autonomy.state(save_id).goal_scope
                unrelated_cancel = bool(target_goal_id and target_goal_id != current_goal)
                pending_request_id = None
                if action == "cancel" and not target_goal_id and not params.get("allArrangements") and work:
                    queued = next((item for item in reversed(self._life_queue) if item.get("save_id") == save_id), None)
                    candidate = queued.get("request_id") if queued else self._active_player_requests.get(save_id)
                    if not candidate and self._busy_lock.locked() and self._active_task is None and self.run_dir:
                        lease_path = self.run_dir / "data" / "life-turn.json"
                        try:
                            lease = json.loads(lease_path.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            lease = {}
                        if lease.get("saveId") == save_id:
                            candidate = lease.get("requestId")
                    request = next((r for r in work.requests if r.get("id") == candidate), None)
                    if request is not None:
                        # The candidate is only a pending request while it does not
                        # own the active decision; a request bound to the selected
                        # task or goal is the current arrangement's own instruction,
                        # and a plain cancel must stop it outright (native job and
                        # autonomy goal included), not withdraw it as background.
                        decision_task = work.decision.get("taskId")
                        owns_decision = (
                            work.decision.get("requestId") == candidate
                            or (bool(decision_task) and decision_task in (request.get("taskIds") or []))
                            or (bool(work.decision.get("goalId"))
                                and request.get("goalId") == work.decision.get("goalId"))
                        )
                        if not owns_decision:
                            pending_request_id = str(candidate)
                if (action in {"pause", "resume", "cancel"} and not unrelated_cancel
                        and preserve_life_request_id != request_id):
                    if lease_path is not None:
                        temp = lease_path.with_suffix(".tmp")
                        temp.write_text(json.dumps({"requestId": None, "saveId": save_id, "playerText": None}), encoding="utf-8")
                        temp.replace(lease_path)
                    self._life_control_generation[save_id] = self._life_control_generation.get(save_id, 0) + 1
            native_ok = True
            if action == "resume" and not await self._retire_invalidated_native_jobs(save_id):
                raise ValueError("NATIVE_SCOPE_CHANGE_UNCONFIRMED: 旧动作尚未确认停止，保持暂停")
            if action == "cancel":
                if unrelated_cancel:
                    # Cancelling a named, unrelated arrangement must not interrupt
                    # the command connection or cancel the currently active goal.
                    self._withdraw_work_goals(save_id, {target_goal_id})
                    unrelated_cancel = True
                else:
                    native_ok = await self.handle_chat_cancel(ws, None, "F8 cancel", save_id, command_id=command_id or None,
                        all_arrangements=bool(params.get("allArrangements")), target_goal_id=target_goal_id or None,
                        pending_request_id=pending_request_id)
            state = (self._autonomy.state(save_id) if action == "cancel" and (params.get("goalId") or pending_request_id)
                     else self._autonomy.control(save_id, action, **params))
            if self._work_store is not None and not unrelated_cancel:
                self._apply_work_control(save_id, action, params)
            if action == "resume":
                if self._work_store is not None:
                    self._work_store.set_outbox_retry_block(save_id, None)
                self._notify_plan_worker(save_id)
                self._maybe_deliver_outbox(save_id)
            status, reason = ("confirmed", None) if native_ok else ("rejected", "NATIVE_CANCEL_UNCONFIRMED")
        except (TypeError, ValueError) as ex:
            state, status, reason = self._autonomy.state(save_id), "rejected", str(ex)
        reply = Envelope.create_autonomy_state(
            self.instance_id, request_id, save_id, self._autonomy_state_payload(save_id, state), status, reason
        )
        reply.payload["controlAction"] = action
        await self._send_reply(ws, reply)
        if action == "cancel" and self._milestone_store is not None:
            await self._send_reply(ws, self._build_milestones_state(save_id, ""))
        return status == "confirmed"

    async def _stop_autonomy_if_disabled(
        self, ws: WebSocketClient | None, save_id: str | None
    ) -> None:
        """Apply a shared disable immediately on every chat event, not only timeout."""
        if self._autonomy is None or not save_id:
            return
        if (
            not self._autonomy.state(save_id).enabled
            and not self._autonomy.state(save_id).goal_scope
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
        all_arrangements: bool = False,
        target_goal_id: str | None = None,
        pending_request_id: str | None = None,
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
        cancelled_goal_id = None
        cancelled_goal_ids: set[str] = set()
        if save_id and self._work_store is not None:
            state_before = self._work_store.state(save_id)
            cancelled_goal_id = None if pending_request_id else target_goal_id or state_before.decision.get("goalId")
            if pending_request_id:
                scoped_request = next((r for r in state_before.requests if r.get("id") == pending_request_id), {})
                cancelled_goal_ids.update(g.id for g in state_before.goals
                    if g.constraints.get("playerRequestId") == pending_request_id or g.id == scoped_request.get("goalId"))
            if all_arrangements:
                cancelled_goal_ids.update(goal.id for goal in state_before.goals if goal.status in {"active", "paused"})
            if reason not in {"Superseded by player", "autonomy disabled"}:
                original_request_id = pending_request_id or state_before.decision.get("requestId")
                if original_request_id:
                    self._work_store.stop_request(save_id, original_request_id, reason="player_cancel")
            selected_task = next((item for item in state_before.tasks
                                  if item.id == state_before.decision.get("taskId")), None)
            if native_command_id is None and selected_task is not None:
                native_command_id = next((step.command_id for step in selected_task.steps
                                          if step.command_id and step.status == "running"), None)
        self._active_player_requests.pop(save_id or "", None)
        native_ok = True if pending_request_id else (await self._confirm_native_terminal(native_command_id, save_id)
                     if pending and pending[0] == current_command_id
                     else await self._interrupt_short_job(save_id))
        if save_id and not native_ok and native_command_id:
            self._pending_native_cancels[save_id] = (current_command_id or command_id or "", native_command_id)
        self._autonomy_pending_task_fingerprints.clear()
        task = self._active_task
        if pending_request_id and task is not None and pending_request_id not in {task.request_id, task.command_id}:
            task = None
        if self._autonomy is not None and save_id:
            self._autonomy.reset_breaker(save_id)
            if reason not in {"Superseded by player", "autonomy disabled"}:
                cancelled_goal_id = cancelled_goal_id or (None if pending_request_id else self._autonomy.state(save_id).goal_scope)
                if cancelled_goal_id:
                    cancelled_goal_ids.add(cancelled_goal_id)
                if self._work_store:
                    try:
                        self._withdraw_work_goals(save_id, cancelled_goal_ids, all_arrangements=all_arrangements)
                    except WorkStateError:
                        logger.debug("Cancelled arrangement was already unavailable")
                if not pending_request_id:
                    self._autonomy.control(save_id, "cancel")
        if task is not None:
            active_req = task.request_id
            active_save = task.save_id or save_id or ""
            active_prompt = task.prompt

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
            # The drained init event can establish a new CID and reset its baseline.
            start_idx = task.start_max_idx
            cid = task.provider_conversation_id or self.get_conversation_id(active_save)
            usage_delta = None
            if self.backend_name == "kimi":
                end_idx = -1
                # Cancellation still settles: read whatever the wire recorded so far.
                usage_delta = read_usage_since(
                    cid, task.wire_start_offset, cwd=project_root()
                )
            elif self.backend_name in {"codex", "mcode"}:
                end_idx = -1
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

            if self.backend_name not in {"codex", "mcode"} and not task.recorded:
                self._record_command(
                    request_id=active_req,
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

            display_usage = normalize_usage(self.backend_name, usage_delta)
            reply = Envelope.create_chat_reply(
                sender_instance_id=self.instance_id,
                request_id=request_id or active_req,
                status="cancelled" if native_ok else "job-waiting",
                reply_text="任务已由玩家取消。" if native_ok else "取消已请求，原生作业终态尚未确认。",
                save_id=active_save,
                tokens_used=display_usage.get("total_tokens") if display_usage else None,
                prompt_tokens=display_usage.get("input_tokens") if display_usage else None,
                output_tokens=display_usage.get("output_tokens") if display_usage else None,
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
            if old_async_task is None and not task.read_only:
                await self._release_execution(active_save or save_id)
        else:
            logger.info("chat.cancel received but no task was actively running.")
            binding = getattr(self, "_job_reply_binding", None)
            binding_command = (binding[4] if len(binding) > 4 else binding[0]) if binding else None
            if not pending_request_id and binding and binding[1] == save_id and binding_command == current_command_id:
                await self._send_reply(ws, Envelope.create_chat_reply(
                    self.instance_id, binding[0], "cancelled" if native_ok else "job-waiting",
                    "任务已由玩家取消。" if native_ok else "取消已请求，原生作业终态尚未确认。",
                    save_id=save_id, error="PLAYER_CANCELLED" if native_ok else "NATIVE_CANCEL_UNCONFIRMED",
                    command_id=current_command_id, command_complete=native_ok,
                ))
        if native_ok and save_id:
            self._pending_native_cancels.pop(save_id, None)
        if native_ok and not pending_request_id and getattr(self, "_job_reply_binding", None) and self._job_reply_binding[1] == save_id:
            self._job_reply_binding = None
        return native_ok

    def _withdraw_work_goals(self, save_id: str, goal_ids: set[str], *, all_arrangements: bool = False) -> None:
        """Revoke durable authority in both the work ledger and saved arrangements."""
        if self._work_store is None:
            return
        for goal_id in goal_ids:
            self._work_store.cancel_goal(save_id, goal_id)
            goal = next((g for g in self._work_store.state(save_id).goals if g.id == goal_id), None)
            if goal and goal.constraints.get("playerRequestId"):
                self._work_store.stop_request(save_id, goal.constraints["playerRequestId"], reason="player_cancel")
        if self._milestone_store:
            self._milestone_store.withdraw_goals(save_id, goal_ids, all_arrangements=all_arrangements)
            proposal = self._life_proposals.get(save_id)
            if proposal and not self._current_life_proposal(save_id):
                self._life_proposals.pop(save_id, None)

    def _current_command_id(self, save_id: str | None) -> str | None:
        if not save_id:
            return None
        root = self._active_player_requests.get(save_id)
        if root is not None:
            return root
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

    async def _retire_invalidated_native_jobs(self, save_id: str | None) -> bool:
        """Confirm revoked commands before a revised scope can resume physical work."""
        if not save_id or self._work_store is None:
            return True
        unresolved = [(task, step) for task in self._work_store.state(save_id).tasks
                      if task.status == "cancelled" for step in task.steps
                      if step.command_id and step.status in {"running", "unknown", "waiting"}]
        if not unresolved:
            return True
        worker = self._plan_worker
        if worker is None:
            return False
        for task, step in unresolved:
            try:
                receipt = await worker.client.reconcile(step.command_id)
                native = receipt.get("native") if isinstance(receipt, dict) and "found" in receipt else receipt
                outcome, reason, effects, revision = normalise_native_result(native)
                if outcome is None:
                    await worker.client.call_tool("cancel_plan_command", {"command_id": step.command_id})
                    deadline = time.monotonic() + 5.0
                    while outcome is None and time.monotonic() < deadline:
                        await asyncio.sleep(0.1)
                        receipt = await worker.client.reconcile(step.command_id)
                        native = receipt.get("native") if isinstance(receipt, dict) and "found" in receipt else receipt
                        outcome, reason, effects, revision = normalise_native_result(native)
                if outcome is None:
                    return False
                current = self._work_store.state(save_id)
                pending = next((item for entry in current.tasks if entry.id == task.id
                                for item in entry.steps if item.id == step.id), None)
                if pending and pending.status in {"running", "unknown", "waiting"}:
                    self._work_store.commit_step_result(save_id, task_id=task.id, step_id=step.id,
                        outcome=outcome, effects=effects, reason_code=reason, snapshot_revision=revision,
                        command_id=step.command_id, game_date=self._plan_snapshot_state()[1],
                        feedback=compact_job_feedback(native, operation=step.operation, status=outcome, params=step.params))
            except Exception:
                logger.warning("Revoked native command could not be retired", exc_info=True)
                return False
        return True

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
                return await self._confirm_native_terminal(native_command_id, save_id)
        return True

    async def _confirm_native_terminal(self, command_id: str | None, save_id: str | None = None) -> bool:
        if not command_id:
            return False

        def recorded_terminal() -> bool:
            if not save_id or self._work_store is None:
                return False
            entry = next((entry for entry in reversed(self._work_store.state(save_id).executions)
                          if entry.command_id == command_id), None)
            return bool(entry and entry.outcome in {"completed", "partial", "failed", "rejected", "cancelled"}
                        and entry.reason_code != "NATIVE_TERMINAL_UNCONFIRMED")

        if recorded_terminal():
            return True
        if self._plan_worker is None:
            return False
        try:
            result = await self._plan_worker.client.reconcile(command_id)
            if isinstance(result, dict) and "found" in result:
                result = result.get("native") if result.get("found") else None
            return recorded_terminal() or isinstance(result, dict) and result.get("terminalState") in {
                "succeeded", "partially-succeeded", "partial", "failed",
                "rejected", "cancelled", "canceled",
            }
        except Exception:
            logger.debug("Unable to confirm native terminal for cancelled command %s", command_id, exc_info=True)
            # The native terminal callback can land while reconciliation is
            # waiting on an IPC session that subsequently closes.
            return recorded_terminal()

    async def handle_chat_submit(
        self,
        ws: WebSocketClient | None,
        request_id: str,
        text: str,
        save_id: str | None,
        *, _slot_owned: bool = False, _goal_scope: str | None = None,
        _goal_default: str | None = None,
        _delivery: dict[str, Any] | None = None,
    ) -> None:
        """Processes a user chat submit message."""
        logger.info("Processing chat submit [%s]: %s (saveId=%s)", request_id, text, save_id)
        if self._active_task and self._active_task.request_id == request_id:
            # Transport replay must not cancel and restart the same decision.
            return
        # Stable preset replay is transport recovery, not a new human instruction.
        # It must not preempt delivery, revoke authority, or undo a human pause.
        if (self._is_preset_request(request_id) and save_id
                and self._work_store is not None and self._autonomy is not None):
            bound = self._preset_bound_goal(save_id, request_id)
            policy = getattr(self, "_preset_day_boundary", None)
            preset_path = getattr(policy, "preset_path", None)
            if bound is None and preset_path:
                preset = json.loads(Path(preset_path).read_text(encoding="utf-8-sig"))
                import hashlib
                expected = "preset-" + hashlib.sha256(
                    (str(preset["runId"]) + "\n" + save_id).encode()).hexdigest()[:12]
                authorized = str(preset.get("goalText") or "")
                if request_id == expected and text == authorized:
                    self._work_store.record_request(save_id, request_id, authorized, origin="player")
                    bound = self._work_store.ensure_preset_main_goal(save_id, request_id, authorized)
                    logger.info("Verified preset [%s] bound main goal %s before model planning", request_id, bound.id)
            if bound is not None and bound.status == "active":
                self._autonomy.set_goal_scope(save_id, bound.id)
                # Reconnect replays this request. Wake once for the same native
                # state; transport generations/hold ids do not constitute progress.
                progress = (request_id, self._native_recovery_fingerprint(
                    self._latest_snapshot_payload or {}))
                resumes = getattr(self, "_preset_resume_progress", None)
                if resumes is None:
                    resumes = self._preset_resume_progress = {}
                if resumes.get(save_id) != progress:
                    self._autonomy.request_job_decision(save_id)
                    resumes[save_id] = progress
                self._preset_runs[save_id] = {"request_id": request_id, "goal_id": bound.id, "notified": False}
                logger.info("Preset goal [%s] resumed: re-scoped to active goal %s", request_id, bound.id)
                resume_reply = Envelope.create_chat_reply(
                    self.instance_id, request_id, "selected",
                    "继续已交付的农场照料安排。",
                    save_id=save_id, command_id=request_id, command_complete=False)
                resume_reply.payload["presetGoalId"] = bound.id
                await self._send_reply(ws, resume_reply)
                return
        if self._autonomy is not None and save_id:
            self._autonomy.record_event(save_id, f"chat-submit:{request_id}", "chat.submit")

        is_player = self._is_player_request(request_id)
        paused_chat = bool(is_player and save_id and (
            self._work_store and self._work_store.state(save_id).paused
            or self._autonomy and self._autonomy.state(save_id).paused))
        if paused_chat and self._requests_work_resume(text):
            envelope = Envelope.create_autonomy_control(self.instance_id, request_id, save_id, "resume")
            await self._handle_autonomy_control(ws, envelope, save_id)
            await self._send_reply(ws, Envelope.create_chat_reply(self.instance_id, request_id, "completed",
                "好，接着做。", save_id=save_id, command_id=request_id, command_complete=True))
            return
        midflight = False
        if (is_player and not paused_chat and save_id
                and not request_id.startswith("preparation-")
                and self._job_in_flight(save_id)):
            # A native job owns the execution connection: never interrupt it for
            # chat. The original text is recorded durably; control utterances
            # reach the in-flight command through the existing control bypass.
            if self._work_store is not None:
                try:
                    self._work_store.record_request(save_id, request_id, text, origin="player-midflight")
                except WorkStateError:
                    logger.debug("Could not record midflight request %s", request_id, exc_info=True)
            control = self._control_utterance(text)
            if control in {"pause", "cancel"}:
                envelope = Envelope.create_autonomy_control(self.instance_id, request_id, save_id, control)
                confirmed = await self._handle_autonomy_control(ws, envelope, save_id)
                if self._work_store is not None:
                    self._work_store.mark_midflight_consumed(save_id, [request_id])
                await self._send_reply(ws, Envelope.create_chat_reply(
                    self.instance_id, request_id,
                    "completed" if confirmed else "job-waiting",
                    ("好，先停下来。" if control == "pause" else "好，取消这项安排。") if confirmed
                    else "已收到，原生作业终态尚未确认。",
                    save_id=save_id,
                    error=None if confirmed else "NATIVE_CANCEL_UNCONFIRMED",
                    command_id=request_id, command_complete=confirmed))
                return
            midflight = True
        if is_player and not paused_chat and not midflight:
            self._preempt_autonomy_for_player(request_id)
            if self._autonomy is not None and save_id:
                self._autonomy.reset_breaker(save_id)
            await self._interrupt_short_job(save_id)
            if self._active_task and not self._active_task.cancelled:
                was_free = bool(self._autonomy and save_id and self._autonomy.state(save_id).enabled)
                await self.handle_chat_cancel(ws, self._active_task.request_id, "Superseded by player", save_id)
                if was_free:
                    self._autonomy.set_enabled(save_id, True)
                async with self._busy_lock:
                    pass
            if save_id:
                if self._work_store is not None:
                    try:
                        self._work_store.record_request(save_id, request_id, text, origin="player")
                    except WorkStateError:
                        logger.debug("Could not record player request %s", request_id, exc_info=True)
                self._active_player_requests[save_id] = request_id
        if (not is_player and save_id and self._work_store is not None
                and request_id in self._autonomy_requests):
            try:
                self._work_store.record_request(save_id, request_id, "结合当前目标和游戏状态安排下一步。", origin="autonomy")
            except WorkStateError:
                logger.debug("Could not record autonomy request %s", request_id, exc_info=True)

        # Independent autostart test resume: a re-fired stable preset id whose request
        # is already bound to a goal never plans a duplicate. An active goal resumes
        # its scope (the next snapshot's autonomy decision continues the persisted
        # work without replaying completed native steps); a goal that already reached
        # a persisted terminal state (e.g. a crash before the terminal marker reached
        # the Mod) gets its terminal signal replayed idempotently instead.
        if (is_player and self._is_preset_request(request_id) and save_id
                and self._work_store is not None and self._autonomy is not None):
            if self._preset_business_blocked(save_id):
                await self._maybe_notify_preset_terminal(ws, save_id)
                return
            bound = self._preset_bound_goal(save_id, request_id)
            if bound is not None and bound.status in {"completed", "cancelled"}:
                self._preset_runs[save_id] = {"request_id": request_id, "goal_id": bound.id, "notified": True}
                logger.info(
                    "Preset goal [%s] re-fired after goal %s already %s; replaying terminal signal",
                    request_id, bound.id, bound.status)
                terminal_reply = Envelope.create_chat_reply(
                    self.instance_id, request_id, bound.status,
                    "本轮农场照料目标已完成。" if bound.status == "completed" else "本轮农场照料目标已取消。",
                    save_id=save_id, command_id=request_id, command_complete=True)
                terminal_reply.payload["presetGoalId"] = bound.id
                terminal_reply.payload["presetGoalTerminal"] = bound.status
                await self._send_reply(ws, terminal_reply)
                return

        # 1. Concurrency deduplication guard
        if not _slot_owned and (self._busy_lock.locked() or (self._active_task and not self._active_task.cancelled)):
            logger.warning("Chat submit [%s] rejected (companion busy)", request_id)
            if _delivery is not None:
                # The runner retries at the next boundary; no UI noise.
                self._settle_delivery(request_id, False)
                return
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

        async with contextlib.nullcontext() if _slot_owned else self._busy_lock:
            instructions = load_instruction_bundle(
                actor_mode=getattr(self, "actor_mode", None))
            # A changed profile/tool surface (or a legacy session without a
            # recorded fingerprint) must not reuse the old provider session.
            existing_cid = self._ensure_session_matches_profile(
                save_id, self.get_conversation_id(save_id), instruction_revision=instructions.revision
            )
            previous_cid = existing_cid
            if _delivery is not None:
                prompt = self._format_delivery_prompt(
                    _delivery["events"], save_id or "",
                    midflight=_delivery.get("midflight") or [],
                    read_only=bool(_delivery.get("read_only")),
                    instructions=instructions.text,
                )
                task_prompt = "[outbox-delivery]"
            elif request_id in self._autonomy_requests:
                prompt = instructions.text + "\n\n" + text
                task_prompt = text
            else:
                prompt = self._format_agent_prompt(text, save_id, instructions=instructions.text)
                task_prompt = text
            if midflight or paused_chat:
                prompt += "\n执行环境：" + json.dumps({
                    "workDispatchAvailable": False, "paused": bool(paused_chat),
                    "inFlight": self._in_flight_summary(save_id) if midflight else None,
                }, ensure_ascii=False)
            # PromptDelivery shares acknowledged guidance across all turn types.
            reload_summary = self._work_reload_context(save_id, existing_cid)
            if reload_summary:
                prompt += "\n读档变化：" + json.dumps(reload_summary, ensure_ascii=False)
            is_kimi = self.backend_name == "kimi"
            # The agy SQLite bill is agy-only; Kimi usage comes from the provider
            # wire file. Capture the byte offset now so this turn's records are
            # attributable even on a resumed (cumulative) session.
            start_max_idx = get_max_gen_idx(existing_cid) if self.backend_name == "agy" else -1
            start_max_step_idx = get_max_step_idx(existing_cid) if self.backend_name == "agy" else -1
            wire_start_offset = wire_offset(existing_cid, cwd=project_root()) if is_kimi else 0

            # Create active task record with prompt and starting boundary
            active_task = ActiveChatTask(
                request_id=request_id,
                save_id=save_id or "",
                command_id=(str(_delivery.get("command_id") or request_id)
                            if _delivery is not None else request_id),
                prompt=task_prompt,
                start_max_idx=start_max_idx,
                start_max_step_idx=start_max_step_idx,
                wire_start_offset=wire_start_offset,
                async_task=asyncio.current_task(),
                instructions_text=instructions.text,
                read_only=paused_chat or midflight or bool((_delivery or {}).get("read_only")),
                preserve_session=paused_chat or midflight or _delivery is not None,
                goal_scope=_goal_scope,
                goal_default=_goal_default,
                work_request_ids=[str(m["id"]) for m in (_delivery or {}).get("midflight") or []],
            )
            self._active_task = active_task
            starting_decision_token = ((self._work_store.state(save_id).decision or {}).get("token")
                                       if self._work_store is not None and save_id else None)
            # Park the internal plan worker and release its MCP session: the Mod
            # serves one command socket and the provider turn now owns it.
            # Read-only turns (paused/midflight/read-only delivery) only use the
            # cached life surface and never touch the execution connection.
            execution_claimed = False
            try:
                if not active_task.read_only:
                    execution_claimed = await self._claim_execution(save_id, recover=not paused_chat)
                # 2. Immediate progress notification.
                prog_reply = Envelope.create_chat_reply(
                    sender_instance_id=self.instance_id,
                    request_id=request_id,
                    status="processing",
                    reply_text="正在思考与执行...",
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

                # Chat and work share the same bounded session recovery.
                result = await self._await_work_provider(active_task, existing_cid, prompt)
                session_retried = bool(result.get("session_retried"))

                # If task was cancelled during execution, handle_chat_cancel already handled reply & record
                if active_task.cancelled:
                    logger.info("Task [%s] was cancelled during execution; cancel handler finished.", request_id)
                    if _delivery is not None:
                        self._settle_delivery(request_id, False)
                    if not getattr(active_task, "recorded", False):
                        duration = time.monotonic() - getattr(active_task, "start_time", time.monotonic())
                        cid = active_task.provider_conversation_id or result.get("conversation_id") or self.get_conversation_id(save_id)
                        interrupted_usage = (get_command_usage_delta(cid, active_task.start_max_idx) if self.backend_name == "agy"
                            else self._codex_turn_usage(result) if self.backend_name == "codex"
                            else result.get("usage") if self.backend_name == "mcode" else None)
                        self._record_command(
                            request_id=request_id,
                            save_id=save_id,
                            conversation_id=cid,
                            prompt=text,
                            status="interrupted",
                            start_idx=active_task.start_max_idx,
                            end_idx=-1,
                            usage=interrupted_usage,
                            missing_reason=None if interrupted_usage else "interrupted_usage_unknown",
                            error=getattr(active_task, "abort_reason", None) or "INTERRUPTED",
                            duration=duration,
                        )
                        active_task.recorded = True
                        if self.backend_name in {"codex", "mcode"} and self._autonomy and save_id:
                            self._autonomy.record_usage(save_id, self._current_game_day_key or "unknown", result.get("usage"))
                    return

                # 5. Handle output & token usage
                if result.get("status") == "interrupted":
                    status = "interrupted"
                else:
                    status = "completed" if result.get("success") else "failed"
                new_session_established = previous_cid is None
                self._notify_terminal(status)
                reply_text = result.get("response", "")
                if session_retried and result.get("success"):
                    reply_text = "（会话已失效，已安全新建会话后继续。）\n" + reply_text
                cid = result.get("conversation_id") or (None if result.get("session_retried") else existing_cid)
                if cid and existing_cid and cid != existing_cid:
                    start_max_idx = -1
                if new_session_established and cid:
                    logger.info("New %s session established [%s]; game agent profile bound.", self.provider, cid)
                err = result.get("error")
                duration = result.get("duration", 0.0)

                if not result.get("success"):
                    if err in {"TOOL_APPROVAL_REQUIRED", "CODEX_INVALID_PROMPT", "CONTENT_REVIEW_BLOCKED"}:
                        reply_text = ("MiniMax Code 的内容审核拒绝了本次请求（CONTENT_REVIEW_BLOCKED）。工作及结果重投已暂停，保留游戏实际进度；请在客户端确认原因后手动继续。"
                                      if err == "CONTENT_REVIEW_BLOCKED" else
                                      "模型服务安全检查拒绝了本次请求（invalid_prompt）。自主工作及结果重投已暂停，保留此前实际进度；请处理请求问题后手动继续。"
                                      if err == "CODEX_INVALID_PROMPT" else
                                      "工具调用被审批策略拒绝，拒绝的操作未执行。自主工作已暂停，保留此前实际进度；请处理审批配置后手动继续。")
                        if self._autonomy is not None and save_id:
                            self._autonomy.control(save_id, "pause")
                        if self._work_store is not None and save_id:
                            self._work_store.set_paused(save_id, True)
                            self._work_store.set_outbox_retry_block(save_id, str(err))
                    elif err == "RESOURCE_EXHAUSTED":
                        reply_text = "AI 模型额度已用尽，本次任务已停止。自由模式已暂停，请处理额度后手动继续。"
                        if self._autonomy is not None and save_id:
                            try:
                                self._autonomy.control(save_id, "pause")
                            except (TypeError, ValueError):
                                logger.warning("Unable to pause free mode after quota exhaustion for %s", save_id)
                        if self._work_store is not None and save_id:
                            self._work_store.set_paused(save_id, True)
                            self._work_store.set_outbox_retry_block(save_id, "RESOURCE_EXHAUSTED")
                    elif err == "AUTHENTICATION_REQUIRED":
                        reply_text = "Kimi 登录或认证已失效，请完成登录后再试。"
                    elif err == "RATE_LIMIT_EXCEEDED":
                        reply_text = "Kimi 请求过于频繁，请稍后重试。"
                    elif err == "MCODE_SESSION_UNAVAILABLE":
                        reply_text = "会话已失效，已安全新建会话；请再发一次。"

                if cid and save_id and (not active_task.read_only or active_task.preserve_session):
                    self.record_conversation_id(save_id, cid)
                    if result.get("success") and reload_summary:
                        self._work_reload_context(save_id, cid, delivered=True,
                                                  expected_game_session_id=reload_summary.get("gameSessionId"))

                end_max_idx = get_max_gen_idx(cid) if (cid and self.backend_name == "agy") else -1

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
                elif self.backend_name == "codex":
                    usage = self._codex_turn_usage(result)
                    usage_source = result.get("usage_source", "unknown")
                    if usage is None:
                        missing_reason = result.get("usage_limits") or "codex_usage_unavailable"
                elif self.backend_name == "dsh":
                    # SDK assistant/message counters are already scoped to this
                    # turn, including resumed sessions and interrupted jobs.
                    usage = result.get("usage") or self._usage_display.turn_response_usage(request_id, save_id)
                    usage_source = "dsh_assistant_message" if usage else "unknown"
                    if usage is None:
                        missing_reason = "dsh_response_usage_unavailable"
                elif self.backend_name == "mcode":
                    # One exec result is already scoped to exactly this turn,
                    # resumed session or not.
                    usage = result.get("usage")
                    usage_source = "mcode_exec_result" if usage else "unknown"
                    if usage is None:
                        missing_reason = "mcode_exec_usage_unavailable"
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
                    display_usage = normalize_usage(self.backend_name, usage)
                    tokens_used = display_usage.get("total_tokens")
                    prompt_tokens = display_usage.get("input_tokens")
                    if prompt_tokens is None:
                        prompt_tokens = usage.get("prompt_tokens")
                    output_tokens = usage.get("output_tokens")
                    cached_tokens = usage.get("cache_read_tokens", usage.get("cache_read_input_tokens"))
                    # Explicit split: cache read and cache write are different
                    # counters and must not be merged into one "cache" number.
                    cache_read_tokens = usage.get("cache_read_tokens", usage.get("cache_read_input_tokens"))
                    cache_write_tokens = usage.get("cache_creation_tokens", usage.get("cache_write_input_tokens"))
                    # Number of provider model calls aggregated for this turn
                    # (usage.record lines already de-duplicated by the wire reader).
                    model_calls = usage.get("generations_count", usage.get("requestCount"))

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

                # Structured progress already feeds the player's work panel.
                # Keep diagnostic operation names/reason codes out of chat.
                reply_status = status
                job_selected = False
                if not active_task.read_only and self._work_store and save_id:
                    selected = self._work_store.state(save_id).decision
                    if selected.get("selected") and not selected.get("finished"):
                        # job-selected means dispatched, not finished: the model's
                        # own acknowledgement is delivered verbatim and the
                        # confirmed terminal arrives as a later delivery turn.
                        reply_status = "selected"
                        job_selected = True
                        self._job_reply_binding = (request_id, save_id, selected.get("taskId"), ws, active_task.command_id)
                    elif status == "completed":
                        reply_status = "decision-completed"
                # Independent autostart test: once the model binds a goal to the preset
                # request, adopt it as the autonomy scope so continuation and restart
                # resume the same persisted work instead of planning a duplicate goal.
                # A goal completed within this very turn still reports its verified
                # terminal state on the dedicated presetGoalTerminal field — generic
                # reply status/commandComplete never settle the preset run.
                preset_goal_id = None
                preset_goal_terminal = None
                if (self._is_preset_request(request_id) and save_id and self._autonomy is not None
                        and self._work_store is not None and not active_task.cancelled):
                    bound = self._preset_bound_goal(save_id, request_id)
                    if bound is not None:
                        preset_goal_id = bound.id
                        self._preset_runs[save_id] = {"request_id": request_id, "goal_id": bound.id, "notified": False}
                        if bound.status == "active":
                            self._autonomy.set_goal_scope(save_id, bound.id)
                            logger.info("Preset goal [%s] adopted goal scope %s", request_id, bound.id)
                        elif bound.status in {"completed", "cancelled"}:
                            policy = getattr(self, "_preset_day_boundary", None)
                            if policy and policy.enabled:
                                assessed = policy.reconcile(save_id, self._work_store, self._latest_snapshot_payload)
                                bound = self._preset_bound_goal(save_id, request_id)
                                if assessed and assessed.get("reason") and bound.status == "cancelled":
                                    reply_text = "本轮未达到已核验的原生睡觉保存边界，已安全结束，不报告成功。 " + assessed["reason"]
                            preset_goal_terminal = bound.status
                final_reply = Envelope.create_chat_reply(
                    sender_instance_id=self.instance_id,
                    request_id=request_id,
                    status=reply_status,
                reply_text=reply_text or ("本次对话未能完成。" if active_task.read_only and status == "failed"
                    else "已保留暂停。" if active_task.read_only else "任务未能成功执行。" if status == "failed" else "任务执行完毕。"),
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
                if preset_goal_id:
                    final_reply.payload["presetGoalId"] = preset_goal_id
                if preset_goal_terminal:
                    final_reply.payload["presetGoalTerminal"] = preset_goal_terminal
                notification_saved = False
                if _delivery is not None and result.get("success") and self._work_store and save_id:
                    ids = [event["id"] for event in _delivery["events"]
                           if event.get("requestId") and not str(event["requestId"]).startswith("autonomy-")]
                    if ids:
                        final_reply.payload["deliveryOutboxIds"] = ids
                        self._work_store.queue_outbox_notification(save_id, ids, final_reply.to_mapping())
                        notification_saved = True
                sent_ok = await self._send_reply(ws, final_reply)
                if preset_goal_terminal and sent_ok and self._preset_runs.get(save_id, {}).get("goal_id") == preset_goal_id:
                    # The terminal signal rode this final reply; the snapshot-time
                    # notifier must not send it again.
                    self._preset_runs[save_id]["notified"] = True
                if _delivery is not None:
                    # Model completion consumes its turn. Player delivery needs
                    # the native receipt; a failed socket replays the saved reply.
                    self._settle_delivery(request_id, bool(sent_ok or notification_saved) and bool(result.get("success")))
                if self._autonomy is not None and save_id and not active_task.read_only:
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
                            # A finished selection left by an earlier turn is
                            # historical feedback, not a dispatch by this turn.
                            selected_this_turn = bool(decision.get("selected") and (
                                not decision.get("finished")
                                or decision.get("token") != starting_decision_token))
                            if not selected_this_turn:
                                self._autonomy.record_action_result(
                                    save_id, autonomy_fingerprint, True, reason="standby"
                                )
                                self._rearm_after_standby_turn(save_id)
                            else:
                                day_key = self._current_game_day_key or "unknown"
                                if getattr(self, "_standby_rearm_streak", None) is not None:
                                    self._standby_rearm_streak.pop(f"{save_id}:{day_key}", None)
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

                                if fail_reason == "BEDTIME":
                                    pass  # Planned rest is not a work failure.
                                elif terminal_status in {"unknown", "cancelled"}:
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
                    self._note_session_context(save_id, usage_delta or usage)
                logger.info(
                    "Completed chat submit [%s] with status=%s, tokens=%s (source=%s)",
                    request_id,
                    status,
                    tokens_used,
                    usage.get("source", "none") if usage else (usage_source or "none"),
                )
            except asyncio.CancelledError:
                logger.info("handle_chat_submit [%s] cancelled.", request_id)
                if _delivery is not None:
                    self._settle_delivery(request_id, False)
                fingerprint = self._autonomy_requests.pop(request_id, None)
                if fingerprint and self._autonomy is not None and save_id:
                    decision = self._work_store.state(save_id).decision if self._work_store else {}
                    if not decision.get("selected"):
                        # No native work owns this consumed wakeup. Permit one
                        # fresh decision on reconnect; selected jobs keep their
                        # existing execution/reconciliation gate unchanged.
                        self._autonomy.request_job_decision(save_id)
                if not getattr(active_task, "recorded", False):
                    duration = time.monotonic() - getattr(active_task, "start_time", time.monotonic())
                    cid = active_task.provider_conversation_id or self.get_conversation_id(save_id)
                    interrupted_usage = get_command_usage_delta(cid, active_task.start_max_idx) if self.backend_name == "agy" else None
                    self._record_command(
                        request_id=request_id,
                        save_id=save_id,
                        conversation_id=cid,
                        prompt=active_task.prompt,
                        status="interrupted",
                        start_idx=active_task.start_max_idx,
                        end_idx=-1,
                        usage=interrupted_usage,
                        missing_reason=None if interrupted_usage else "interrupted",
                        error=getattr(active_task, "abort_reason", None) or "INTERRUPTED",
                        duration=duration,
                    )
                    active_task.recorded = True
            except Exception as ex:
                logger.error("Error in handle_chat_submit [%s]: %s", request_id, ex, exc_info=True)
                if _delivery is not None:
                    self._settle_delivery(request_id, False)
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
                # the provider just committed (no model turn per step). Read-only
                # turns never claimed it.
                if execution_claimed:
                    await self._release_execution(save_id)
                # Publish after the provider slot is cleared. Publishing inside
                # the try block leaves the player's last projection at planning,
                # even when the native job and its model follow-up have settled.
                if save_id:
                    try:
                        await self._push_work_state(ws, save_id)
                    except Exception:
                        logger.debug("Unable to publish settled work state", exc_info=True)
                # A work turn just freed the single model slot: deliver any queued
                # life-chat submits FIFO (each always reaches a terminal state).
                if self._life_queue or self._pending_life_preparation:
                    try:
                        task = asyncio.get_running_loop().create_task(self._drain_life_queue(ws))
                        self._life_drain_tasks.add(task)
                        task.add_done_callback(self._life_drain_tasks.discard)
                    except RuntimeError:
                        pass

    def _work_reload_context(self, save_id: str | None, session_id: str | None, *, delivered: bool = False,
                             expected_game_session_id: str | None = None):
        from .game_reload import _read, _write, reload_context
        from .snapshot_cache import cache_lock
        if self.run_dir is None or not save_id:
            return None
        player_mode = getattr(self, "actor_mode", "companion") == "player"
        # A delivery acknowledgement only needs immutable load metadata, never
        # another full unlocked read of a work ledger that is being committed.
        summary = (_read(self.run_dir / "data/reload-context.json").get(save_id)
                   if player_mode else reload_context(self.run_dir, save_id))
        if not summary:
            return None
        path = self.run_dir / "data/reload-deliveries.json"
        actor_id = (self._latest_snapshot_payload or {}).get("actorMultiplayerId", "unknown")
        # A model context rotation is not a native save reload. Acknowledgment
        # belongs to the save/actor/world session, not the disposable provider CID.
        key = (f"native-world:{save_id}:player:{actor_id}" if player_mode
               else f"{self.backend_name}:{save_id}:{session_id}")
        reload_id = summary.get("gameSessionId")
        with cache_lock(path):
            records = _read(path)
            if (player_mode or session_id) and records.get(key) == reload_id:
                return None
            if delivered:
                if player_mode and expected_game_session_id is not None and (
                        reload_id != expected_game_session_id or
                        getattr(self, "_player_session_id", None) != expected_game_session_id):
                    return None  # A late old-world reply cannot acknowledge a new load.
                records[key] = reload_id
                _write(path, records)
                return None
        if player_mode or session_id:
            return {k: summary[k] for k in ("gameSessionId", "loadedGameDate", "instruction", "requiresWorldRevalidation", "historyTool") if k in summary}
        return summary

    def _format_agent_prompt(self, user_text: str, save_id: str | None = None, *, instructions: str | None = None) -> str:
        live = self._decision_context(save_id, origin="chat")
        live.pop("reloadSummary", None)
        context = render_decision_context(model_decision_context(live))
        return (
            f"实时上下文：{context}\n"
            f"{instructions or decision_policy()}\n\n"
            f"玩家指令：{user_text}"
        )

    def _is_player_request(self, request_id: str) -> bool:
        if request_id in self._autonomy_requests:
            return False
        if request_id.startswith("autonomy-") or request_id.startswith("delivery-"):
            return False
        return True

    # Independent autostart test (isolated launcher opt-in). The Mod dispatches a
    # preset goal through this exact chat pipeline with a stable per-run/save request
    # id, so reconnect/restart never creates a duplicate goal: a re-fired id whose
    # request is already bound to an active goal resumes that goal's scope instead of
    # planning again.
    _PRESET_REQUEST_PREFIX = "preset-"

    def _is_preset_request(self, request_id: str) -> bool:
        return isinstance(request_id, str) and request_id.startswith(self._PRESET_REQUEST_PREFIX)

    def _preset_bound_goal(self, save_id: str | None, request_id: str):
        """The active goal already bound to a preset request id, or None."""
        if self._work_store is None or not save_id:
            return None
        state = self._work_store.state(save_id)
        request = next((r for r in state.requests if r.get("id") == request_id), None)
        goal_id = request.get("goalId") if request else None
        if not goal_id:
            return None
        return next((g for g in state.goals if g.id == goal_id), None)


    @staticmethod
    def _requests_work_resume(text: str) -> bool:
        # Whole work-control utterances only; '继续讲讲' is ordinary conversation.
        normalized = text.strip().rstrip("。！!，, ")
        return normalized in {"继续", "继续吧", "继续工作", "继续干活", "继续任务", "恢复工作", "恢复任务",
                              "继续刚才的工作", "继续刚才的任务", "继续之前的工作", "继续暂停的工作", "现在开始吧", "开始工作", "恢复", "恢复吧"}

    def _control_utterance(self, text: str) -> str | None:
        # Whole work-control utterances only; conversation that merely mentions
        # pause or cancel must never steer native work.
        normalized = text.strip().rstrip("。！!，, ")
        if self._requests_work_resume(text):
            return "resume"
        if normalized in {"暂停", "先停一下", "暂停工作", "先停下", "停一下"}:
            return "pause"
        if normalized in {"取消当前任务", "取消现在的工作", "取消任务"}:
            return "cancel"
        if explicit_cancel_scope(text) is not None:
            return "cancel"
        return None

    def _job_in_flight(self, save_id: str | None) -> bool:
        """A selected/undispatched-or-running native job owns the connection."""
        if not save_id or self._work_store is None:
            return False
        state = self._work_store.state(save_id)
        decision = state.decision
        if decision.get("selected") and not decision.get("finished"):
            selected = next((t for t in state.tasks if t.id == decision.get("taskId")), None)
            # Legacy direct selections have no task. A cancelled pending plan,
            # however, owns no physical work and cannot block its replacement.
            if selected is None or selected.status != "cancelled":
                return True
        return any(
            any(step.command_id and step.status in {"running", "unknown", "waiting"}
                for step in task.steps)
            for task in state.tasks
        )

    def _work_settling(self, save_id: str | None) -> bool:
        """Work executes natively or confirmed facts still await delivery."""
        if not save_id or self._work_store is None:
            return False
        if self._job_in_flight(save_id):
            return True
        try:
            return bool(self._work_store.pending_outbox(save_id))
        except Exception:
            return False

    def _in_flight_summary(self, save_id: str | None) -> dict[str, Any]:
        if not save_id or self._work_store is None:
            return {}
        state = self._work_store.state(save_id)
        task = next((t for t in state.tasks if t.id == state.decision.get("taskId")), None)
        summary: dict[str, Any] = {}
        if task is not None:
            summary["title"] = task.title
            summary["status"] = task.status
        if state.decision.get("taskId"):
            summary["taskId"] = state.decision.get("taskId")
        return summary

    async def _on_job_terminal(
        self, save_id: str, execution: StepExecution, reason: str
    ) -> None:
        if not save_id:
            return
        error_text = " ".join((str(execution.reason_code or ""), str(execution.message or ""),
                               json.dumps(execution.result, ensure_ascii=False, default=str)))
        blocker = re.search(r"\bIDEMPOTENCY_(?:CAPACITY_EXCEEDED|RECEIPT_UNAVAILABLE)\b", error_text)
        if blocker:
            # Receipt storage is infrastructure. The gameplay model cannot
            # repair it by selecting another route, query, or business action.
            self._pause_native_recovery(save_id, blocker.group())
        task = next((t for t in self._work_store.state(save_id).tasks
                     if t.id == execution.task_id), None) if self._work_store is not None else None
        # Normal execution commits this fact with its native outcome.
        # Callback-only integrations can still publish their supplied receipt
        # before any care/UI await; never infer ownership from a newer request.
        # Persist the confirmed terminal fact for the owning player request and
        # schedule its boundary delivery. Autonomy-originated jobs already reach
        # the model via lastResult in the next autonomy turn, so they stay out
        # of the outbox (no extra model turn per autonomous job).
        if self._work_store is not None:
            request_record = None
            try:
                request_record = self._work_store.find_request_for_task(save_id, execution.task_id)
            except Exception:
                logger.debug("Could not resolve request for task %s", execution.task_id, exc_info=True)
            request_id = (request_record or {}).get("id")
            origin = (request_record or {}).get("origin")
            if origin in {"player", "player-midflight"} and request_id:
                feedback = compact_task_feedback(task,
                    {**(execution.result or {}), "effects": execution.effects,
                     "reasonCode": execution.reason_code, "commandId": execution.command_id},
                    operation=execution.operation or "", status=execution.outcome)
                event = {
                    "kind": "terminal",
                    "commandId": execution.command_id or execution.task_id or "unknown",
                    "taskId": execution.task_id,
                    "goalId": getattr(task, "goal_id", None),
                    "operation": execution.operation,
                    "outcome": execution.outcome,
                    "reasonCode": execution.reason_code,
                    "effects": execution.effects,
                    "feedback": feedback,
                    "snapshotRevision": self._latest_snapshot_revision,
                    "gameDate": self._current_game_day_key,
                    "requestId": request_id,
                }
                try:
                    recorded = self._work_store.state(save_id).outbox
                    if not any(e.get("commandId") == event["commandId"]
                               and e.get("outcome") == event["outcome"] for e in recorded):
                        self._work_store.append_outbox_event(save_id, event)
                except WorkStateError:
                    logger.warning("Could not append outbox event for %s", execution.task_id, exc_info=True)
                if blocker:
                    self._work_store.set_outbox_retry_block(save_id, blocker.group())
                else:
                    self._maybe_deliver_outbox(save_id)

        pending_af = self._autonomy_pending_task_fingerprints.pop(execution.task_id, None)
        job_success = execution.task_status == "completed" and execution.outcome == "completed"
        if job_success:
            # Real native success terminal: record a system memory event (deduped
            # by commandId) and evaluate the work-done care hook (contract §2/§4).
            # Failures/cancellals/plans never reach this branch.
            task_title: str | None = None
            if task is not None:
                task_title = task.title
            if not task_title:
                task_title = execution.operation or "农场作业"
            event_ref = execution.command_id or execution.task_id or task_title
            feedback = compact_task_feedback(task,
                {**(execution.result or {}), "effects": execution.effects,
                 "reasonCode": execution.reason_code},
                operation=execution.operation or "", status=execution.outcome)
            fact = f"作业「{task_title}」的实际结果：{feedback['progressSummary']}"
            self._record_memory_event(save_id, fact, event_ref)
            # The durable result outbox owns the single player-facing report.
            # A second care turn here duplicated results and delayed continuation.
        if pending_af and self._autonomy is not None and execution.reason_code != "BEDTIME" and execution.outcome not in {"failed", "rejected", "partial"}:
            af_save_id, af_fingerprint = pending_af
            job_fail_reason = execution.reason_code or execution.message or execution.outcome or reason
            self._autonomy.record_action_result(
                af_save_id, af_fingerprint, job_success, reason=None if job_success else job_fail_reason
            )
        await self._push_work_state(self._chat_ws, save_id)

    def _maybe_deliver_outbox(self, save_id: str | None) -> None:
        """Start the boundary delivery turn for persisted terminal facts."""
        if not save_id or self._work_store is None:
            return
        if self._latest_snapshot_payload is None:
            return
        if self.actor_mode == "player" and self.control_owner != "agent":
            return
        running = self._delivery_tasks.get(save_id)
        if running is not None and not running.done():
            return
        try:
            if not (self._work_store.pending_outbox(save_id)
                    or self._work_store.pending_outbox_notifications(save_id)):
                return
        except Exception:
            logger.debug("Could not read pending outbox for %s", save_id, exc_info=True)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._run_outbox_delivery(save_id))
        self._delivery_tasks[save_id] = task

        def _clear(done_task: asyncio.Task) -> None:
            if self._delivery_tasks.get(save_id) is done_task:
                self._delivery_tasks.pop(save_id, None)

        task.add_done_callback(_clear)

    def _delivery_read_only(self, save_id: str) -> bool:
        """Current native ownership, not the event source, gates execution."""
        if self.actor_mode == "player" and self.control_owner != "agent":
            return True
        if self._preset_business_blocked(save_id):
            return True
        if self._work_store is not None and self._work_store.state(save_id).paused:
            return True
        if self._bedtime_winding_down(save_id):
            return True
        return self._job_in_flight(save_id)

    def _settle_delivery(self, request_id: str, ok: bool) -> None:
        pending = self._delivery_pending.pop(request_id, None)
        if pending is not None and not pending.done():
            pending.set_result(ok)

    async def _run_outbox_delivery(self, save_id: str) -> None:
        """Deliver persisted terminal facts as the next turn of the same session.

        Facts are consumed after a successful model turn with a persisted reply.
        Pending player notifications replay that reply until the native receipt;
        socket failure never requires another model turn or native command.
        """
        try:
            while (
                self._busy_lock.locked()
                or self._active_task is not None
                or self._life_queue
                or self._life_draining
                or (self._plan_worker is not None and self._plan_worker._provider_active)
                or (self._plan_worker is not None and getattr(self._plan_worker, "_executing", False) is True)
            ):
                await asyncio.sleep(0.05)
            if self._work_store is None:
                return
            for notification in self._work_store.pending_outbox_notifications(save_id):
                await self._send_reply(self._chat_ws, Envelope.from_mapping(notification))
            events = self._work_store.pending_outbox(save_id)
            if not events:
                return
            if any((event.get("delivery") or {}).get("retryBlocked") for event in events):
                return
            ws = self._chat_ws
            if ws is None:
                return
            self._work_store.recover(save_id)
            read_only = self._delivery_read_only(save_id)
            midflight = [] if read_only else self._work_store.pending_midflight_requests(save_id)
            requests_by_id = {str(r.get("id")): r for r in self._work_store.state(save_id).requests}
            event_requests = {str(event["requestId"]) for event in events if event.get("requestId")}
            if (not midflight and event_requests
                    and all(requests_by_id.get(root, {}).get("workStopped") for root in event_requests)):
                read_only = True
            visible = any(
                event.get("requestId") and not str(event.get("requestId")).startswith("autonomy-")
                for event in events
            )
            request_id = f"{'' if visible else 'autonomy-'}delivery-{uuid.uuid4().hex[:12]}"
            root = next(
                (str(event["requestId"]) for event in events
                 if event.get("requestId") and not str(event.get("requestId")).startswith("autonomy-")),
                None,
            ) or request_id
            ids = [str(event["id"]) for event in events if event.get("id")]
            # The delivery turn continues the originating goal thread: a scoped
            # job's follow-up dispatch must stay inside the same active goal so
            # execution-scope constraints survive. A goal that ended between the
            # terminal and this boundary falls back to the unscoped default.
            goal_scope = None
            try:
                active_goal_ids = {
                    goal.id for goal in self._work_store.state(save_id).goals if goal.status == "active"
                }
                goal_scope = next(
                    (str(event["goalId"]) for event in events
                     if event.get("goalId") and str(event.get("goalId")) in active_goal_ids),
                    None,
                )
            except Exception:
                goal_scope = None
            self._work_store.mark_outbox_sent(save_id, ids)
            done: asyncio.Future = asyncio.get_running_loop().create_future()
            self._delivery_pending[request_id] = done
            try:
                await self.handle_chat_submit(
                    ws, request_id, "", save_id,
                    _goal_default=goal_scope,
                    _delivery={
                        "events": events,
                        "midflight": midflight,
                        "midflight_ids": [m["id"] for m in midflight],
                        "read_only": read_only,
                        "command_id": root,
                    },
                )
            finally:
                self._delivery_pending.pop(request_id, None)
            delivered = done.result() if done.done() else False
            if delivered and self._work_store is not None:
                self._work_store.mark_outbox_consumed(save_id, ids)
                # Delivering A's terminal facts does not resolve queued request B.
                # Only B's own selected job (or an explicit resolution) consumes
                # it; a provider reply without a plan is merely an acknowledgement.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Outbox delivery turn failed for %s", save_id, exc_info=True)

    async def wait_for_deliveries(self, timeout: float = 5.0) -> None:
        tasks = [task for task in self._delivery_tasks.values() if not task.done()]
        if not tasks:
            return
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )

    def _format_delivery_prompt(
        self,
        events: list[dict[str, Any]],
        save_id: str,
        *,
        midflight: list[dict[str, Any]],
        read_only: bool,
        instructions: str | None = None,
    ) -> str:
        live = self._decision_context(save_id, origin="delivery")
        live.pop("reloadSummary", None)
        context = render_decision_context(model_decision_context(live))
        requests_by_id: dict[str, dict[str, Any]] = {}
        request_totals: dict[str, Any] = {}
        tasks_by_id: dict[str, Any] = {}
        if self._work_store is not None and save_id:
            try:
                request_totals = self._work_store.overview(save_id).get("confirmedRequestTotals") or {}
                state = self._work_store.state(save_id)
                requests_by_id = {
                    str(r.get("id")): r for r in state.requests
                }
                tasks_by_id = {entry["task"]["id"]: entry["task"] for entry in state.archive
                               if isinstance(entry.get("task"), dict) and entry["task"].get("id")}
                tasks_by_id.update({task.id: task for task in state.tasks})
            except Exception:
                requests_by_id = {}
        facts = []
        for event in events:
            record = requests_by_id.get(str(event.get("requestId"))) or {}
            feedback = event.get("feedback")
            task = tasks_by_id.get(str(event.get("taskId")))
            if task is not None and isinstance(feedback, dict):
                feedback = project_task_feedback_state(feedback, task)
            facts.append({
                "kind": event.get("kind"),
                "commandId": event.get("commandId"),
                "taskId": event.get("taskId"),
                "goalId": event.get("goalId"),
                "requestId": event.get("requestId"),
                "operation": event.get("operation"),
                "outcome": event.get("outcome"),
                "reasonCode": event.get("reasonCode"),
                "effects": event.get("effects"),
                "feedback": feedback,
                "requestText": record.get("text"),
                "requestWorkStopped": bool(record.get("workStopped")),
                "requestStopReason": record.get("workStoppedReason"),
            })
        payload = {
            "qualityCodes": {"0": "普通", "1": "银星", "2": "金星", "4": "铱星"},
            "terminalFacts": facts,
            "playerMessagesWhileWorking": [
                {"requestId": m.get("id"), "text": m.get("text")} for m in midflight
            ],
            "confirmedRequestTotals": {
                request_id: request_totals[request_id]
                for request_id in dict.fromkeys(
                    [str(event.get("requestId")) for event in events]
                    + [str(message.get("id")) for message in midflight])
                if request_id in request_totals
            },
        }
        payload["workDispatchAvailable"] = not read_only
        return (
            f"{instructions or decision_policy()}\n"
            f"实时上下文：{context}\n"
            f"终态事实：{json.dumps(payload, ensure_ascii=False)}\n"
            "结合实际结果和玩家的新安排，说明进展并接续剩余工作。"
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
        if save_id and self._work_store is not None and not active_task.read_only:
            scope = active_task.goal_scope or (self._autonomy.state(save_id).goal_scope
                     if self._autonomy and active_task.request_id.startswith("autonomy-") else None)
            state = self._work_store.state(save_id)
            goal = next((g for g in state.goals if g.id == scope), None)
            source_id = goal.constraints.get("playerRequestId") if goal else None
            source = next((r for r in state.requests if r.get("id") == source_id), None)
            if source:
                prompt += "\n玩家原始指令（保留动作限制、汇报要求与统计范围；当前已采纳范围以目标为准）：" + json.dumps(
                    {"text": source["text"]}, ensure_ascii=False)
            self._work_store.begin_decision(
                save_id, token, goal_scope=scope, goal_default=active_task.goal_default,
                request_id=source_id if source else active_task.command_id or active_task.request_id,
                request_ids=list(dict.fromkeys([
                    *active_task.work_request_ids,
                    *(str(request["id"]) for request in self._work_store.pending_midflight_requests(save_id)
                      if not active_task.read_only),
                ])),
            )
            active_task.decision_token = token
            active_task.decision_day = self._work_store.state(save_id).last_settled_day
        previous_token = os.environ.get("STARDEW_DECISION_TOKEN")
        previous_surface = os.environ.get("STARDEW_MCP_SURFACE")
        # Capability discovery stays fixed; only cached reads/execution admission differ.
        os.environ["STARDEW_MCP_SURFACE"] = "life" if active_task.read_only else "light"
        if active_task.read_only:
            os.environ.pop("STARDEW_DECISION_TOKEN", None)
        else:
            os.environ["STARDEW_DECISION_TOKEN"] = token
        try:
            backend = self._get_backend()
            if isinstance(backend, (KimiBackend, CodexBackend, DshBackend, McodeBackend)):
                backend.progress = getattr(self, "_backend_progress_callback", None)
            turn_native_session = getattr(self, "_player_session_id", None)
            for attempt in range(2):
                delivery_key, sent_prompt, pending_prompt = self._prepare_prompt_delivery(
                    active_task, conversation_id, prompt, backend)
                result = backend.run(active_task, conversation_id, sent_prompt)
                if attempt:
                    result["session_retried"] = True
                if (attempt == 0 and conversation_id and save_id and not active_task.cancelled
                        and (self.backend_name, result.get("error")) in {
                            ("mcode", "MCODE_SESSION_UNAVAILABLE"),
                            ("codex", "CODEX_SESSION_UNAVAILABLE"),
                            ("codex", "CODEX_CONTEXT_EXHAUSTED"),
                        }):
                    self._rotate_provider_session(save_id, reason=str(result["error"]))
                    conversation_id = None
                    continue
                break
            delivered_session = result.get("conversation_id")
            if delivered_session and not active_task.cancelled and not result.get("error"):
                self._acknowledge_prompt_delivery(delivery_key, pending_prompt, save_id, str(delivered_session))
                if self.actor_mode == "player":
                    self._work_reload_context(active_task.save_id, str(delivered_session), delivered=True,
                                              expected_game_session_id=turn_native_session)
            logger.info("Prompt delivery save=%s session=%s version=%s originalBytes=%s newBytes=%s",
                        active_task.save_id, delivered_session, pending_prompt["version"],
                        pending_prompt["originalPromptBytes"], pending_prompt["newPromptBytes"])
            return result
        finally:
            if previous_surface is None:
                os.environ.pop("STARDEW_MCP_SURFACE", None)
            else:
                os.environ["STARDEW_MCP_SURFACE"] = previous_surface
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
        requested_cid = conversation_id
        # AGY MCP workers outlive CLI turns. Refresh the exact authorization
        # through the same explicit per-run context mechanism used by other
        # persistent provider workers; never bind to the global common install.
        if self.run_dir:
            context = {key: os.environ[key] for key in
                       ("STARDEW_LIFE_MODE", "STARDEW_LIFE_TURN_ID", "STARDEW_LIFE_SAVE_ID", "STARDEW_DECISION_TOKEN")
                       if key in os.environ}
            path = Path(self.run_dir) / "data" / "agy-turn-context.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(context), encoding="utf-8")
            temp.replace(path)
        server = os.environ.get("STARDEW_AGY_GAME_SERVER")
        if server:
            prompt = f"本回合游戏工具仅来自 MCP 服务 {server}，对应当前已绑定隔离存档；不要调用其他游戏服务。\n" + prompt
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
            def on_conversation(cid: str) -> None:
                if cid != requested_cid:
                    active_task.start_max_idx = -1
                    active_task.start_max_step_idx = -1

            outcome = run_agy_process(active_task, cmd, prompt, conversation_id,
                run_dir=self._commands_file.parent,
                model=self.model or "", start_idx=active_task.start_max_idx,
                timeout_seconds=600, on_conversation=on_conversation)
            conversation_id = outcome.conversation_id or conversation_id
            returncode = outcome.returncode
            stdout, stderr = outcome.stdout, outcome.stderr
            if outcome.timed_out:
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

            if active_task.cancelled or outcome.cancelled:
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
            step_baseline = -1 if (cid and requested_cid and cid != requested_cid) else active_task.start_max_step_idx
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
            is_new_session = requested_cid is None
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
            is_error = (returncode != 0) or (op_status not in ("SUCCESS", None))
            if is_error:
                cli_err = (parsed.get("error") or "") if is_new_session else ""
                err_text = f"{stderr}\n{cli_err}".strip()
                if err_text:
                    err_marker = err_text[:200]
                    resp = response_text if response_text else f"AI 执行出错 (退出码 {returncode})：{err_text[:200]}"
                else:
                    err_marker = f"AGY_EXIT_{returncode}" if returncode != 0 else f"AGY_STATUS_{op_status}"
                    resp = response_text if response_text else f"AI 执行出错 (退出码 {returncode})"
                logger.error("agy exited with error: code=%s, status=%s, err=%s", returncode, op_status, err_text[:300] or err_marker)
                return {
                    "success": False,
                    "response": resp,
                    "error": err_marker,
                    "conversation_id": cid,
                    "duration": duration,
                }

            # Plain text output fallback if proc.returncode == 0
            clean_stdout = stdout.strip()
            if returncode == 0 and clean_stdout:
                return {
                    "success": True,
                    "response": clean_stdout,
                    "conversation_id": conversation_id,
                    "duration": duration,
                }

            return {
                "success": False,
                "response": f"AI 未能正常生成回复 (code {returncode})",
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

    async def _push_work_state(self, ws: WebSocketClient | None, save_id: str) -> None:
        if ws is None:
            return
        work = self._life_work_projection(save_id)
        signature = json.dumps(work, sort_keys=True, ensure_ascii=False)
        if self._last_work_projection.get(save_id) == signature:
            return
        profile = self._profile_store.get(save_id) if self._profile_store else {}
        reply = Envelope.create_life_profile_state(
            self.instance_id, "work-update", save_id, profile.get("profile"),
            int(profile.get("profileRevision") or 0), work=work)
        if await self._send_reply(ws, reply):
            self._last_work_projection[save_id] = signature

    async def _maybe_notify_preset_terminal(self, ws: WebSocketClient | None, save_id: str) -> None:
        """Report an autostart preset run's terminal goal state to the Mod exactly once.

        The Mod tracks the run lifecycle off the stable preset request id; the bound
        goal may complete during an autonomy continuation turn, so completion is
        detected here from the persisted work store rather than from a single turn.
        """
        if ws is None or not save_id or self._work_store is None:
            return
        policy = getattr(self, "_preset_day_boundary", None)
        ended = policy.reconcile(save_id, self._work_store, self._latest_snapshot_payload) if policy else None
        if ended:
            self._autonomy.set_goal_scope(save_id, None)
            existing = self._preset_runs.get(save_id)
            if not existing or existing.get("request_id") != ended["requestId"]:
                self._preset_runs[save_id] = {"request_id": ended["requestId"], "goal_id": ended["goalId"], "notified": False}
        run = self._preset_runs.get(save_id)
        if not run or run.get("notified"):
            return
        goal_id = run.get("goal_id")
        if not goal_id:
            return
        goal = next((g for g in self._work_store.state(save_id).goals if g.id == goal_id), None)
        if goal is None or goal.status not in {"completed", "cancelled"}:
            return
        run["notified"] = True
        request_id = run["request_id"]
        status = "completed" if goal.status == "completed" else "cancelled"
        text = ("本轮限定游戏日已通过原生睡觉保存结束，停止派工；实际成果以原生结算记录为准。" if goal.status == "completed"
                else "本轮农场照料目标已取消。")
        if ended:
            facts = ended.get("verifiedEffectsBeforeSleep") or {}
            if ended.get("phase") == "completed" and ended.get("effectsScopeRequestId") == request_id:
                text += f" 本轮原生账：浇水{facts.get('watered', 0)}格，收获{facts.get('harvested', 0)}格；已核验原生睡觉保存。"
            elif ended.get("phase") == "completed":
                text += " 重载检查点缺少本轮动作明细，数量不重报；原生睡觉保存已核验。"
            elif ended.get("reason"):
                text += " " + ended["reason"]
        reply = Envelope.create_chat_reply(
            self.instance_id, request_id, status, text,
            save_id=save_id, command_id=request_id, command_complete=True)
        reply.payload["presetGoalId"] = goal_id
        # The dedicated terminal field is the Mod's ONLY completion authority for
        # the run; the generic status/commandComplete on this reply describe just
        # this notification turn.
        reply.payload["presetGoalTerminal"] = goal.status
        await self._send_reply(ws, reply)
        logger.info("Preset goal [%s] reached terminal state %s; notified Mod", request_id, status)

    # ---------------------------------------------------- player mode control
    def _track_actor_mode(self, payload: Any) -> None:
        """Learn player-mode facts from the snapshot's additive fields."""
        if not isinstance(payload, dict):
            return
        actor_mode = payload.get("actorMode")
        if actor_mode in {"companion", "player"} and actor_mode != self.actor_mode:
            logger.info("Actor mode observed on chat channel: %s (was %s)", actor_mode, self.actor_mode)
            self.actor_mode = actor_mode
        control_owner = payload.get("controlOwner")
        if control_owner in {"human", "agent"}:
            self.control_owner = control_owner

    async def _maybe_claim_player_control(
        self, ws: WebSocketClient | None, save_id: str | None
    ) -> None:
        """Claim native player control once per connection (player mode only)."""
        if save_id and self._manual_takeover.get(save_id):
            # An explicit F9 takeover persists as manual across reconnects; only an
            # explicit re-handoff (owner=agent from the mod) clears it. Never
            # re-claim immediately after a reconnect.
            logger.info(
                "Skipping player.control auto-claim: explicit human takeover in effect for save %s until re-handoff",
                save_id)
            return
        self._player_claim_attempted = True
        request_id = f"player-claim-{uuid.uuid4().hex[:8]}"
        envelope = Envelope.create_player_control(
            self.instance_id, request_id, "claim", save_id=save_id, game_session_id=getattr(self,"_player_session_id",None))
        if await self._send_reply(ws, envelope):
            logger.info(
                "Sent player.control claim [%s] (actorMode=player, saveId=%s)",
                request_id, save_id)
        else:
            logger.warning(
                "player.control claim [%s] could not be sent; control stays with its current owner",
                request_id)

    async def _handle_player_authority(
        self,
        ws: WebSocketClient | None,
        payload: dict[str, Any],
        active_save_id: str | None,
    ) -> None:
        """Apply a control-owner change reported by the mod (claim result, F9 takeover, unload)."""
        owner = payload.get("owner")
        if owner not in {"human", "agent"}:
            return
        if self.actor_mode != "player":
            logger.debug("Ignoring player.authority owner=%s in companion mode", owner)
            return
        reason = str(payload.get("reason") or "")
        save_id = str(payload.get("saveId") or active_save_id or "")
        previous = self.control_owner
        self.control_owner = owner
        if owner == "agent":
            # An explicit re-handoff (or the bootstrap claim) ends manual mode;
            # work may resume under agent authority.
            if save_id:
                was_manual = self._manual_takeover.pop(save_id, None)
                if was_manual and self._autonomy is not None:
                    self._autonomy.set_paused(save_id, False)
            self._maybe_arm_hold_recovery(save_id, self._latest_snapshot_payload)
            self._maybe_deliver_outbox(save_id)
            if previous != "agent":
                logger.info(
                    "Player control held by the agent (%s); work dispatch may resume",
                    reason or "no reason")
            return
        if reason in {"human-takeover", "awaiting-user"} and save_id:
            # Explicit F9 takeover persists as manual across bridge reconnects:
            # no auto re-claim until the mod reports owner=agent again.
            self._manual_takeover[save_id] = True
        if reason in {"human-takeover", "awaiting-user", "disconnected"} and save_id:
            # Handing the character back is a pause, not cancellation of the
            # player's durable goal. Native actions are stopped by the Mod.
            self._autonomy_generation += 1
            if self._active_task is not None and not self._active_task.cancelled:
                self.abort_active_task(f"player_takeover: {reason}")
            if self._autonomy is not None:
                self._autonomy.set_paused(save_id, True)
            if self._work_store is not None:
                self._work_store.revoke_decision(save_id)
            return
        logger.warning(
            "Player control taken over by the human (%s); aborting current agent work",
            reason or "no reason")
        # A preset (independent autostart) run whose bound goal is still active is
        # paused, not cancelled: the human takeover stops agent work immediately,
        # and an explicit re-handoff resumes the same persisted goal instead of
        # replays or duplicates. Non-preset work keeps the existing cancel path.
        preset_run = self._preset_runs.get(save_id) if save_id else None
        preset_goal = None
        if self._work_store is not None and save_id:
            if preset_run is None:
                # The in-memory run table is lost on a bridge restart; recover the
                # preset binding from the persisted work store so a takeover before
                # the next resume dispatch still pauses instead of cancelling.
                state = self._work_store.state(save_id)
                for record in state.requests:
                    record_id = str(record.get("id") or "")
                    if not self._is_preset_request(record_id) or not record.get("goalId"):
                        continue
                    candidate = next((g for g in state.goals if g.id == record["goalId"]), None)
                    if candidate is not None and candidate.status == "active":
                        preset_run = {"request_id": record_id, "goal_id": candidate.id, "notified": False}
                        self._preset_runs[save_id] = preset_run
                        break
            if preset_run:
                preset_goal = next(
                    (g for g in self._work_store.state(save_id).goals
                     if g.id == preset_run.get("goal_id")), None)
        if preset_goal is not None and preset_goal.status == "active":
            self._autonomy_generation += 1
            task = self._active_task
            if task is not None and not task.cancelled:
                self.abort_active_task(f"player_takeover: {reason or 'human takeover'}")
            if self._autonomy is not None:
                self._autonomy.set_paused(save_id, True)
            try:
                self._work_store.revoke_decision(save_id)
            except Exception:
                logger.debug("Decision revoke after human takeover failed", exc_info=True)
            logger.info(
                "Preset run [%s] paused on human takeover; goal %s stays active for re-handoff resume",
                preset_run.get("request_id"), preset_goal.id)
            return
        # Reuse the existing chat.cancel machinery: abort the active provider
        # turn, withdraw dispatched work and stop new dispatches. Nothing new
        # is dispatched until a fresh player.control claim is acknowledged
        # with owner=agent.
        self._autonomy_generation += 1
        task = self._active_task
        if task is not None and not task.cancelled:
            await self.handle_chat_cancel(
                ws, task.request_id, f"player_takeover: {reason or 'human takeover'}",
                save_id or task.save_id or None)
        elif self._autonomy is not None and save_id:
            self._autonomy.control(save_id, "cancel")
        if self._work_store is not None and save_id:
            try:
                self._work_store.revoke_decision(save_id)
            except Exception:
                logger.debug("Decision revoke after human takeover failed", exc_info=True)

    @contextlib.asynccontextmanager
    async def _thinking_pause(self, request_id: str, save_id: str | None):
        """Pause the world clock around one provider model turn (player mode).

        Sends agent.thinking begin before the provider call and always sends
        the matching end (success, provider failure or cancellation) in a
        finally. The /chat channel never sees mod.welcome, so the bridge
        treats a player-mode snapshot as supporting the lease protocol and
        the mod-side pause policy stays authoritative; a failed send is
        logged and ignored because the mod expires thinking leases itself.
        """
        enabled = (
            getattr(self, "actor_mode", None) == "player"
            and getattr(self, "supports_thinking_pause", None) is not False
        )
        if not enabled:
            yield
            return
        lease_id = uuid.uuid4().hex[:12]
        lease_ws = self._chat_ws
        lease_session = getattr(self,"_player_session_id",None)
        renewal_task = None
        # Both production providers have a 600-second call deadline. Keep an
        # additional absolute safety bound in case an executor itself hangs.
        renewal_deadline = time.monotonic() + 630.0
        async def renew_active_provider():
            while True:
                await asyncio.sleep(getattr(self,"thinking_renew_interval_seconds",1.0))
                if self._chat_ws is not lease_ws or getattr(self,"_player_session_id",None) != lease_session:
                    return
                if time.monotonic() >= renewal_deadline:
                    logger.warning("Thinking renewal stopped at provider safety deadline [%s]", lease_id)
                    return
                active = getattr(self,"_active_task",None)
                if getattr(self,"control_owner",None) == "human" or (active is not None and active.request_id == request_id and active.cancelled):
                    return
                renew = Envelope.create_agent_thinking(self.instance_id,request_id,"renew",lease_id,
                    save_id=save_id,game_session_id=lease_session)
                if not await self._send_reply(lease_ws,renew):
                    return
        begin = Envelope.create_agent_thinking(
            self.instance_id, request_id, "begin", lease_id, save_id=save_id, game_session_id=lease_session)
        try:
            if not await self._send_reply(lease_ws, begin):
                logger.warning(
                    "agent.thinking begin [%s] could not be sent; continuing without a pause lease",
                    lease_id)
            else:
                renewal_task=asyncio.create_task(renew_active_provider())
            yield
        finally:
            if renewal_task is not None:
                renewal_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewal_task
            end = Envelope.create_agent_thinking(
                self.instance_id, request_id, "end", lease_id, save_id=save_id, game_session_id=lease_session)
            try:
                if not await self._send_reply(lease_ws, end):
                    logger.warning("agent.thinking end [%s] could not be sent", lease_id)
            except Exception:
                logger.warning("agent.thinking end [%s] send failed", lease_id, exc_info=True)

    async def _send_reply(self, ws: WebSocketClient | None, reply: Envelope) -> bool:
        if reply.message_type == "chat.reply" and self._active_task is not None and self._active_task.read_only and (
            reply.payload.get("requestId") == self._active_task.request_id
        ):
            reply.payload["readOnly"] = True
        if reply.message_type in {"chat.reply", "life.chat.reply"} and reply.payload.get("status") in {
            "completed", "failed", "cancelled", "interrupted", "selected", "decision-completed", "partial", "unknown",
        }:
            await self._push_usage_state(ws, str(reply.payload.get("saveId") or ""))
        if reply.message_type in {"chat.reply", "life.chat.reply", "autonomy.state"} and ws is not None and reply.payload.get("saveId"):
            save_id = str(reply.payload["saveId"])
            await self._push_work_state(ws, save_id)
        if reply.message_type == "chat.reply" and str(reply.payload.get("requestId", "")).startswith("autonomy-"):
            if reply.payload.get("status") not in {"failed", "job-failed", "rejected", "unknown", "partial"}:
                logger.info("Quiet autonomous reply: %s", json.dumps(dict(reply.payload), ensure_ascii=False))
                return True
        if ws is None:
            return False
        try:
            json_text = json.dumps(reply.to_mapping(), ensure_ascii=False)
            await ws.send_text(json_text)
            return True
        except Exception as ex:
            logger.warning("Failed to send chat reply to WebSocket: %s", ex)
            return False


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
        "--owner-pid", type=int, default=None,
        help="Windows game process owning this automatic launch; exit when it ends",
    )
    parser.add_argument(
        "--backend", type=str, choices=["agy", "kimi", "codex", "dsh", "mcode"], default=None,
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
        choices=["default", "low", "medium", "high", "xhigh"],
        help="Reasoning effort for Codex/agy (xhigh requires Codex); default uses provider settings",
    )
    parser.add_argument(
        "--agy-cmd",
        type=str,
        default="agy.exe",
        help="Path or name of the agy CLI binary",
    )
    parser.add_argument("--kimi-cmd", type=str, default="kimi.exe", help="Path or name of Kimi CLI")
    parser.add_argument("--mcode-cmd", type=str, default="mcode", help="Path or name of MiniMax Code CLI")

    args = parser.parse_args(argv)
    if args.owner_pid is not None and not 0 < args.owner_pid <= 0xFFFFFFFF:
        parser.error("--owner-pid must be a positive Windows process ID")

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    owner_guard = OwnerProcessGuard(args.owner_pid) if args.owner_pid is not None else None
    try:
        _main_bridge(args, owner_guard)
    finally:
        # The module CLI keeps its daemon watchdog armed through interpreter
        # shutdown. Explicit argv callers may release their embedded monitor.
        if argv is not None and owner_guard is not None:
            owner_guard.close()


def _main_bridge(args: argparse.Namespace, owner_guard: OwnerProcessGuard | None) -> None:
    # Start ownership monitoring before construction as configuration and model
    # setup can block too. Manual launches retain the existing resident service.
    if owner_guard is not None and owner_guard.owner_exited.is_set():
        return
    bridge = ChatBridge(
        run_dir=args.run_dir,
        model=args.model,
        effort=args.effort,
        agy_cmd=args.agy_cmd,
        backend=args.backend,
        kimi_cmd=args.kimi_cmd,
        mcode_cmd=args.mcode_cmd,
    )

    _, log_file_path = configure_chat_bridge_file_logging(bridge.run_dir)
    logger.info("ChatBridge 文件日志已启动: %s", log_file_path)
    if owner_guard is not None:
        logger.info("Game owner guard active (owner_pid=%d)", args.owner_pid)

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
        asyncio.run(owner_guard.run(bridge.run) if owner_guard is not None else bridge.run())
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
