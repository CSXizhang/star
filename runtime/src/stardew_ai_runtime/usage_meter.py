"""Durable provider response receipts, independent of job handoff/turn completion."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def dsh_response_usage(raw: dict[str, Any]) -> dict[str, Any] | None:
    def count(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    uncached, cache, output = count("inputTokens"), count("cacheReadTokens"), count("outputTokens")
    if all(value is None for value in (uncached, cache, output, count("totalTokens"))):
        return None
    usage: dict[str, Any] = {"source": "dsh_assistant_message", "input_includes_cache": True}
    for key, value in (("uncached_input_tokens", uncached), ("cache_read_tokens", cache),
                       ("output_tokens", output), ("total_tokens", count("totalTokens")),
                       ("thinking_tokens", count("reasoningTokens"))):
        if value is not None:
            usage[key] = value
    if uncached is not None and cache is not None:
        usage["input_tokens"] = uncached + cache
        usage["latestRequestInputContext"] = uncached + cache
        if "total_tokens" not in usage and output is not None:
            usage["total_tokens"] = uncached + cache + output
    if any(field not in usage for field in ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens")):
        usage["partial"] = True
    return usage


def append_response_receipt(run_dir: Path, *, response_id: str, request_id: str,
                            save_id: str, conversation_id: str, model: str,
                            usage: dict[str, Any], channel: str, timestamp: str | None = None) -> None:
    if not request_id or not save_id:
        return
    row = {"timestamp": timestamp or datetime.now(UTC).isoformat(), "provider": "dsh", "model": model,
           "requestId": request_id, "saveId": save_id, "conversationId": conversation_id,
           "responseId": response_id, "channel": channel, "usage": usage}
    path = run_dir / "data" / "model-usage.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
    except OSError:
        logger.warning("Could not persist provider usage receipt")


def recover_dsh_receipts(run_dir: Path, commands: Path) -> int:
    """Recover only own recorded turns with an unambiguous session/time match.

    Original journals and provider session files remain untouched. Response IDs
    make repeated startup scans idempotent; unmeasured responses stay unknown.
    """
    try:
        rows = [json.loads(line) for line in commands.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return 0
    by_session: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        cid = row.get("conversationId")
        if row.get("provider") == "dsh" and row.get("source") != "test" and isinstance(cid, str) and cid.startswith("stardew-"):
            try:
                end = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
                start = end - float(row.get("durationSeconds") or 0)
            except (TypeError, ValueError, KeyError):
                continue
            by_session.setdefault(cid, []).append({**row, "start": start, "end": end})
    existing: set[str] = set()
    receipt_path = run_dir / "data" / "model-usage.jsonl"
    if receipt_path.is_file():
        for line in receipt_path.read_text(encoding="utf-8").splitlines():
            try:
                existing.add(json.loads(line).get("responseId", ""))
            except ValueError:
                continue
    recovered = 0
    for cid, turns in by_session.items():
        # Session IDs come only from our journal, never arbitrary account data.
        if not all(c.isalnum() or c in "-_" for c in cid):
            continue
        for path in (run_dir / "data" / "dsh-home" / "sessions").glob(f"*/{cid}/session.v3.jsonl"):
            current = None
            try:
                for line in path.open(encoding="utf-8"):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    data = event.get("data") or {}
                    when = event.get("time")
                    if not isinstance(when, (int, float)):
                        continue
                    if event.get("type") == "turn/start":
                        # A terminal is always at/after its own turn start. A
                        # small tolerance covers process-start timestamp jitter.
                        candidates = [t for t in turns if t["start"] - 3 <= when / 1000 <= t["end"] + 1]
                        current = candidates[0] if len(candidates) == 1 else None
                    elif current and event.get("type") == "assistant/message":
                        measured = dsh_response_usage(data.get("usage") or {})
                        identity = (data.get("message") or {}).get("id") or event.get("seq")
                        if measured is None or identity is None:
                            continue
                        response_id = f"{cid}:{identity}"
                        if response_id in existing:
                            continue
                        append_response_receipt(run_dir, response_id=response_id,
                            request_id=current["requestId"], save_id=current.get("saveId", ""),
                            conversation_id=cid, model=current.get("model", ""), usage=measured,
                            channel="autonomy" if current["requestId"].startswith("autonomy-") else "conversation",
                            timestamp=datetime.fromtimestamp(when / 1000, UTC).isoformat())
                        existing.add(response_id)
                        recovered += 1
            except OSError:
                continue
    return recovered
