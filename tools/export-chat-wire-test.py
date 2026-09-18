"""Offline cross-language fixture: real WorkStore -> ChatBridge -> Envelope JSON.

No sockets, provider, game, or native execution. Writes only the supplied test directory.
"""
import json
import sys
from pathlib import Path
from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.protocol import Envelope

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
bridge = ChatBridge(run_dir=output, backend="agy", enable_plan_worker=False)
store = bridge._work_store
store.begin_decision("wire-save", "wire-decision")
store.submit_plan("wire-save", goal_text="offline wire contract", decision_token="wire-decision",
                  tasks=[{"id": "task", "title": "等待补水", "steps": [{"id": "step", "operation": "refill_watering_can"}]}])
store.mark_task_waiting("wire-save", "task", step_id="step", condition={"type": "inventory", "params": {"itemId": "WateringCan"}}, reason_code="WIRE_WAIT")
state = bridge._autonomy.state("wire-save")
payload = bridge._autonomy_state_payload("wire-save", state)
assert payload["waitingConditions"][0]["operation"] == "refill_watering_can"
envelope = Envelope.create_autonomy_state("wire", "request", "wire-save", payload)
(output/"autonomy-state.json").write_text(json.dumps(envelope.to_mapping(), ensure_ascii=False), encoding="utf-8")

(output/"work-context.json").write_text(json.dumps(bridge._work_context("wire-save"), ensure_ascii=False), encoding="utf-8")
