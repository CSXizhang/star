"""Regression for real-provider submit_plan failures at the FastMCP boundary."""

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.decision_context import build_decision_context
from stardew_ai_runtime.mcp_server import MEMORY_WRITE_SCHEMA, create_mcp_server
from stardew_ai_runtime.work_state import WorkStore


def setup_server(tmp_path):
    class Scheduler:
        run_dir = tmp_path

        async def get_status(self):
            return {"saveId": "save"}

    return create_mcp_server(scheduler=Scheduler(), run_dir=tmp_path, surface="light")


@pytest.mark.parametrize("task", [
    {"id": "t2", "label": "浇灌旱地", "operation": "water_auto", "params": {}},
    {"id": "t2", "label": "浇灌旱地", "operation": "water_auto", "params": {"maxTiles": 10}},
    {"id": "t3", "label": "浇灌剩余1块干地", "params": {}, "type": "water_auto"},
    {"title": "浇水", "steps": [{"operation": "water_auto", "params": {"max_tiles": 10}}]},
])
def test_provider_shapes_succeed_once_through_bridge_and_fastmcp(tmp_path, monkeypatch, task):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    store = bridge._work_store
    goal = store.add_goal("save", "收成熟防风草并浇干地", source="agent")
    server = setup_server(tmp_path)
    results = []

    class Provider:
        def run(self, active_task, conversation_id, prompt):
            _, result = asyncio.run(server.call_tool("submit_plan", {"tasks": [task]}))
            results.append(result)
            return {"success": True, "response": "selected"}

    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    monkeypatch.setattr(bridge, "_get_backend", lambda: Provider())
    bridge._execute_turn(ActiveChatTask(request_id="turn", save_id="save"), None, "浇干地")
    assert len(results) == 1
    assert results[0]["goalId"] == goal.id
    assert results[0]["goalCreated"] is False
    selected = results[0]["tasks"][0]
    assert selected["title"] == task.get("label", task.get("title"))
    assert selected["steps"][0]["operation"] == "water_auto"
    if task.get("params", {}).get("maxTiles"):
        assert selected["steps"][0]["params"] == {"max_tiles": 10}
    assert store.state("save").decision["selected"] is True


def test_schema_and_discovery_expose_one_nested_task(tmp_path):
    async def run():
        server = setup_server(tmp_path)
        tool = next(t for t in await server.list_tools() if t.name == "submit_plan")
        schema = tool.inputSchema
        tasks = schema["properties"]["tasks"]
        assert tasks["minItems"] == tasks["maxItems"] == 1
        assert "tasks" in schema["required"]
        nested = schema["$defs"]["ShortJobTask"]
        assert set(nested["required"]) == {"title", "steps"}
        assert nested["properties"]["dependencies"]["maxItems"] == 0
        assert "operation" in schema["$defs"]["ShortJobStep"]["required"]
        assert "exactly 1" in MEMORY_WRITE_SCHEMA["submit_plan"]["parameters"]["tasks"]

    asyncio.run(run())


@pytest.mark.parametrize("task", [
    {"title": "water", "steps": [{"operation": "water_auto"}], "dependencies": ["t1"]},
    {"title": "water", "steps": [{"operation": "water_auto"}], "operation": "harvest_auto"},
    {"title": "water", "operation": "water_auto", "type": "harvest_auto"},
    {"title": "water", "operation": "water_auto", "params": {"maxTiles": 10, "max_tiles": 1}},
    {"title": "water", "operation": "water_auto", "params": {"maxTiles": 65}},
    {"title": "water", "steps": [{"operation": "water_auto", "wait": {"type": "gameDay"}}]},
])
def test_ambiguous_or_unsafe_inputs_select_nothing(tmp_path, monkeypatch, task):
    store = WorkStore(tmp_path / "data" / "work-state.json")
    store.begin_decision("save", "decision")
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision")
    with pytest.raises(ToolError):
        asyncio.run(setup_server(tmp_path).call_tool("submit_plan", {"goal_text": "care", "tasks": [task]}))
    assert not store.state("save").decision["selected"]
    assert not store.list_tasks("save")
    assert not store.list_goals("save")


def test_multi_business_can_be_repaired_once_without_consuming_decision(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "data" / "work-state.json")
    goal = store.add_goal("save", "harvest then water")
    store.begin_decision("save", "decision")
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision")
    server = setup_server(tmp_path)

    async def run():
        tasks = [{"title": "harvest", "steps": [{"operation": "harvest_auto"}]},
                 {"title": "water", "dependencies": ["t1"], "steps": [{"operation": "water_auto"}]}]
        with pytest.raises(ToolError):
            await server.call_tool("submit_plan", {"tasks": tasks})
        assert not store.state("save").decision["selected"]
        _, result = await server.call_tool("submit_plan", {"tasks": tasks[:1]})
        assert result["goalId"] == goal.id
        assert len(result["tasks"]) == 1
        with pytest.raises(ToolError, match="NEW_MODEL_DECISION_REQUIRED"):
            await server.call_tool("submit_plan", {"tasks": tasks[:1]})

    asyncio.run(run())


@pytest.mark.parametrize("case", ["none", "many", "cancelled", "expired"])
def test_no_guessing_or_stale_goal_binding(tmp_path, monkeypatch, case):
    store = WorkStore(tmp_path / "data" / "work-state.json")
    if case != "none":
        goal = store.add_goal("save", "care")
    if case == "many":
        store.add_goal("save", "other")
    store.begin_decision("save", "decision")
    if case == "cancelled":
        store.cancel_goal("save", goal.id)
    if case == "expired":
        store._mutate("save", lambda state: state.decision.update(expires=0))
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision")
    with pytest.raises(ToolError):
        asyncio.run(setup_server(tmp_path).call_tool("submit_plan", {
            "tasks": [{"title": "water", "steps": [{"operation": "water_auto"}]}]}))
    assert not store.list_tasks("save")


def test_decision_context_keeps_goal_identity():
    ctx = build_decision_context({"world": {"year": 1}}, work={"goals": [
        {"id": "goal-1", "text": "care", "source": "user"}]})
    assert ctx["goals"][0]["id"] == "goal-1", json.dumps(ctx)


@pytest.mark.parametrize("explicit", ["goal_id", "goal_text"])
def test_explicit_goal_overrides_decision_binding(tmp_path, monkeypatch, explicit):
    store = WorkStore(tmp_path / "data" / "work-state.json")
    original = store.add_goal("save", "care")
    store.begin_decision("save", "decision")
    other = store.add_goal("save", "different business")
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision")
    _, result = asyncio.run(setup_server(tmp_path).call_tool("submit_plan", {
        explicit: other.id if explicit == "goal_id" else other.text,
        "tasks": [{"title": "water", "steps": [{"operation": "water_auto"}]}]}))
    assert result["goalId"] == other.id != original.id


def test_jobs_typo_rejected_with_canonical_required_field(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "data" / "work-state.json")
    store.begin_decision("save", "decision")
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "decision")
    with pytest.raises(ToolError, match="tasks"):
        asyncio.run(setup_server(tmp_path).call_tool("submit_plan", {
            "jobs": [{"reason": "收获防风草", "type": "harvest_auto"}]}))
    assert not store.state("save").decision["selected"]

