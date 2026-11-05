"""Local measurements of actual turn payloads, separate from provider token usage."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path


def record_context_usage(run_dir, task, provider, requested_session, result, pending, confirmed):
    if not run_dir:
        return
    row = {
        "at": datetime.now(UTC).isoformat(), "requestId": task.request_id, "saveId": task.save_id,
        "provider": provider, "requestedSession": requested_session,
        "returnedSession": result.get("actual_provider_session_id") or result.get("conversation_id"),
        "inputDelivered": confirmed, "error": result.get("error"),
        "contextMode": pending.get("contextMode"), "contextVersion": pending["version"],
        "guidanceRevision": pending.get("static", {}).get("rules"),
        "originalBytes": pending["originalPromptBytes"], "sentBytes": pending["newPromptBytes"],
        "durationSeconds": result.get("duration"), "usage": result.get("usage"),
    }
    try:
        path = Path(run_dir) / "data" / "context-usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        logging.getLogger(__name__).warning("Could not record context usage", exc_info=True)
