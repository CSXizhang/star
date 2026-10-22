"""Durable provider response receipts, independent of job handoff/turn completion."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def normalize_usage(provider: str, raw: Any) -> dict[str, Any] | None:
    """Return inclusive input counters without changing an older raw receipt.

    agy DB/CLI counters, Kimi wire counters and legacy DSH counters record
    uncached input separately. New inclusive receipts explicitly opt out of
    adding cache again. Missing components remain a labelled lower bound.
    """
    if not isinstance(raw, dict) or not raw:
        return None

    def count(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    result = dict(raw)
    inp = count(raw.get("input_tokens"))
    out = count(raw.get("output_tokens"))
    cache = count(raw.get("cache_read_tokens", raw.get("cache_read_input_tokens", raw.get("cached_input_tokens"))))
    write = count(raw.get("cache_creation_tokens", raw.get("cache_write_input_tokens")))
    separate = raw.get("input_includes_cache") is False or (
        raw.get("input_includes_cache") is not True and provider in {"agy", "kimi", "dsh"}
    )
    if separate:
        parts = [inp, cache]
        if provider == "kimi" or "cache_creation_tokens" in raw or "cache_write_input_tokens" in raw:
            parts.append(write)
        measured = [part for part in parts if part is not None]
        result["raw_input_tokens"] = raw.get("raw_input_tokens", raw.get("input_tokens"))
        result["raw_total_tokens"] = raw.get("raw_total_tokens", raw.get("total_tokens"))
        result["uncached_input_tokens"] = inp
        result["input_tokens"] = sum(measured) if measured else None
        result["input_includes_cache"] = True
        missing = any(part is None for part in parts)
        if missing and provider == "kimi":
            # Preserve the older Kimi display contract: an incomplete three-part
            # input is unknown, while a measured output remains independently known.
            result["input_tokens"] = None
            result["total_tokens"] = None
        else:
            if missing:
                result["partial"] = True
            if out is not None and result["input_tokens"] is not None:
                result["total_tokens"] = result["input_tokens"] + out
            elif out is not None:
                result["total_tokens"] = out
                result["partial"] = True
            elif result["input_tokens"] is not None:
                result["total_tokens"] = result["input_tokens"]
                result["partial"] = True
            else:
                result["total_tokens"] = None
    elif count(raw.get("total_tokens")) is None and inp is not None and out is not None:
        result["total_tokens"] = inp + out
    if cache is not None:
        result["cache_read_tokens"] = cache
    result["normalized_usage_version"] = 1
    return result


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
                            usage: dict[str, Any] | None, channel: str, timestamp: str | None = None,
                            provider: str = "dsh", **extra: Any) -> bool:
    if not request_id or not save_id:
        return False
    row = {**extra, "timestamp": timestamp or datetime.now(UTC).isoformat(), "provider": provider, "model": model,
           "requestId": request_id, "saveId": save_id, "conversationId": conversation_id,
           "responseId": response_id, "channel": channel, "usage": usage}
    path = run_dir / "data" / "model-usage.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
        return True
    except OSError:
        logger.warning("Could not persist provider usage receipt")
        return False


def _agy_fields(data: bytes) -> dict[int, list[Any]]:
    """Decode only scalar and length-delimited metadata, with bounded varints."""
    pos = 0
    fields: dict[int, list[Any]] = {}

    def varint() -> int:
        nonlocal pos
        result = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError("truncated protobuf")
            value = data[pos]
            pos += 1
            result |= (value & 127) << shift
            if not value & 128:
                return result
        raise ValueError("oversized protobuf varint")

    try:
        while pos < len(data):
            tag = varint()
            if not tag >> 3:
                raise ValueError("invalid protobuf tag")
            wire = tag & 7
            if wire == 0:
                value = varint()
            elif wire in {1, 2, 5}:
                size = varint() if wire == 2 else 8 if wire == 1 else 4
                if pos + size > len(data):
                    raise ValueError("truncated protobuf field")
                value = data[pos:pos + size]
                pos += size
            else:
                raise ValueError("unsupported protobuf field")
            fields.setdefault(tag >> 3, []).append(value)
    except (ValueError, TypeError):
        return {}
    return fields


def _agy_first(fields: dict[int, list[Any]], tag: int, default: Any = None) -> Any:
    values = fields.get(tag)
    return values[0] if values else default


def _agy_usage_fields(data: bytes) -> dict[int, list[Any]]:
    meta = _agy_first(_agy_fields(data), 1)
    if not isinstance(meta, bytes):
        return {}
    usage = _agy_first(_agy_fields(meta), 4)
    return _agy_fields(usage) if isinstance(usage, bytes) else {}


def _agy_cid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        return None


def read_agy_generations(conversation_id: str, start_idx: int = -1,
                         end_idx: int | None = None, *, conversations_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read just an explicitly associated conversation and generation interval.

    Never searches account-wide sessions. Raw counters remain available alongside
    normalized counters; generation completion timestamps come from matching
    assistant step metadata, not from recovery time.
    """
    cid = _agy_cid(conversation_id)
    if cid is None:
        return []
    folder = conversations_dir or Path.home() / ".gemini" / "antigravity-cli" / "conversations"
    path = folder / f"{cid}.db"
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.1) as con:
            rows = con.execute("SELECT idx, data FROM gen_metadata WHERE idx > ? AND (? IS NULL OR idx <= ?) ORDER BY idx",
                               (start_idx, end_idx, end_idx)).fetchall()
            timestamps: dict[str, str] = {}
            try:
                for (metadata,) in con.execute("SELECT metadata FROM steps WHERE step_type=15"):
                    fields = _agy_fields(metadata or b"")
                    usage_blob = _agy_first(fields, 9)
                    usage_fields = _agy_fields(usage_blob) if isinstance(usage_blob, bytes) else {}
                    response = _agy_first(usage_fields, 11)
                    stamp = _agy_first(fields, 7)
                    parts = _agy_fields(stamp) if isinstance(stamp, bytes) else {}
                    seconds = _agy_first(parts, 1)
                    nanos = _agy_first(parts, 2, 0)
                    if isinstance(response, bytes) and isinstance(seconds, int) and isinstance(nanos, int):
                        timestamps[response.decode("utf-8")] = datetime.fromtimestamp(seconds + nanos / 1_000_000_000, UTC).isoformat()
            except (sqlite3.Error, ValueError, OverflowError, UnicodeError):
                pass
        records = []
        for idx, data in rows:
            fields = _agy_usage_fields(data or b"")
            if not fields or not any(isinstance(_agy_first(fields, tag), int) for tag in (2, 3, 5)):
                continue
            inp, out = _agy_first(fields, 2), _agy_first(fields, 3)
            cache, thinking = _agy_first(fields, 5, 0), _agy_first(fields, 9, 0)
            if any(value is not None and not isinstance(value, int) for value in (inp, out)) or not isinstance(cache, int) or not isinstance(thinking, int):
                continue
            response = _agy_first(fields, 11)
            response_id = response.decode("utf-8") if isinstance(response, bytes) else f"{cid}:gen:{idx}"
            context = inp + cache if inp is not None else None
            raw = {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": cache,
                   "thinking_tokens": thinking, "total_tokens": sum(value for value in (inp, out) if value is not None),
                   "input_includes_cache": False, "source": "db_gen_metadata_delta",
                   "latestRequestInputContext": context, "input_context_measured": context is not None,
                   "generations_count": 1, "start_idx": idx, "end_idx": idx}
            if inp is None or out is None:
                raw["partial"] = True
            records.append({"idx": idx, "response_id": response_id, "raw_usage": raw,
                            "usage": normalize_usage("agy", raw), "timestamp": timestamps.get(response_id)})
        return records
    except (OSError, sqlite3.Error, UnicodeError):
        return []


