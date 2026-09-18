"""Provider-specific token metering for the Kimi CLI.

The Kimi CLI does not publish usage on its ``stream-json`` stdout.  It records
one ``usage.record`` line per model request in the session wire file:

    <home>/.kimi-code/sessions/<workspace-id>/<session-id>/agents/main/wire.jsonl

Each record carries ``{"inputOther", "output", "inputCacheRead",
"inputCacheCreation"}`` plus ``usageScope``.  This module reads only the slice of
that file produced during one Bridge request (byte offset captured before the
turn), so a resumed session's cumulative totals are never reported as one turn.

Two different quantities are reported and must not be confused:

* **billing totals** — the sum of every field over every record, used for the
  usage/cost report;
* **request input context** — ``inputOther + inputCacheRead + inputCacheCreation``
  of a *single* request.  That single number approximates how large the context
  the provider had to read for that request was.  Summing it across requests
  double-counts replayed and cached context, so the raw per-request list is kept
  and only the latest/maximum single-request values are exposed for the
  context-budget rotation.

The wire file is read-only.  Missing files, missing fields, or an unrecognised
schema produce "unknown" (``None`` counters and an ``unknown`` flag) instead of a
fabricated zero, and the source is always preserved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("stardew_ai_runtime.kimi_wire_usage")

KIMI_WIRE_SOURCE = "kimi_wire_usage_record"

# Wire field -> public aggregate key.  Alternative spellings are accepted so a
# minor CLI rename degrades to "unknown field" rather than a wrong sum.
_INPUT_OTHER_KEYS = ("inputOther", "input_other", "inputTokensOther", "nonCachedInput")
_OUTPUT_KEYS = ("output", "outputTokens", "completion_tokens")
_CACHE_READ_KEYS = ("inputCacheRead", "input_cache_read", "cacheRead", "cache_read_tokens")
_CACHE_CREATION_KEYS = ("inputCacheCreation", "input_cache_creation", "cacheCreation", "cache_creation_tokens")


def _workspace_id_for_root(root: str, home: Path) -> list[str]:
    workspaces_file = home / ".kimi-code" / "workspaces.json"
    try:
        data = json.loads(workspaces_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    normalize = lambda value: str(value).replace("/", "\\").rstrip("\\").lower()  # noqa: E731
    target = normalize(root)
    ids: list[str] = []
    for workspace_id, info in (data.get("workspaces") or {}).items():
        if isinstance(info, dict) and normalize(info.get("root", "")) == target:
            ids.append(str(workspace_id))
    return ids


def resolve_wire_path(
    session_id: str | None,
    *,
    cwd: str | Path | None = None,
    home: str | Path | None = None,
) -> Path | None:
    """Locate the main-agent wire file for one Kimi session without scanning others."""
    if not session_id:
        return None
    home_path = Path(home) if home else Path.home()
    root = str(cwd if cwd is not None else Path.cwd())

    # 1. Deterministic workspace mapping (no session enumeration).
    for workspace_id in _workspace_id_for_root(root, home_path):
        candidate = home_path / ".kimi-code" / "sessions" / workspace_id / session_id / "agents" / "main" / "wire.jsonl"
        if candidate.is_file():
            return candidate

    # 2. Targeted session-index lookup.
    index_file = home_path / ".kimi-code" / "session_index.jsonl"
    try:
        with open(index_file, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("sessionId") == session_id and record.get("sessionDir"):
                    candidate = Path(record["sessionDir"]) / "agents" / "main" / "wire.jsonl"
                    if candidate.is_file():
                        return candidate
    except OSError:
        pass

    # 3. Last resort: match only this session id.
    sessions_root = home_path / ".kimi-code" / "sessions"
    try:
        for candidate in sessions_root.glob(f"*/{session_id}/agents/main/wire.jsonl"):
            if candidate.is_file():
                return candidate
    except OSError:
        pass
    return None


def wire_offset(
    session_id: str | None,
    *,
    cwd: str | Path | None = None,
    home: str | Path | None = None,
) -> int:
    """Byte offset to start reading from for the *next* turn of this session."""
    path = resolve_wire_path(session_id, cwd=cwd, home=home)
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _first_int(record: dict[str, Any], keys: tuple[str, ...]) -> tuple[int, bool]:
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value, True
    return 0, False


def aggregate_usage_records(text: str) -> dict[str, Any]:
    """Aggregate a wire slice. Missing fields stay unknown (``None``), never 0.

    Returns both the per-request list (``requests``) and the billing totals. The
    only legitimate "context length" reading is one request's
    ``inputOther + inputCacheRead + inputCacheCreation``; that is exposed as
    ``latestRequestInputContext`` / ``maxRequestInputContext`` and never as a sum
    across requests.
    """
    records = 0
    unknown_records = 0
    turn_prompts = 0
    models: set[str] = set()
    scopes: set[str] = set()
    totals = {"inputOther": 0, "output": 0, "inputCacheRead": 0, "inputCacheCreation": 0}
    present = {"inputOther": True, "output": True, "inputCacheRead": True, "inputCacheCreation": True}
    requests: list[dict[str, Any]] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.prompt":
            turn_prompts += 1
            continue
        if kind != "usage.record":
            continue
        raw = event.get("usage")
        if not isinstance(raw, dict):
            unknown_records += 1
            for key in present:
                present[key] = False
            continue
        records += 1
        if event.get("model"):
            models.add(str(event["model"]))
        if event.get("usageScope"):
            scopes.add(str(event["usageScope"]))

        per_request: dict[str, Any] = {"index": records, "usageScope": event.get("usageScope")}
        request_complete = True

        value, found = _first_int(raw, _INPUT_OTHER_KEYS)
        per_request["inputOther"] = value if found else None
        request_complete = request_complete and found
        if found:
            totals["inputOther"] += value
        else:
            present["inputOther"] = False
        value, found = _first_int(raw, _OUTPUT_KEYS)
        per_request["output"] = value if found else None
        if found:
            totals["output"] += value
        else:
            present["output"] = False
        value, found = _first_int(raw, _CACHE_READ_KEYS)
        per_request["inputCacheRead"] = value if found else None
        request_complete = request_complete and found
        if found:
            totals["inputCacheRead"] += value
        else:
            present["inputCacheRead"] = False
        value, found = _first_int(raw, _CACHE_CREATION_KEYS)
        per_request["inputCacheCreation"] = value if found else None
        request_complete = request_complete and found
        if found:
            totals["inputCacheCreation"] += value
        else:
            present["inputCacheCreation"] = False

        # Context length of this one request: the input side only, never output.
        per_request["inputContext"] = (
            per_request["inputOther"]
            + per_request["inputCacheRead"]
            + per_request["inputCacheCreation"]
            if request_complete
            else None
        )
        requests.append(per_request)

    unknown_fields = [key for key, ok in present.items() if not ok]
    all_known = not unknown_fields and unknown_records == 0
    total = (
        totals["inputOther"] + totals["output"] + totals["inputCacheRead"] + totals["inputCacheCreation"]
        if all_known
        else None
    )
    measured_contexts = [r["inputContext"] for r in requests if isinstance(r["inputContext"], int)]
    latest_input_context = (
        requests[-1]["inputContext"] if requests and isinstance(requests[-1]["inputContext"], int) else None
    )
    return {
        "input_tokens": totals["inputOther"] if present["inputOther"] else None,
        "output_tokens": totals["output"] if present["output"] else None,
        "cache_read_tokens": totals["inputCacheRead"] if present["inputCacheRead"] else None,
        "cache_creation_tokens": totals["inputCacheCreation"] if present["inputCacheCreation"] else None,
        # The wire schema has no prompt-total or thinking field: keep them unknown.
        "prompt_tokens": None,
        "thinking_tokens": None,
        "total_tokens": total,
        "inputOther": totals["inputOther"] if present["inputOther"] else None,
        "output": totals["output"] if present["output"] else None,
        "inputCacheRead": totals["inputCacheRead"] if present["inputCacheRead"] else None,
        "inputCacheCreation": totals["inputCacheCreation"] if present["inputCacheCreation"] else None,
        "generations_count": records,
        "turn_prompt_count": turn_prompts,
        "unknown_records": unknown_records,
        "unknown_fields": unknown_fields,
        "unknown": bool(unknown_fields) or unknown_records > 0,
        "models": sorted(models),
        "usage_scopes": sorted(scopes),
        # Per-request context sizes. ``latestRequestInputContext`` is the size of
        # the context the provider read for the newest request; summing these is
        # wrong (replayed/cached context) and is deliberately not offered.
        "requests": requests,
        "input_context_measured": bool(measured_contexts),
        "latestRequestInputContext": latest_input_context,
        "maxRequestInputContext": max(measured_contexts) if measured_contexts else None,
        "source": KIMI_WIRE_SOURCE,
    }


def read_usage_since(
    session_id: str | None,
    start_offset: int = 0,
    *,
    cwd: str | Path | None = None,
    home: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the usage produced after ``start_offset`` bytes for one session.

    Returns ``None`` when the wire (or any usage record) is unavailable, which
    callers must surface as "unknown", not zero.
    """
    path = resolve_wire_path(session_id, cwd=cwd, home=home)
    if path is None:
        return None
    try:
        size = path.stat().st_size
        offset = start_offset if 0 <= start_offset <= size else 0
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError as ex:
        logger.debug("Unable to read Kimi wire %s: %s", path, ex)
        return None

    text = data.decode("utf-8", errors="replace")
    aggregate = aggregate_usage_records(text)
    if aggregate["generations_count"] == 0 and aggregate["unknown_records"] == 0:
        return None
    aggregate["wirePath"] = str(path)
    aggregate["wireOffsetStart"] = offset
    aggregate["wireOffsetEnd"] = size
    return aggregate
