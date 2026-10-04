"""Shared, bounded facts for the next decision, player replies and memory."""

import json
from collections import Counter
from typing import Any

from stardew_ai_runtime.work_state import WorkStore

_PROGRESS_KEYS = (
    "targetCount", "completedCount", "skippedCount", "failedCount",
    "unprocessedTargetCount", "remaining", "remainingCountSource",
    "worldRevision", "snapshotRevision", "finalWorldRevision", "resources",
    "targetCountSource", "unprocessedTargetCountSource",
    "candidateCount", "nativeCountsScope",
    # This is the operation's observed scope, not completion of its milestone.
    "goalSatisfied", "knownNoWork", "scopeObservationIncomplete",
)


def _known_no_work(raw: dict[str, Any], status: str | None) -> bool:
    """An explicitly complete empty observation is different from no evidence."""
    remaining = raw.get("remaining")
    return bool(
        raw.get("status") == "no-work" and raw.get("outcome") == "completed"
        and status in {None, "completed", "no-work"}
        and raw.get("goalSatisfied") is True and raw.get("terminalState") == "none"
        and type(raw.get("targetCount")) is int and raw["targetCount"] == 0
        and isinstance(remaining, dict) and remaining
        and all(type(value) is int and value == 0 for value in remaining.values())
        and raw.get("effects") == [] and not raw.get("error")
        and not raw.get("scopeObservationIncomplete") and not raw.get("isTruncated")
        and all(key not in raw or (type(raw[key]) is int and raw[key] == 0)
                for key in ("completedCount", "skippedCount", "failedCount", "unprocessedTargetCount"))
    )


def _reason_counts(raw: dict[str, Any], kind: str) -> dict[str, int]:
    details = raw.get("details")
    rows = details.get(f"{kind}Tiles") if isinstance(details, dict) else None
    if not isinstance(rows, list):
        rows = [effect for effect in (raw.get("effects") or [])
                if isinstance(effect, dict) and effect.get("state") == kind]
    counts = Counter(str(row.get("reason") or "unspecified")[:160]
                     for row in rows if isinstance(row, dict))
    return dict(counts.most_common(8))


def feedback_summary(feedback: dict[str, Any]) -> str:
    """Describe recorded effects, never turn a requested title into completion."""
    parts = [str(feedback.get("actualSummary") or "未确认实际变化")]
    candidate_attempts = feedback.get("nativeCountsScope") == "waterSourceCandidates"
    for field, label, reasons_field in (("skippedCount", "跳过水源候选点" if candidate_attempts else "跳过", "skipReasons"),
                                        ("failedCount", "失败的水源候选点" if candidate_attempts else "失败", "failureReasons")):
        count = feedback.get(field)
        if isinstance(count, int) and count > 0:
            reasons = feedback.get(reasons_field) or {}
            detail = "、".join(f"{reason} {n}" for reason, n in reasons.items())
            parts.append(f"{label} {count} 项" + (f"（{detail}）" if detail else ""))
    remaining = feedback.get("unprocessedTargetCount")
    if isinstance(remaining, int) and remaining > 0:
        parts.append(f"还有 {remaining} 个目标未处理")
    if feedback.get("reason"):
        parts.append(str(feedback["reason"]))
    return "；".join(parts)


