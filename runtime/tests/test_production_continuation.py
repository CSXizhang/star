"""Production resumes on real prerequisites, without idle clock polling."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.protocol import Envelope
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.work_state import WorkStore


def test_production_conditions_wake_once_across_reload_and_next_machine_cycle(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._autonomy.set_enabled("a", True)
    store = bridge._work_store
    goal = store.add_goal("a", "持续生产", source="user")
    triggers = [
        {"type": "gameTime", "year": 1, "season": "spring", "day": 2, "timeOfDay": 900},
        {"type": "resource", "resource": "stamina", "minAmount": 30},
        {"type": "resource", "resource": "water", "minAmount": 10},
        {"type": "machineReady", "locationId": "Shed", "tile": {"x": 3, "y": 4}},
    ]
    todos = [store.add_todo("a", intent=str(t), trigger=t, goal_id=goal.id) for t in triggers]
    payload = {"world": {"year": 1, "season": "spring", "dayOfMonth": 2, "timeOfDay": 800},
               "companion": {"stamina": 5, "waterCanLevel": 1},
               # Existing crop priority must not hide a newly due prerequisite.
               "farmWork": {"cropUnwateredTiles": [{"x": 1, "y": 2}]},
               "productionSignals": []}

    async def tick():
        envelope = Envelope.from_mapping({"protocolVersion": "0.1", "messageType": "world.snapshot",
            "messageId": "m", "senderInstanceId": "mod", "sequenceNumber": 1, "worldRevision": 1,
            "sentAt": "2026-01-01T00:00:00Z", "saveId": "a", "gameSessionId": "s", "payload": payload})
        tracked = set()
        await bridge._maybe_schedule_autonomy(envelope, "a", tracked, None, force=True)
        await asyncio.gather(*tracked)

    async def run():
        nonlocal bridge, store
        bridge.handle_chat_submit = AsyncMock()
        await tick()
        assert bridge.handle_chat_submit.await_count == 1
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        store = bridge._work_store
        bridge.handle_chat_submit = AsyncMock()
        for minute, stamina in [(810, 6), (820, 7), (830, 8)]:
            payload["world"]["timeOfDay"] = minute
            payload["companion"]["stamina"] = stamina
            await tick()
        assert bridge.handle_chat_submit.await_count == 0
        payload["companion"].update(stamina=35, waterCanLevel=20)
        payload["world"]["timeOfDay"] = 900
        payload["productionSignals"] = [{"locationId": "Shed", "tile": {"x": 3, "y": 4}, "isReady": True}]
        await tick()
        await tick()
        assert bridge.handle_chat_submit.await_count == 1
        date = {"year": 1, "season": "spring", "day": 2}
        assert len(store.evaluate_todos("a", snapshot=payload, game_date=date)) == 4
        assert len(store.evaluate_todos("a", snapshot={"payload": payload}, game_date=date)) == 4
        for todo in todos:
            store.complete_todo("a", todo.id)
        payload["productionSignals"] = []
        await tick()
        store.add_todo("a", intent="下一批收取", trigger=triggers[-1], goal_id=goal.id)
        await tick()
        count = bridge.handle_chat_submit.await_count
        payload["productionSignals"] = [{"locationId": "Shed", "tile": {"x": 3, "y": 4}, "isReady": True}]
        await tick()
        await tick()
        assert bridge.handle_chat_submit.await_count == count + 1
        assert WorkStore(store.state_path).state("a").goals[0].status == "active"
    asyncio.run(run())


def test_deposit_keeps_requested_map_at_both_execution_boundaries():
    async def run():
        scheduler = CompanionScheduler()
        client = MagicMock()
        client.execute_deposit_chest = AsyncMock(return_value="native")
        async def execute(**kwargs):
            assert kwargs["location_id"] == "Shed"
            await kwargs["dispatch"](client, task_id="t", idempotency_key="i", expires_seconds=30)
            return {"terminalState": "succeeded"}
        scheduler.execute_skill = execute
        await scheduler.deposit_to_chest(3, 4, ["(O)306"], location_id="Shed", command_id="stable")
        assert client.execute_deposit_chest.call_args.kwargs["location_id"] == "Shed"
        assert client.execute_deposit_chest.call_args.kwargs["command_id"] == "stable"
    asyncio.run(run())


def test_named_animal_ambiguity_uses_stable_id_on_actual_plan_dispatch_path():
    import pytest

    from stardew_ai_runtime.scheduler import SchedulerError
    async def run():
        scheduler = CompanionScheduler()
        scheduler.query_livestock = AsyncMock(return_value={"buildings": [{"animals": [
            {"name": "Hen", "animalId": "1", "locationName": "Coop", "tile": {"x": 1, "y": 2}},
            {"name": "Hen", "animalId": "2", "locationName": "Coop", "tile": {"x": 4, "y": 5}},
        ]}]})
        scheduler._execute_native_action = AsyncMock(return_value={"terminalState": "succeeded"})
        with pytest.raises(SchedulerError, match="unambiguous"):
            await scheduler.pet_animal("Hen", location_id="Coop")
        await scheduler.pet_animal(animal_id="2", location_id="Coop", command_id="stable")
        parameters = scheduler._execute_native_action.call_args.args[1]
        assert parameters["animalId"] == "2" and parameters["tiles"] == [{"x": 4, "y": 5}]
        assert scheduler._execute_native_action.call_args.kwargs["command_id"] == "stable"
    asyncio.run(run())


def test_explicit_production_activation_binds_scope_and_preserves_player_pause(tmp_path):
    from stardew_ai_runtime.autonomy import AutonomyController
    from stardew_ai_runtime.mcp_server import create_mcp_server
    store = WorkStore(tmp_path / "data" / "work-state.json")
    goal = store.add_goal("a", "持续鸡舍生产")
    autonomy = AutonomyController(tmp_path / "data" / "autonomy-state.json")
    assert not autonomy.state("a").enabled
    store.set_paused("a", True)
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.get_status = AsyncMock(return_value={"saveId": "a"})
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler)
        _, result = await server.call_tool("set_autonomy", {"enabled": True, "goal_id": goal.id})
        assert result["enabled"] and result["paused"] and result["goal_scope"] == goal.id
        assert autonomy.state("a").goal_scope == goal.id
        assert store.state("a").paused
    asyncio.run(run())


def test_external_activation_releases_socket_and_unused_decision_before_bridge_takeover(tmp_path, monkeypatch):
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    from stardew_ai_runtime.autonomy import AutonomyController
    from stardew_ai_runtime.mcp_server import create_mcp_server
    monkeypatch.setenv("STARDEW_EXTERNAL_CODEX", "1")
    monkeypatch.delenv("STARDEW_DECISION_TOKEN", raising=False)
    store = WorkStore(tmp_path / "data" / "work-state.json")
    goal = store.add_goal("a", "持续加工")
    autonomy = AutonomyController(tmp_path / "data" / "autonomy-state.json")
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.get_status = AsyncMock(return_value={"saveId": "a"})
    async def close():
        assert not autonomy.state("a").enabled
    scheduler.close = AsyncMock(side_effect=close)
    scheduler.query_machines = AsyncMock()
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler)
        await server.call_tool("begin_game_turn", {})
        assert store.state("a").decision.get("token")
        await server.call_tool("set_autonomy", {"enabled": True, "goal_id": goal.id})
        scheduler.close.assert_awaited_once()
        assert store.state("a").decision == {}
        assert autonomy.state("a").goal_scope == goal.id
        with pytest.raises(ToolError, match="AUTONOMY_OWNS_SESSION"):
            await server.call_tool("observe_machines", {"location_id": "Shed"})
        with pytest.raises(ToolError, match="GAME_BUSY"):
            await server.call_tool("begin_game_turn", {})
        scheduler.query_machines.assert_not_awaited()
        await server.call_tool("work_plan_overview", {})
    asyncio.run(run())
