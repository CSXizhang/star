"""Read-only agy usage and quota evidence; never creates or resumes sessions."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("stardew_ai_runtime.chat_bridge")

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
