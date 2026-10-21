"""Native ACP maintenance of an existing game session. Never creates a session."""
from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .provider_diagnostics import redact_text


class _ACPRequestError(RuntimeError):
    """Retain bounded public error data hidden behind ACP's generic message."""

    def __init__(self, method: str, error: dict[str, Any]):
        self.method = method
        self.rpc_code = error.get("code")
        parts: list[str] = []

        def collect(value: Any, depth: int = 0) -> None:
            if depth > 4 or len(parts) >= 12:
                return
            if isinstance(value, dict):
                for key in ("code", "message", "type", "status", "statusCode", "error", "data", "detail", "details", "cause", "exception", "reason"):
                    if key in value:
                        collect(value[key], depth + 1)
            elif isinstance(value, list):
                for item in value[:3]:
                    collect(item, depth + 1)
            elif isinstance(value, (str, int)):
                parts.append(str(value)[:800])

        collect(error)
        super().__init__(redact_text(" | ".join(parts))[:1200] or "ACP request failed")


def _counters(session_id: str, after: int | None = None) -> list[tuple]:
    path = Path(os.environ.get("MINIMAX_DATA_DIR") or Path.home() / ".minimax") / "v2/sqlite/runtime-state.sqlite"
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2) as connection:
            if after is None:
                return connection.execute("SELECT COALESCE(MAX(id),0) FROM local_runtime_token_usage WHERE session_id=?", (session_id,)).fetchall()
            return connection.execute(
                "SELECT input_tokens,output_tokens,cache_read_tokens,cache_write_tokens "
                "FROM local_runtime_token_usage WHERE session_id=? AND id>? ORDER BY id", (session_id, after)).fetchall()
    except (OSError, ValueError, sqlite3.Error):
        return []