def compact_job_feedback(result: Any, *, operation: str = "", status: str | None = None,
                         params: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = result if isinstance(result, dict) else {}
    if isinstance(raw.get("native"), dict):
        raw = {**raw["native"], **{k: v for k, v in raw.items() if k != "native"}}
    native_error = raw.get("error")
    if isinstance(native_error, dict):
        raw = {**raw, "reasonCode": raw.get("reasonCode") or native_error.get("code"),
               "message": raw.get("message") or native_error.get("message")}
    raw = {**raw, "knownNoWork": _known_no_work(raw, status)}
    # Reconciled native receipts can omit targetCount. Only explicit unique
    # coordinates establish an exact count; rectangles and max_tiles do not.
    tiles = (params or {}).get("tiles", (params or {}).get("target_tiles"))
    if operation == "refill_watering_can":
        # Water-source coordinates are alternatives for one refill service.
        # Unattempted alternatives are never unfinished farm work.
        raw = {**raw, "nativeCountsScope": "waterSourceCandidates"}
        if raw.get("targetCountSource") != "singleRefillService":
            raw.pop("targetCount", None)
            raw.pop("unprocessedTargetCount", None)
        if raw.get("candidateCount") is None and isinstance(tiles, list):
            raw["candidateCount"] = len(tiles)
        if (raw.get("terminalState") == "succeeded" and
                isinstance(raw.get("completedCount"), int) and raw["completedCount"] > 0):
            raw.update(targetCount=1, targetCountSource="singleRefillService",
                       unprocessedTargetCount=0, unprocessedTargetCountSource="nativeSuccessfulRefill")
    if operation not in {"refill_watering_can", "water_auto", "harvest_auto"} and raw.get("targetCount") is None and isinstance(tiles, list) and tiles and all(
        isinstance(tile, dict) and all(isinstance(tile.get(key), int) and
                                      not isinstance(tile[key], bool) for key in ("x", "y"))
        for tile in tiles
    ):
        raw = {**raw, "targetCount": len({(tile["x"], tile["y"]) for tile in tiles}),
               "targetCountSource": "explicitStepTiles"}
    counters = [raw.get(key) for key in ("targetCount", "completedCount", "skippedCount", "failedCount")]
    if (raw.get("unprocessedTargetCount") is None and
            (status or raw.get("status") or raw.get("outcome")) in {"completed", "partial", "failed", "cancelled", "rejected"} and
            raw.get("terminalState") not in {"running", "unknown"} and
            all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counters)
            and counters[0] >= sum(counters[1:])):
        raw = {**raw, "unprocessedTargetCount": counters[0] - sum(counters[1:]),
               "unprocessedTargetCountSource": "nativeCounters"}
    feedback = {"operation": operation, "status": status or raw.get("status", raw.get("outcome", "unknown")),
                "nextBusiness": "new_model_decision_required"}
    effects = [effect for effect in (raw.get("effects") or []) if isinstance(effect, dict)]
    actual_summary = "所选范围当前无待处理工作" if raw["knownNoWork"] else WorkStore.effect_summary(effects)
    for key in ("commandId", "reasonCode", "message", "error", "terminalState", "effects", "inventoryDelta", "resourceDelta", "tile", "locationId", *_PROGRESS_KEYS):
        value = raw.get(key)
        if value is not None:
            feedback[key] = value[:8] if isinstance(value, list) else value[:320] if isinstance(value, str) else value
    for kind, field in (("skipped", "skipReasons"), ("failed", "failureReasons")):
        reasons = _reason_counts(raw, kind)
        if reasons:
            feedback[field] = reasons
    if len(effects) > 8:
        feedback["effectCount"] = len(effects)
        feedback["effectsTruncated"] = True
    if len(json.dumps(feedback, ensure_ascii=False)) > 2400:
        essential = {key: feedback[key] for key in (*_PROGRESS_KEYS, "skipReasons", "failureReasons", "commandId") if key in feedback}
        feedback = {"operation": operation, "status": feedback["status"], "nextBusiness": "new_model_decision_required", "reasonCode": raw.get("reasonCode"), "effectCount": len(effects), "detailsTruncated": True, **essential}
    feedback["actualSummary"] = actual_summary
    if raw.get("reasonCode") and raw["reasonCode"] != "OK" and not raw["knownNoWork"]:
        feedback["reason"] = WorkStore.reason_summary(raw["reasonCode"], raw.get("message"))
    feedback["progressSummary"] = feedback_summary(feedback)
    return feedback


def compact_task_feedback(task: Any, result: Any, *, operation: str = "", status: str | None = None) -> dict[str, Any]:
    """Keep each native receipt's counts when a composite task settles."""
    feedback = compact_job_feedback(result, operation=operation, status=status)
    if task is None:
        return feedback
    effects = [effect for step in task.steps for effect in step.effects]
    feedback["knownNoWork"] = bool(feedback["knownNoWork"] and not effects
                                   and all(step.status == "completed" for step in task.steps))
    if effects:
        feedback["effects"] = effects[:8]
        feedback["effectCount"] = len(effects)
        feedback["actualSummary"] = WorkStore.effect_summary(effects)
        if len(effects) > 8:
            feedback["effectsTruncated"] = True
    rows = []
    for step in task.steps:
        receipt = getattr(step, "feedback", {}) or {}
        if not receipt and step.effects:
            receipt = compact_job_feedback({"effects": step.effects}, operation=step.operation, status=step.outcome)
        if receipt:
            rows.append({"stepId": step.id, "operation": step.operation, "status": step.status,
                         **{key: receipt[key] for key in (*_PROGRESS_KEYS, "actualSummary", "skipReasons", "failureReasons", "reason") if key in receipt}})
    if rows:
        feedback["stepResults"] = rows
        feedback["countsScope"] = "lastNativeCommand"
    feedback["stepsCompleted"] = sum(step.status == "completed" for step in task.steps)
    feedback["remainingSteps"] = [{"stepId": step.id, "operation": step.operation, "status": step.status}
                                  for step in task.steps if step.status != "completed"]
    # The player summary covers all recorded receipts; top-level counters still
    # describe the last command and must not masquerade as a unique plot count.
    summary = {"actualSummary": feedback["actualSummary"]}
    if rows and all(row.get("operation") == "refill_watering_can" for row in rows):
        summary["nativeCountsScope"] = "waterSourceCandidates"
    for key in ("skippedCount", "failedCount", "unprocessedTargetCount"):
        counts = [row[key] for row in rows if isinstance(row.get(key), int)]
        if counts:
            summary[key] = sum(counts)
        elif key in feedback:
            summary[key] = feedback[key]
    for key in ("skipReasons", "failureReasons"):
        counts = Counter()
        for row in rows:
            counts.update(row.get(key) or {})
        if counts:
            summary[key] = dict(counts.most_common(8))
        elif key in feedback:
            summary[key] = feedback[key]
    if feedback.get("reason"):
        summary["reason"] = feedback["reason"]
    feedback["progressSummary"] = feedback_summary(summary)
    return feedback
