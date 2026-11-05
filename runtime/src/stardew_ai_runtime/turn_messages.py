"""Deliver player messages at tool boundaries without starting a second decision maker."""
from __future__ import annotations

import json
from pathlib import Path

from .snapshot_cache import cache_lock, replace_cache_file


def exchange(path: Path, turn_id: str, save_id: str, *, publish=None, take=False,
             ack_world=False, pending_world=False, acknowledge_ids=()):
    with cache_lock(path):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        original = json.dumps(state, ensure_ascii=False)
        if (state.get("turnId"), state.get("saveId")) != (turn_id, save_id):
            state = {"turnId": turn_id, "saveId": save_id, "pending": [], "delivered": []}
        offered = state.setdefault("offeredWorld", [])
        if acknowledge_ids:
            for bucket in ("pending", "offeredWorld"):
                state["delivered"].extend(m for m in state[bucket] if m["requestId"] in acknowledge_ids)
                state[bucket] = [m for m in state[bucket] if m["requestId"] not in acknowledge_ids]
            offered = state["offeredWorld"]
        # A subsequent tool call (or a successful provider completion) confirms
        # receipt. Merely writing a tool result does not prove it reached the model.
        if ack_world:
            state["delivered"].extend(offered)
            state["offeredWorld"] = offered = []
        if publish is not None:
            known = {m["requestId"] for m in state["pending"] + state["delivered"] + offered}
            state["pending"].extend(m for m in publish if m["requestId"] not in known)
        messages = list(state["pending"] if take else state["delivered"])
        if take:
            state["delivered"].extend(m for m in messages if m.get("kind") != "world_changed")
            state["offeredWorld"].extend(m for m in messages if m.get("kind") == "world_changed")
            state["pending"] = []
        if pending_world:
            messages = [m for m in state["pending"] + state["offeredWorld"] if m.get("kind") == "world_changed"]
        updated = json.dumps(state, ensure_ascii=False)
        if updated != original:
            temp = path.with_suffix(".tmp")
            temp.write_text(updated, encoding="utf-8")
            replace_cache_file(temp, path)
        return messages


def day_event(day: str, payload: dict) -> dict:
    """Native facts only; the model chooses what to do with the changed day."""
    return {"requestId": "world-day-" + day, "kind": "world_changed", "day": day,
            "text": "游戏已进入新一天。按当前状态重新判断原目标的后续安排；不要重放昨天的动作。",
            "world": payload.get("world") or {}, "companion": payload.get("companion") or {},
            "nextStep": "Observe current crops, inventory and native action before choosing remaining work."}
