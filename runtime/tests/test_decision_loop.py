import asyncio
import json
from pathlib import Path
import pytest
from stardew_ai_runtime.work_state import WorkStore, WorkStateError
from stardew_ai_runtime.chat_bridge import PlanWorker
from stardew_ai_runtime.job_feedback import compact_job_feedback


def tasks(op="water_auto", steps=None):
    return [{"title": "short job", "steps": steps or [{"operation": op, "params": {}}]}]


def choose(store, token="decision-1", plan=None):
    store.begin_decision("save", token)
    return store.submit_plan("save", goal_text="farm care", tasks=plan or tasks(), decision_token=token)


def test_same_decision_cannot_select_second_business_or_bundle(tmp_path):
    store=WorkStore(tmp_path/"work.json")
    choose(store)
    with pytest.raises(WorkStateError, match="NEW_MODEL"):
        store.submit_plan("save", goal_text="store", tasks=tasks("deposit_to_chest"), decision_token="decision-1")
    store.begin_decision("save", "second")
    with pytest.raises(WorkStateError, match="ONE_SHORT"):
        store.submit_plan("save", goal_text="many", tasks=tasks()+tasks(), decision_token="second")
    with pytest.raises(WorkStateError, match="MULTIPLE_BUSINESSES"):
        store.submit_plan("save", goal_text="mixed", tasks=tasks(steps=[{"operation":"water_auto"},{"operation":"ship_items"}]), decision_token="second")


def test_legacy_plan_is_intent_and_revocation_stops_next_step(tmp_path):
    store=WorkStore(tmp_path/"work.json")
    goal=store.add_goal("save", "old plan", source="agent")
    store.create_plan("save", goal.id, tasks())
    assert not store.has_ready_step("save")
    choose(store)
    assert store.has_ready_step("save")
    store.revoke_decision("save")
    assert not WorkStore(tmp_path/"work.json").has_ready_step("save")


def test_multi_navigation_and_native_steps_run_without_model_then_stop(tmp_path):
    store=WorkStore(tmp_path/"work.json")
    choose(store, plan=tasks(steps=[{"operation":"navigate_to","params":{"tile":{"x":i,"y":1}}} for i in range(3)]+[{"operation":"water_auto","params":{}}]))
    calls=[]
    class Client:
        async def current_save_id(self): return "save"
        async def call_tool(self, name, args):
            calls.append(args["operation"])
            return {"status":"completed", "effects":[{"kind":"native-test-effect"}]}
    worker=PlanWorker(store,Client())
    asyncio.run(worker.evaluate())
    assert calls == ["navigate_to"]*3+["water_auto"]
    assert store.state("save").last_job["status"] == "completed"
    asyncio.run(worker.evaluate())
    assert len(calls)==4
    assert not store.has_ready_step("save")


def test_direct_and_plan_share_decision_budget(tmp_path):
    store=WorkStore(tmp_path/"work.json")
    store.begin_decision("save","one")
    store.select_direct_job("save","one","water_auto")
    with pytest.raises(WorkStateError): store.select_direct_job("save","one","ship_items")
    with pytest.raises(WorkStateError): store.submit_plan("save",goal_text="again",tasks=tasks(),decision_token="one")


def test_feedback_is_compact_and_does_not_invent_state():
    result=compact_job_feedback({"status":"completed","snapshot":{"large":"x"*10000},"effects":[{"tile":i} for i in range(100)]},operation="water_auto")
    assert "snapshot" not in result and len(result["effects"])==8
    assert "inventoryDelta" not in result
    assert len(json.dumps(result))<1000


def test_cross_day_does_not_resume_old_job(tmp_path):
    store=WorkStore(tmp_path/"work.json")
    store.settle_game_day("save",year=1,season="spring",day=1)
    choose(store)
    store.settle_game_day("save",year=1,season="spring",day=2)
    assert not WorkStore(tmp_path/"work.json").has_ready_step("save")


def test_mcp_direct_and_capability_cannot_bypass_job_selection(tmp_path, monkeypatch):
    from stardew_ai_runtime.mcp_server import create_mcp_server
    from mcp.server.fastmcp.exceptions import ToolError
    class Scheduler:
        run_dir=tmp_path
        async def get_status(self): return {"saveId":"save"}
    server=create_mcp_server(scheduler=Scheduler(),run_dir=tmp_path,surface="full")
    tools={t.name:t.fn for t in server._tool_manager.list_tools()}
    store=WorkStore(tmp_path/"data/work-state.json")
    store.begin_decision("save","one")
    monkeypatch.setenv("STARDEW_DECISION_TOKEN","one")
    first=asyncio.run(tools["water_auto"](max_tiles=3))
    assert first["status"]=="job-selected" and first["effectStatus"]=="not_executed_yet"
    with pytest.raises(ToolError,match="NEW_MODEL"):
        asyncio.run(tools["call_capability"](tool="water_auto",params={"max_tiles":3}))
    with pytest.raises(ToolError,match="UNAUTHORIZED"):
        asyncio.run(tools["dispatch_plan_operation"](operation="water_auto",params={"max_tiles":3},command_id="forged"))


