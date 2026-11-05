"""One model-facing context composer for conversation, work and game events.

This module formats facts; it neither chooses work nor owns a provider session.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .decision_context import model_decision_context, render_decision_context


def compose_turn_prompt(prompt: str, context: dict[str, Any], *, event: dict[str, Any],
                        external: bool = False, original_request: dict | None = None) -> str:
    """Replace all legacy context blocks with one current state and one event.

    Player text is opaque. Event metadata describes this input, never the world.
    Original instructions live in the diffable state, not a repeated prose suffix.
    """
    marker = re.search(r"(?m)^(?:玩家说：|玩家指令：)", prompt)
    boundary = marker.start() if marker else len(prompt)
    prefix, player = prompt[:boundary], prompt[boundary:]
    metadata = {}
    for match in re.finditer(r"(?m)^实时上下文：([^\n]*)", prefix):
        original = json.loads(match.group(1))
        for key in ("projectScope", "objective", "wakeReason", "acceptedArrangement",
                    "boxRange", "idlePreference", "idleHelpEnabled", "autonomyMode"):
            if key in original:
                metadata[key] = original[key]
    metadata.update(event)
    state = model_decision_context(context)
    if original_request:
        state["playerRequest"] = original_request
    summary = state.get("reloadSummary")
    if isinstance(summary, dict):
        state["reloadSummary"] = {k: v for k, v in summary.items()
                                  if k not in {"goals", "agreementsAndPreferences", "decisions"}}
    # Multiple old UI adapters may already have supplied a state block. Keep
    # their non-state notice text but never repeat the canonical snapshot.
    prefix = re.sub(r"(?m)^实时上下文：[^\n]*\n?", "", prefix).strip()
    if external:
        prefix = "沿用原对话的身份、角色指令、称呼和记忆；以下只增加当前游戏场景与操作规则。\n" + prefix
    return (prefix + "\n实时上下文：" + render_decision_context(state)
            + "\n本轮事件：" + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            + ("\n\n" + player if player else ""))


def compact_action_result(result: Any, *, detail: bool = False) -> Any:
    """Omit only exact duplicates from a receipt; never infer output or success.

    Native wrappers lift useful details to the top level. Unknown fields,
    partial results, errors, remaining quantities and afterWork observations
    stay intact. Explicit detail returns the original receipt.
    """
    if detail or not isinstance(result, dict) or not isinstance(result.get("details"), dict):
        return result
    details = {key: value for key, value in result["details"].items()
               if key not in result or result[key] != value}
    return {**{key: value for key, value in result.items() if key != "details"},
            **({"details": details} if details else {})}