class AgyTurnReceipts:
    """Durable start/CID bindings and response receipts for one owned agy turn."""

    def __init__(self, run_dir: Path, *, request_id: str, save_id: str, model: str,
                 conversation_id: str | None = None, start_idx: int = -1,
                 prompt: str = "", conversations_dir: Path | None = None,
                 binding: dict[str, Any] | None = None):
        self.run_dir = Path(run_dir)
        self.conversations_dir = conversations_dir
        self.state = binding or {
            "turnKey": uuid.uuid4().hex, "requestId": request_id, "saveId": save_id,
            "model": model, "conversationId": _agy_cid(conversation_id),
            "startGenIdx": start_idx, "startedAt": datetime.now(UTC).isoformat(),
            "channel": "autonomy" if request_id.startswith("autonomy-") else "conversation",
            "promptHash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "status": "started",
        }
        self.state.setdefault("logFile", f"data/agy-process/{self.state['turnKey']}.log")
        self.log_file = self.run_dir / self.state["logFile"]
        self._seen_measured: set[str] = set()
        self._seen_unknown: set[str] = set()
        self._seen_usage: dict[str, dict[str, Any]] = {}
        self._write_failed = False
        self._load_seen()

    def _load_seen(self) -> None:
        path = self.run_dir / "data" / "model-usage.jsonl"
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if (row.get("provider"), row.get("saveId"), row.get("requestId")) == (
                        "agy", self.state["saveId"], self.state["requestId"]
                    ):
                        target = self._seen_measured if row.get("usage") else self._seen_unknown
                        response_id = str(row.get("responseId", ""))
                        target.add(response_id)
                        if row.get("usage"):
                            self._seen_usage[response_id] = {"usage": row["usage"], "timestamp": row.get("timestamp"),
                                                             "dateUnallocated": bool(row.get("dateUnallocated"))}
        except OSError:
            pass

    def _persist(self) -> None:
        path = self.run_dir / "data" / "agy-turns.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self.state, ensure_ascii=False) + "\n")
                stream.flush()
        except OSError:
            self._write_failed = True
            logger.warning("Could not persist agy turn association")

    def begin(self) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._persist()

    def bind(self, conversation_id: str) -> None:
        cid = _agy_cid(conversation_id)
        if cid is None:
            return
        if self.state.get("conversationId") != cid:
            self.state["startGenIdx"] = -1
        self.state.update(conversationId=cid, status="running")
        self._persist()

    def collect(self) -> dict[str, Any] | None:
        cid = self.state.get("conversationId")
        if not cid:
            return None
        records = read_agy_generations(cid, self.state["startGenIdx"], self.state.get("endGenIdx"),
                                       conversations_dir=self.conversations_dir)
        for record in records:
            response_id = record["response_id"]
            measured = {"usage": {**record["usage"], "partial": bool(record["usage"].get("partial"))},
                        "timestamp": record["timestamp"] or self.state["startedAt"],
                        "dateUnallocated": record["timestamp"] is None}
            if self._seen_usage.get(response_id) == measured:
                continue
            ok = append_response_receipt(self.run_dir, provider="agy", response_id=response_id,
                request_id=self.state["requestId"], save_id=self.state["saveId"],
                conversation_id=cid, model=self.state["model"], usage=measured["usage"],
                channel=self.state["channel"], timestamp=measured["timestamp"],
                dateUnallocated=measured["dateUnallocated"])
            if ok:
                self._seen_measured.add(response_id)
                self._seen_usage[response_id] = measured
            else:
                self._write_failed = True
        remote_ids: set[str] = set()
        try:
            # This path was recorded before spawn and belongs to this turn.
            for line in self.log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if ":streamGenerateContent?" in line and (match := re.search(r"ResponseID: (\S+)", line)):
                    remote_ids.add(match[1])
        except OSError:
            pass
        unknown = remote_ids - {record["response_id"] for record in records}
        for response_id in unknown - self._seen_unknown - self._seen_measured:
            ok = append_response_receipt(self.run_dir, provider="agy", response_id=response_id,
                request_id=self.state["requestId"], save_id=self.state["saveId"],
                conversation_id=cid, model="", usage=None, channel=self.state["channel"],
                timestamp=self.state["startedAt"], missingReason="agy_response_usage_unavailable")
            if ok:
                self._seen_unknown.add(response_id)
            else:
                self._write_failed = True
        if not records:
            return None
        usage = {"source": "durable_agy_db_responses", "input_includes_cache": True,
                 "generations_count": len(records), "unknown_response_count": len(unknown),
                 "partial": bool(unknown) or any(record["usage"].get("partial") for record in records)
                 or self.state.get("status") not in {"completed", "failed", "cancelled", "timeout", "recovered"}}
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens", "total_tokens"):
            values = [record["usage"].get(field) for record in records]
            if any(isinstance(value, int) for value in values):
                usage[field] = sum(value for value in values if isinstance(value, int))
        usage["latestRequestInputContext"] = records[-1]["usage"]["latestRequestInputContext"]
        usage["input_context_measured"] = records[-1]["usage"]["input_context_measured"]
        return usage

    def finish(self, status: str) -> None:
        # Persist the inclusive upper boundary so later resumptions cannot be
        # reassigned to an earlier interrupted turn.
        boundary = _agy_generation_boundary(self.state.get("conversationId") or "", self.state["startGenIdx"],
                                             self.state.get("endGenIdx"), conversations_dir=self.conversations_dir)
        if boundary is not None:
            self.state["endGenIdx"] = boundary[0]
        records = read_agy_generations(self.state.get("conversationId") or "", self.state["startGenIdx"],
                                       self.state.get("endGenIdx"), conversations_dir=self.conversations_dir)
        # A process can close while the DB is briefly locked or a receipt is
        # incomplete. A closed turn still needs a bounded startup retry then.
        self.state["meteringPending"] = bool(self.state.get("conversationId")) and (
            boundary is None or len(records) != boundary[1]
            or any(record["usage"].get("partial") or record["timestamp"] is None for record in records))
        self.state["status"] = status
        self.state.setdefault("finishedAt", datetime.now(UTC).isoformat())
        self.collect()
        if self._write_failed:
            self.state["status"] = "receipt_write_failed"
        self._persist()


