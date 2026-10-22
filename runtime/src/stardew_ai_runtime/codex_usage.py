"""Read usage only from the explicitly owned Codex thread, never other chats."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def normalize_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_output_tokens",
                "cache_write_input_tokens", "cache_read_input_tokens"):
        value = raw.get(key, raw.get("cached_input_tokens") if key == "cache_read_input_tokens" else None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result or None


class CodexUsageMeter:
    def __init__(self, session_id: str | None, *, home: Path | None = None):
        self.home = home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.started = datetime.now(UTC)
        self.session_id = session_id
        self.baseline = self._read(session_id) if session_id else None

    def _path(self, session_id: str | None) -> Path | None:
        try:
            identity = uuid.UUID(str(session_id))
            if identity.version != 7:
                return None
            created = datetime.fromtimestamp((identity.int >> 80) / 1000, UTC)
        except (ValueError, TypeError, OverflowError, OSError):
            return None
        # CLI filenames use local dates; check only the UUID's UTC/local dates.
        dates = {created.strftime("%Y/%m/%d"), created.astimezone().strftime("%Y/%m/%d")}
        paths = [p for date in dates for p in (self.home / "sessions" / date).glob(f"rollout-*-{identity}.jsonl")]
        return paths[0] if len(paths) == 1 else None

    def _read(self, session_id: str | None) -> dict[str, Any] | None:
        path = self._path(session_id)
        if path is None:
            return None
        latest = None
        latest_request_input = None
        requests = 0
        created = None
        try:
            with path.open(encoding="utf-8") as stream:
                first = json.loads(next(stream))
                meta = first.get("payload", {})
                if first.get("type") != "session_meta" or meta.get("id") != session_id:
                    return None
                created = datetime.fromisoformat(meta["timestamp"].replace("Z", "+00:00"))
                for line in stream:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # An interrupted final write is not usage evidence.
                    payload = event.get("payload", {})
                    if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info") or {}
                    last_usage = info.get("last_token_usage")
                    request_input = last_usage.get("input_tokens") if isinstance(last_usage, dict) else None
                    latest_request_input = (
                        request_input if isinstance(request_input, int)
                        and not isinstance(request_input, bool) and request_input >= 0 else None
                    )
                    usage = normalize_usage(info.get("total_token_usage"))
                    if usage and usage != latest:
                        requests += 1
                        latest = usage
        except (OSError, ValueError, KeyError, StopIteration):
            return None
        return {"usage": latest, "requests": requests, "created": created, "path": str(path),
                "latest_request_input": latest_request_input}

    def finish(self, session_id: str | None, cli_usage: Any = None) -> dict[str, Any]:
        current = self._read(session_id)
        baseline = self.baseline
        # Context is one request's input, independent of cumulative billing.
        context_metadata = {}
        if current and (
            session_id == self.session_id
            or (self.session_id is None and current["created"] >= self.started)
        ) and current["latest_request_input"] is not None:
            context_metadata["usage_latest_request_input_context"] = current["latest_request_input"]
        if not self.session_id and current and current["created"] >= self.started:
            baseline = {"usage": {}, "requests": 0}
        if current and baseline and (self.session_id is None or session_id == self.session_id):
            before = baseline["usage"] or {}
            after = current["usage"]
            if after and all(after.get(key, 0) >= value for key, value in before.items()):
                usage = {key: value - before.get(key, 0) for key, value in after.items()}
                return {**context_metadata, "usage": usage, "usage_source": "codex_rollout_cumulative_delta",
                        "usage_status": "recorded", "usage_request_count": current["requests"] - baseline["requests"],
                        "usage_limits": "Only recorded requests; interrupted unrecorded usage remains unknown."}
        # A fresh CLI thread has no inherited cumulative usage. Resume does.
        if self.session_id is None and session_id and normalize_usage(cli_usage):
            return {**context_metadata, "usage": normalize_usage(cli_usage), "usage_source": "codex_cli_new_thread",
                    "usage_status": "recorded", "usage_limits": "CLI completion only; request count unavailable."}
        return {**context_metadata, "usage": None, "usage_source": "unavailable", "usage_status": "unknown",
                "usage_limits": "Owned rollout or pre-turn cumulative baseline unavailable."}