def test_free_mode_job_terminal_requests_next_model_without_user_approval(tmp_path):
    from stardew_ai_runtime.autonomy import AutonomyController
    control=AutonomyController(tmp_path/"autonomy.json")
    control.set_enabled("save",True)
    snapshot={"world":{"year":1,"season":"spring","dayOfMonth":1}}
    candidate=control.next_candidate("save",snapshot)
    fingerprint=control.fingerprint("save",snapshot,candidate,control.state("save"))
    control.record_world_event("save",fingerprint,1)
    assert control.next_candidate("save",snapshot) is None
    control.request_job_decision("save")
    assert control.next_candidate("save",snapshot) is not None


def test_interrupt_at_step_boundary_does_not_advance(tmp_path):
    store=WorkStore(tmp_path/"work.json")
    choose(store,plan=tasks(steps=[{"operation":"navigate_to"},{"operation":"water_auto"}]))
    calls=[]
    class Client:
        async def current_save_id(self): return "save"
        async def call_tool(self,name,args):
            calls.append(args["operation"])
            store.revoke_decision("save")
            return {"status":"completed"}
    asyncio.run(PlanWorker(store,Client()).evaluate())
    assert calls==["navigate_to"]
    assert not store.has_ready_step("save")


def test_native_cancel_bypasses_work_queue(tmp_path):
    from types import SimpleNamespace
    from stardew_ai_runtime.chat_bridge import InternalMcpPlanClient
    async def exercise():
        client=InternalMcpPlanClient(tmp_path)
        client._task=asyncio.current_task()
        calls=[]
        class Session:
            async def call_tool(self,name,args):
                calls.append(args["operation"])
                return SimpleNamespace(isError=False,structuredContent={"status":"cancelling"},content=[])
        client._live_session=Session()
        await client.call_tool("dispatch_plan_operation",{"operation":"cancel_task","params":{}})
        assert calls==["cancel_task"]
    asyncio.run(exercise())


def test_fake_provider_two_decisions_receive_real_worker_feedback(tmp_path, monkeypatch):
    """No provider process: production grant -> registered MCP -> worker -> prompt."""
    import os
    from stardew_ai_runtime.chat_bridge import ChatBridge, ActiveChatTask
    from stardew_ai_runtime.mcp_server import create_mcp_server
    from mcp.server.fastmcp.exceptions import ToolError
    bridge=ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    store=bridge._work_store
    class Scheduler:
        run_dir=tmp_path
        async def get_status(self): return {"saveId":"save"}
    server=create_mcp_server(scheduler=Scheduler(),run_dir=tmp_path,surface="full")
    registered={t.name:t.fn for t in server._tool_manager.list_tools()}
    prompts=[]; tokens=[]; dispatches=[]
    class FakeProvider:
        def run(self, task, cid, prompt):
            prompts.append(prompt); tokens.append(os.environ["STARDEW_DECISION_TOKEN"])
            steps=[{"operation":"navigate_to","params":{"tile":{"x":i,"y":1}}} for i in range(3)]
            steps += [{"operation":"water_auto","params":{"max_tiles":3}}]
            asyncio.run(registered["submit_plan"](tasks=tasks(steps=steps),goal_text="care"))
            with pytest.raises(ToolError,match="NEW_MODEL"):
                asyncio.run(registered["submit_plan"](tasks=tasks("ship_items"),goal_text="later"))
            return {"success":True,"response":"selected"}
    # This test intentionally has no game/DLL; isolate the backend from packaging checks.
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible",lambda _:None)
    monkeypatch.setattr(bridge,"_get_backend",lambda:FakeProvider())
    class NativeClient:
        async def current_save_id(self): return "save"
        async def call_tool(self,name,args):
            dispatches.append(args["operation"])
            return {"status":"completed","effects":[{"kind":"offline-native-fixture"}],"inventoryDelta":{"water":-3}}
    worker=PlanWorker(store,NativeClient())
    bridge._execute_turn(ActiveChatTask(request_id="d1",save_id="save"),None,bridge._format_agent_prompt("care","save"))
    assert dispatches==[]
    asyncio.run(worker.evaluate())
    assert len(dispatches)==4 and len(prompts)==1
    asyncio.run(worker.evaluate())
    assert len(dispatches)==4 and len(prompts)==1
    prompt=bridge._format_agent_prompt("continue","save")
    assert 'offline-native-fixture' in prompt, json.dumps(store.state('save').last_job)
    bridge._execute_turn(ActiveChatTask(request_id="d2",save_id="save"),None,prompt)
    assert len(prompts)==2 and tokens[0]!=tokens[1]
    store.revoke_decision("save")
    asyncio.run(worker.evaluate())
    assert len(dispatches)==4


def test_same_active_chat_request_is_transport_replay(tmp_path, monkeypatch):
    from stardew_ai_runtime.chat_bridge import ChatBridge, ActiveChatTask
    bridge=ChatBridge(run_dir=tmp_path,enable_plan_worker=False)
    bridge._active_task=ActiveChatTask(request_id="same",save_id="save")
    def forbidden(*args): raise AssertionError("replay must not invoke provider")
    monkeypatch.setattr(bridge,"_execute_turn",forbidden)
    asyncio.run(bridge.handle_chat_submit(None,"same","same","save"))
    assert not bridge._active_task.cancelled
