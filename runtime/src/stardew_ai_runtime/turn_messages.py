"""Deliver player messages at tool boundaries without starting a second decision maker."""
from __future__ import annotations

import json
from pathlib import Path

from .snapshot_cache import cache_lock


def exchange(path: Path, turn_id: str, save_id: str, *, publish=None, take=False):
    with cache_lock(path):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if (state.get("turnId"), state.get("saveId")) != (turn_id, save_id):
            state = {"turnId": turn_id, "saveId": save_id, "pending": [], "delivered": []}
        if publish is not None:
            known = {m["requestId"] for m in state["pending"] + state["delivered"]}
            state["pending"].extend(m for m in publish if m["requestId"] not in known)
        messages = state["pending"] if take else state["delivered"]
        if take:
            state["delivered"] = state["delivered"] + messages
            state["pending"] = []
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        return messages
