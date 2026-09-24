"""Bounded facts from real results; never materializes invented state."""
import json
from typing import Any


def compact_job_feedback(result: Any, *, operation: str = "", status: str | None = None) -> dict[str, Any]:
    raw = result if isinstance(result, dict) else {}
    feedback = {"operation": operation, "status": status or raw.get("status", raw.get("outcome", "unknown")),
                "nextBusiness": "new_model_decision_required"}
    for key in ("commandId", "reasonCode", "message", "error", "terminalState", "effects", "inventoryDelta", "resourceDelta", "tile", "locationId", "completedCount", "failedCount"):
        value = raw.get(key)
        if value is not None:
            feedback[key] = value[:8] if isinstance(value, list) else value[:320] if isinstance(value, str) else value
    if len(json.dumps(feedback, ensure_ascii=False)) > 2400:
        feedback = {"operation": operation, "status": feedback["status"], "nextBusiness": "new_model_decision_required", "reasonCode": raw.get("reasonCode"), "effectCount": len(raw.get("effects") or []), "detailsTruncated": True}
    return feedback