def _agy_generation_boundary(conversation_id: str, start_idx: int, end_idx: int | None = None,
                             *, conversations_dir: Path | None = None) -> tuple[int, int] | None:
    """Distinguish an empty owned interval from an unavailable DB without guessing."""
    cid = _agy_cid(conversation_id)
    if cid is None:
        return None
    folder = conversations_dir or Path.home() / ".gemini" / "antigravity-cli" / "conversations"
    path = folder / f"{cid}.db"
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.1) as con:
            maximum, count = con.execute("SELECT MAX(idx), COUNT(*) FROM gen_metadata WHERE idx > ? AND (? IS NULL OR idx <= ?)",
                                         (start_idx, end_idx, end_idx)).fetchone()
        return (maximum if maximum is not None else start_idx, count)
    except (OSError, sqlite3.Error):
        return None


def recover_agy_receipts(run_dir: Path, *, conversations_dir: Path | None = None) -> int:
    """Recover only persisted CID bindings; unknown/unbound sessions stay unknown."""
    root = Path(run_dir)
    states: dict[str, dict[str, Any]] = {}
    try:
        with (root / "data" / "agy-turns.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and isinstance(row.get("turnKey"), str) and row["turnKey"]:
                        states[row["turnKey"]] = row
                except ValueError:
                    continue
    except OSError:
        return 0
    recovered = 0
    ordered = sorted((row for row in states.values() if isinstance(row.get("startedAt"), str)),
                     key=lambda row: row["startedAt"])
    for index, row in enumerate(ordered):
        closed = row.get("status") in {"completed", "failed", "cancelled", "timeout", "recovered"}
        if closed and not row.get("meteringPending") or not _agy_cid(row.get("conversationId")):
            continue
        try:
            log = (root / row["logFile"]).resolve()
            if not log.is_relative_to((root / "data" / "agy-process").resolve()):
                continue
            if not isinstance(row["startGenIdx"], int) or isinstance(row["startGenIdx"], bool):
                continue
            next_turn = next((later for later in ordered[index + 1:]
                              if later.get("conversationId") == row["conversationId"]), None)
            if next_turn and isinstance(next_turn.get("startGenIdx"), int) and not isinstance(next_turn["startGenIdx"], bool):
                row["endGenIdx"] = min(row.get("endGenIdx", next_turn["startGenIdx"]), next_turn["startGenIdx"])
            tracker = AgyTurnReceipts(root, request_id=row["requestId"], save_id=row["saveId"],
                                      model=row["model"], binding=dict(row), conversations_dir=conversations_dir)
            before = len(tracker._seen_measured)
            tracker.finish(row["status"] if closed else "recovered")
            recovered += len(tracker._seen_measured) - before
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return recovered


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