def compact_existing_session(backend: Any, active_task: Any, session_id: str) -> dict[str, Any]:
    """Use ACP's registered /compact command, with no session/new or game prompt."""
    started = time.monotonic()
    boundary = _counters(session_id)
    before = boundary[0][0] if boundary else None
    workspace = backend.run_dir / "data/mcode"
    metadata_path = workspace / "compaction-provider-error-metadata.jsonl"
    assert metadata_path.resolve().is_relative_to(workspace.resolve())
    metadata_started = False
    process = None
    result: dict[str, Any] = {"success": False, "sessionId": session_id,
                              "method": "acp-native-compact", "sessionNewRequests": 0}
    try:
        command = backend._command(backend._executable(), workspace, session_id)
        command = command[:command.index("exec")] + ["acp"]
        if active_task.cancelled:
            raise RuntimeError("Compaction cancelled before process start")
        environment = os.environ.copy()
        hook = Path(__file__).with_name("mcode_compaction_errors.mjs").resolve()
        metadata_path.write_text("", encoding="utf-8")
        metadata_started = True
        environment["STARDEW_MCODE_COMPACTION_ERROR_METADATA"] = str(metadata_path.resolve())
        environment["NODE_OPTIONS"] = (environment.get("NODE_OPTIONS", "") + " --import=" + hook.as_uri()).strip()
        process = subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", env=environment,
                                   **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        active_task.process = process
        messages: queue.Queue = queue.Queue()
        commands: set[str] = set()
        output: list[str] = []
        capture = False
        deadline = started + min(180, backend.timeout_seconds)

        def read_stdout() -> None:
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
            messages.put(None)

        def drain_stderr() -> None:
            for _ in process.stderr:
                pass  # Account settings, replayed history and stderr are never persisted.

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=drain_stderr, daemon=True).start()

        def send(value: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            process.stdin.flush()

        def request(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
            while time.monotonic() < deadline:
                if active_task.cancelled:
                    raise RuntimeError("Compaction cancelled")
                try:
                    message = messages.get(timeout=min(.25, max(.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if message is None:
                    raise RuntimeError("ACP closed before native compaction receipt")
                if message.get("method") == "session/update":
                    params = message.get("params") or {}
                    if params.get("sessionId") != session_id:
                        raise RuntimeError("ACP returned a different session; no new session is permitted")
                    update = params.get("update") or {}
                    if update.get("sessionUpdate") == "available_commands_update":
                        commands.update(str(c.get("name")) for c in update.get("availableCommands", []) if isinstance(c, dict))
                    if capture and update.get("sessionUpdate") == "agent_message_chunk":
                        content = update.get("content") or {}
                        if content.get("type") == "text" and sum(map(len, output)) < 8000:
                            output.append(str(content.get("text") or "")[:3000])
                if "method" in message and "id" in message:
                    # Native compaction needs no filesystem, terminal or game tool.
                    if message["method"] == "session/request_permission":
                        send({"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                    else:
                        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Context maintenance only"}})
                if message.get("id") == identifier and "method" not in message:
                    if message.get("error"):
                        raise _ACPRequestError(method, message["error"])
                    return message.get("result") or {}
            raise TimeoutError("Native compaction deadline exceeded")

        request(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "stardew-context-maintenance", "version": "1"}})
        loaded = request(2, "session/load", {"sessionId": session_id, "cwd": str(workspace), "mcpServers": []})
        if "compact" not in commands:
            raise RuntimeError("Native compact command is not available in this installed mcode")
        options = {item.get("id"): item for item in loaded.get("configOptions", [])}
        selected_model = str((options.get("model") or {}).get("currentValue") or "")
        if backend.model:
            provider, _, model = backend.model.partition("/")
            if not selected_model.startswith(f"m:{provider}:{model}:v:"):
                raise RuntimeError("Loaded compaction model does not match the configured game model")
        effort = (options.get("thinkingEffort") or {}).get("currentValue")
        if backend.effort and backend.effort != "default" and effort != backend.effort:
            request(3, "session/set_config_option", {"sessionId": session_id, "configId": "thinkingEffort", "value": backend.effort})
        capture = True
        response = request(4, "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text":
            "/compact Preserve exact player constraints, active authorized goals, unresolved native commands and receipts, "
            "failure recovery conditions, dated observations versus hypotheses, field siting decisions, inventory intent "
            "and next intended action. This is a Stardew Valley SMAPI saved-game session. Historical commands must not "
            "be replayed; fresh native state overrides stale observations. Preserve source and uncertainty labels."}]})
        public = "".join(output)
        compacted = "Compaction completed." in public
        noop = "No compaction is needed" in public
        if response.get("stopReason") != "end_turn" or not (compacted or noop):
            raise RuntimeError("No successful native compaction receipt")
        result.update(success=True, compacted=compacted, noCompactionNeeded=noop, model=selected_model)
        for label in ("Messages", "Tokens"):
            match = re.search(label + r":\s*([\d,]+)\s*→\s*([\d,]+)", public)
            if match:
                result[label.lower() + "Before"] = int(match[1].replace(",", ""))
                result[label.lower() + "After"] = int(match[2].replace(",", ""))
    except Exception as error:
        detail = redact_text(str(error))[:1200]
        code = "RESOURCE_EXHAUSTED" if "2067" in detail else "MCODE_COMPACTION_FAILED"
        result["error"] = {"code": code, "message": detail}
        if isinstance(error, _ACPRequestError):
            result["error"].update(rpcMethod=error.method, rpcCode=error.rpc_code)
    finally:
        if process is not None:
            backend.terminate(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        active_task.process = None
    try:
        metadata = [json.loads(line) for line in metadata_path.read_text(encoding="utf-8").splitlines() if line.strip()] if metadata_started else []
        if metadata:
            result["providerErrorMetadata"] = metadata[-4:]
            if not result["success"] and any(str(row.get("code")) == "2067" for row in metadata):
                result["error"] = {**result.get("error", {}), "code": "RESOURCE_EXHAUSTED",
                    "message": "mcode Token Plan quota exhausted (provider 2067). Existing session retained; reset window is not identified by this response."}
        if metadata_started:
            metadata_path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    rows = _counters(session_id, before) if before is not None else []
    if rows and all(type(n) is int and n >= 0 for row in rows for n in row):
        inputs, outputs, reads, writes = (sum(row[index] for row in rows) for index in range(4))
        result["usage"] = {"input_tokens": inputs + reads + writes, "output_tokens": outputs,
            "cache_read_tokens": reads, "cache_creation_tokens": writes,
            "total_tokens": inputs + reads + writes + outputs, "generations_count": len(rows),
            "input_includes_cache": True, "input_context_measured": False, "source": "mcode_native_compaction_counter"}
    result["duration"] = round(time.monotonic() - started, 3)
    return result
