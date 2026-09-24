import asyncio
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.plan_executor import PlanExecutor, StepExecution, classify_step_outcome
from stardew_ai_runtime.work_state import WorkStore


@pytest.mark.parametrize("raw",[None,"success",[],{}, {"status":"executed","terminalState":"failed","effects":[],"error":"blocked path"}, {"outcome":"rejected","reasonCode":"MCP_TOOL_ERROR"}, {"isError":True,"status":"completed"}, {"status":"executed","terminalState":"unknown"}])
def test_invalid_failed_and_rejected_never_success(raw):
    assert classify_step_outcome(raw)[0] != "completed"

def test_error_dict_reason_uses_code_not_stringified_dict():
    outcome, reason = classify_step_outcome({"status":"executed","terminalState":"rejected","error":{"code":"no-produce","message":"Precondition not satisfied: no-produce.","details":None,"retryable":False}})
    assert outcome == "partial"
    assert reason == "no-produce"

@pytest.mark.parametrize("message",["unsupported parameter tiles","World snapshot does not include farming","no watering can","native path blocked"])
def test_business_exception_has_terminal_no_retry(tmp_path,message):
    store=WorkStore(tmp_path/"work.json")
    store.begin_decision("s","d")
    store.submit_plan("s",goal_text="g",decision_token="d",tasks=[{"title":"t","steps":[{"operation":"water_auto"}]}])
    calls=[]
    async def dispatch(*args):
        calls.append(args)
        raise RuntimeError(message)
    e=PlanExecutor(store,dispatch=dispatch)
    result=asyncio.run(e.run_once("s","w"))
    assert result.outcome=="partial" and result.message==message
    assert result.result["status"]=="rejected"
    assert asyncio.run(e.run_once("s","w")).status=="idle" and len(calls)==1


def test_worker_reply_retains_request_and_real_failure(tmp_path):
    bridge=ChatBridge(run_dir=tmp_path,enable_plan_worker=False,backend="agy")
    bridge._send_reply=AsyncMock()
    bridge._job_reply_binding=("request","save","task",None)
    asyncio.run(bridge._publish_job_progress(StepExecution(status="deferred",task_id="task",operation="refill_watering_can",outcome="partial",message="farming absent")))
    envelope=bridge._send_reply.call_args.args[1]
    assert envelope.payload["status"]=="job-failed"
    assert envelope.payload["requestId"]=="request" and "farming absent" in envelope.payload["replyText"]
    asyncio.run(bridge._publish_job_progress(StepExecution(status="executed",task_id="old-task",outcome="completed")))
    assert bridge._send_reply.await_count==1


def test_compatibility_rejects_old_dll_without_writing(tmp_path, monkeypatch):
    import hashlib
    import json

    import stardew_ai_runtime.compatibility as compat
    monkeypatch.setattr(compat,"__file__",str(tmp_path/"runtime/src/stardew_ai_runtime/compatibility.py"))
    manifest=tmp_path/"artifacts/releases/repair-r43/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"modSha256":hashlib.sha256(b"new").hexdigest().upper()}))
    mod=tmp_path/"normal-Mod"
    mod.mkdir()
    dll=mod/"StardewAI.Companion.Mod.dll"
    dll.write_bytes(b"old")
    with pytest.raises(compat.CompatibilityError,match="MOD_RUNTIME_MISMATCH"):
        compat.assert_native_compatible(mod)
    assert dll.read_bytes()==b"old"
    dll.write_bytes(b"new")
    compat.assert_native_compatible(mod)


def test_model_reply_is_selected_then_worker_reports_failure(tmp_path, monkeypatch):
    bridge=ChatBridge(run_dir=tmp_path,enable_plan_worker=False,backend="kimi")
    bridge._send_reply=AsyncMock()
    def choose(*args):
        store=bridge._work_store
        store.begin_decision("save","decision")
        store.submit_plan("save",goal_text="one job",decision_token="decision",tasks=[{"id":"t","title":"water","steps":[{"operation":"water_auto"}]}])
        return {"success":True,"response":"selected","conversation_id":None}
    monkeypatch.setattr(bridge,"_execute_turn",choose)
    asyncio.run(bridge.handle_chat_submit(None,"r","water","save"))
    assert bridge._send_reply.call_args.args[1].payload["status"]=="selected"
    asyncio.run(bridge._publish_job_progress(StepExecution(status="deferred",task_id="t",operation="water_auto",outcome="unknown",message="network retry exhausted")))
    assert bridge._send_reply.call_args.args[1].payload["status"]=="job-failed"
    assert bridge._send_reply.call_args.args[1].payload["requestId"]=="r"


def test_missing_compatibility_proof_is_unknown(tmp_path, monkeypatch):
    import stardew_ai_runtime.compatibility as compat
    monkeypatch.setattr(compat,"__file__",str(tmp_path/"runtime/src/stardew_ai_runtime/compatibility.py"))
    with pytest.raises(compat.CompatibilityError,match="COMPATIBILITY_UNKNOWN"):
        compat.assert_native_compatible(tmp_path)
    manifest=tmp_path/"artifacts/releases/repair-r43/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"modSha256":"expected"}')
    with pytest.raises(compat.CompatibilityError,match="COMPATIBILITY_UNKNOWN"):
        compat.assert_native_compatible(tmp_path)
