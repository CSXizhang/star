"""Shared, bounded facts for the next decision, player replies and memory."""

import json
from collections import Counter
from typing import Any

from stardew_ai_runtime.work_state import WorkStore

_PROGRESS_KEYS = (
    "targetCount", "completedCount", "skippedCount", "failedCount",
    "unprocessedTargetCount", "remaining", "remainingCountSource", "preDispatchObservedCount",
    "worldRevision", "snapshotRevision", "finalWorldRevision", "resources",
    "targetCountSource", "unprocessedTargetCountSource",
    "candidateCount", "nativeCountsScope", "inventoryDelta", "inventoryDeltaScope", "inventoryDeltaObserved", "inventoryDeltaTiming", "inventoryDeltaTruncated", "inventoryDeltaEntryCount",
    "requestedQuantity", "actualQuantity", "remainingQuantity", "quantityUnit",
    "finalLocation", "finalTile", "requestedTile", "targetAdjusted", "hopCount",
    # This is the operation's observed scope, not completion of its milestone.
    "goalSatisfied", "knownNoWork", "scopeObservationIncomplete",
    "resultUncertain", "verificationRequired", "reconciliation",
)


def timed_out_receipt(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    error = raw.get("error")
    code = raw.get("reasonCode") or (error.get("code") if isinstance(error, dict) else "")
    return "TIMEOUT" in str(code).upper() or "TIMED_OUT" in str(code).upper()


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
    if isinstance(raw.get("details"), dict):
        raw = {**{k: v for k, v in raw["details"].items() if k in _PROGRESS_KEYS}, **raw}
    native_error = raw.get("error")
    if isinstance(native_error, dict):
        raw = {**raw, "reasonCode": raw.get("reasonCode") or native_error.get("code"),
               "message": raw.get("message") or native_error.get("message")}
    details = raw.get("details")
    if not raw.get("message") and isinstance(details, dict) and isinstance(details.get("message"), str):
        raw = {**raw, "message": details["message"]}
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
    if isinstance(raw.get("inventoryDelta"), list) and len(raw["inventoryDelta"]) > 8:
        feedback["inventoryDeltaTruncated"] = True
        feedback["inventoryDeltaEntryCount"] = len(raw["inventoryDelta"])
    for kind, field in (("skipped", "skipReasons"), ("failed", "failureReasons")):
        reasons = _reason_counts(raw, kind)
        if reasons:
            feedback[field] = reasons
    if len(effects) > 8:
        feedback["effectCount"] = len(effects)
        feedback["effectsTruncated"] = True
    if len(json.dumps(feedback, ensure_ascii=False)) > 2400:
        essential = {key: feedback[key] for key in (*_PROGRESS_KEYS, "skipReasons", "failureReasons", "commandId") if key in feedback}
        # The native message is the original explanation; keep it reachable even
        # when the full feedback had to be truncated away.
        feedback = {"operation": operation, "status": feedback["status"], "nextBusiness": "new_model_decision_required",
                    "reasonCode": raw.get("reasonCode"), "message": raw.get("message"),
                    "effectCount": len(effects), "detailsTruncated": True, **essential}
    if timed_out_receipt(raw):
        feedback["resultUncertain"] = True
        feedback["verificationRequired"] = "Read current inventory and the affected location before choosing remaining work; absent effects do not establish zero output."
        if not effects:
            actual_summary = "动作超时，实际变化尚未确认；不代表没有产出"
    feedback["actualSummary"] = actual_summary
    reasons = [str(raw.get("reasonCode", "")), *feedback.get("skipReasons", {}),
               *feedback.get("failureReasons", {})]
    if any("unreachable" in reason.lower() or reason.upper() in {
        "PATH_BLOCKED", "NO_PATH", "PATH_NOT_FOUND", "NATIVE_COLLISION"
    } for reason in reasons):
        feedback["obstacleAssessment"] = (
            "Inspect the blocked area first. Decide whether identified obstacles should be cleared "
            "using available tools/stamina, clearing value and player preservation preferences. "
            "Unreachable does not prove removable. Choose another route or target if clearing is unsuitable."
        )
    # Planned observations used to settle as a generic effect-free command and
    # lose their actual facts. The next turn then had to re-read (or substitute
    # its old local tile sample). Keep a bounded, provenance-labelled receipt.
    observation = raw.get("observation")
    if (operation in {"talk_npc", "interact_tile", "native_menu_select", "native_advance",
                      "native_answer", "fish", "attack", "eat_food"}
            and isinstance(raw.get("details"), dict) and raw["details"]
            and not isinstance(observation, dict)):
        observation = {"provenance": "native-action-receipt", "operation": operation,
                       "commandId": raw.get("commandId"), "worldRevision": raw.get("worldRevision"),
                       "facts": raw["details"]}
    if operation in {"observe_player_activities", "get_status", "get_work_overview", "query_inventory", "query_chests", "query_planting_options", "query_route", "query_shop", "query_farm_work",
                     "observe_production", "observe_farming_helpers", "observe_farm_space",
                     "observe_crafting", "observe_building_services", "observe_machines", "observe_livestock"} and not raw.get("error") and not isinstance(observation, dict) and (
                         raw.get("status") not in {"rejected", "failed", "unavailable"}
                         and raw.get("terminalState") not in {"failed", "rejected", "unknown"}
                         and raw.get("reasonCode") not in {"STEP_DISPATCH_REJECTED", "TRANSPORT_RETRY_EXHAUSTED"}):
        # All are existing structured native read operations. Preserve their
        # returned facts, not an inferred effect or an invented world change.
        facts = {key: value for key, value in raw.items()
                 if key not in {"observation", "knownNoWork", "effects", "commandId", "taskId"}}
        # The scheduler exposes native activities both at the top level and
        # inside details. Count and deliver them once: two identical copies can
        # exceed the receipt budget and hide an otherwise usable observation.
        details = facts.get("details")
        if (operation == "observe_player_activities"
                and isinstance(facts.get("activities"), dict)
                and isinstance(details, dict)
                and details.get("activities") == facts["activities"]):
            facts["details"] = {key: value for key, value in details.items()
                                if key != "activities"}
        observation = {"provenance": "native-read-operation", "operation": operation, "facts": facts}
    if (operation == "observe_player_activities" and isinstance(observation, dict)
            and len(json.dumps(observation, ensure_ascii=False).encode("utf-8")) > 16000):
        facts = observation.get("facts") or {}
        activities = facts.get("activities") or (facts.get("details") or {}).get("activities")
        if isinstance(activities, dict):
            # Keep current interaction facts even when the location has many NPCs,
            # shoreline tiles or boundary exits. No action is selected here.
            compact = dict(activities)
            player = activities.get("playerTile") or {}
            for key, value in activities.items():
                if isinstance(value, list):
                    rows = value
                    if key == "festivalExits" and all(type(player.get(k)) is int for k in ("x", "y")):
                        rows = sorted(value, key=lambda row: sum(abs((row.get("standTile") or {}).get(k, player[k])-player[k]) for k in ("x", "y")))
                    compact[key] = rows[:8]
                    compact[key + "Count"] = len(value)
                    compact[key + "Truncated"] = len(value) > 8
            observation = {**observation, "facts": {"activities": compact}, "detailsTruncated": True,
                "retrieve": {"tool": "call_capability", "parameters": {"tool": operation, "params": params or {}}}}
    if isinstance(observation, dict):
        if len(json.dumps(observation, ensure_ascii=False).encode("utf-8")) <= 16000:
            feedback["observation"] = observation
        elif observation.get("provenance") == "native-action-receipt":
            # A historical action cannot be retrieved by running it again.
            # Keep its identity and direct the model to a fresh read instead.
            feedback["observation"] = {
                "provenance": "native-action-receipt", "operation": operation,
                "commandId": raw.get("commandId"), "worldRevision": raw.get("worldRevision"),
                "status": "details-truncated", "reason": "observation-too-large-for-job-receipt",
                "nextAction": "Inspect current state with get_status or observe_player_activities. Do not repeat the action to retrieve this historical receipt.",
            }
        else:
            feedback["observation"] = {"status": "available-on-demand", "reason": "observation-too-large-for-job-receipt",
                                       "operation": operation, "parameters": params or {},
                                       "retrieve": {"tool": "call_capability", "parameters": {"tool": operation, "params": params or {}}},
                                       "nextAction": "Read the existing native details with call_capability. Repeating this observation as submit_plan returns the same bounded receipt; it does not reveal the omitted facts."}
        if not effects and observation.get("provenance") == "native-action-receipt":
            feedback["actualSummary"] = "已记录原生交互回执；物品或世界变化以实际效果为准"
        elif not effects:
            feedback["actualSummary"] = "已取得观察结果；不是物品或田地变化"
    if raw.get("reasonCode") and raw["reasonCode"] != "OK" and not raw["knownNoWork"]:
        feedback["reason"] = WorkStore.reason_summary(raw["reasonCode"], raw.get("message"))
    feedback["progressSummary"] = feedback_summary(feedback)
    return feedback


def project_task_feedback_state(feedback: dict[str, Any], task: Any) -> dict[str, Any]:
    """Refresh task state while retaining the historical native receipt's facts."""
    steps = task.get("steps", []) if isinstance(task, dict) else task.steps
    rows = [{"stepId": step["id"], "operation": step["operation"], "status": step["status"]}
            if isinstance(step, dict) else
            {"stepId": step.id, "operation": step.operation, "status": step.status}
            for step in steps]
    current = {row["stepId"]: row["status"] for row in rows}
    projected = {**feedback,
                 "taskStatus": task.get("status") if isinstance(task, dict) else getattr(task, "status", None),
                 "stepsCompleted": sum(row["status"] == "completed" for row in rows),
                 "remainingSteps": [row for row in rows if row["status"] != "completed"]}
    if isinstance(feedback.get("stepResults"), list):
        projected["stepResults"] = [{**row, "status": current.get(row.get("stepId"), row.get("status"))}
                                    for row in feedback["stepResults"]]
    return projected


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
                         **{key: receipt[key] for key in (*_PROGRESS_KEYS, "commandId", "reasonCode", "message", "actualSummary", "skipReasons", "failureReasons", "reason", "observation") if key in receipt}})
    if rows:
        feedback["stepResults"] = rows
        if any("observation" in row for row in rows):
            feedback.pop("observation", None)  # one copy, tied to its original step
        feedback["countsScope"] = "lastNativeCommand"
    feedback = project_task_feedback_state(feedback, task)
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
