"""Supplement native acceptance with the same round's exported public ACP receipts."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/companion_acceptance"))
from runner import summarize_trace, write

p = argparse.ArgumentParser()
p.add_argument("report", type=Path)
p.add_argument("receipts", type=Path)
a = p.parse_args()
report = json.loads(a.report.read_text(encoding="utf-8"))
end = datetime.fromisoformat(report["turn"]["timestamp"]).timestamp() * 1000
start = end - report["turn"]["durationSeconds"] * 1000 - 1000
events = [x for x in json.loads(a.receipts.read_text(encoding="utf-8"))
          if x["sessionId"] == report["turn"]["conversationId"] and start <= x["timestampMs"] <= end + 1000]
trace = [{"event": {"item": x}} for x in events]
summary = summarize_trace(trace)
operations = {"water_auto", "water_zone", "water_tiles", "refill_watering_can", "harvest_auto", "harvest_crop", "chop_tree",
              "pickup_items", "deposit_to_chest", "withdraw_from_chest", "ship_items", "rest", "navigate_to"}
actions = [c for c in summary["toolCalls"] if str(c["name"]).endswith("__submit_work")
           and operations.intersection((c.get("input") or {}).get("action") or {})]
checks = dict(report["checks"])
checks["real_action_tools_observed"] = any(not c["error"] and isinstance(c["result"], dict) for c in actions)
checks["action_calls_returned_terminal_results"] = bool(actions) and all(
    not c["nonterminal"] and (c["error"] or isinstance(c["result"], dict)
        and (c["result"].get("status") in {"executed", "completed", "failed", "cancelled", "rejected", "player_update"}
             or c["result"].get("status") == "no-work" and c["result"].get("targetCount") == 0
             or c["result"].get("terminalState") in {"succeeded", "failed", "cancelled"})) for c in actions)
result = {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
          "nativeReport": str(a.report), "providerSessionId": report["turn"]["conversationId"],
          "provenance": "Read-only public tool receipts of this explicitly isolated original session, filtered to the actual game round interval; native report remains unchanged.",
          "actions": actions, "publicToolReceiptCount": len(events)}
write(a.report.parent / "external-receipt-verification.json", result)
write(a.report.parent / "external-public-tool-trace.json", trace)
print(json.dumps({"status": result["status"], "actionCount": len(actions), "failed": [k for k,v in checks.items() if not v]}))
raise SystemExit(0 if result["status"] == "passed" else 1)
