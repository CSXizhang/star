"""Durable project continuation and native construction boundary regressions."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.decision_context import build_decision_context
from stardew_ai_runtime.scheduler import CompanionScheduler, PolicyViolationError
from stardew_ai_runtime.work_state import WorkStore


def test_terminal_wake_is_durable_and_save_scoped(tmp_path):
    path = tmp_path / "autonomy.json"
    ctl = AutonomyController(path)
    ctl.request_job_decision("a", "job-1")
    epoch = ctl.state("a").decision_epoch
    restarted = AutonomyController(path)
    restarted.request_job_decision("a", "job-1")
    assert restarted.state("a").decision_epoch == epoch
    restarted.request_job_decision("b", "job-1")
    assert restarted.state("b").decision_epoch == 1
    restarted.request_job_decision("a", "job-2")
    assert restarted.state("a").decision_epoch == epoch + 1


def test_idle_only_wakes_for_changed_work_signal(tmp_path):
    ctl = AutonomyController(tmp_path / "autonomy.json")
    ctl.set_enabled("a", True)
    snapshot = {"world": {"year": 1, "season": "spring", "dayOfMonth": 2}}
    candidate = ctl.next_candidate("a", snapshot, work_signal="no-due")
    fingerprint = ctl.fingerprint("a", snapshot, candidate, ctl.state("a"))
    ctl.record_world_event("a", fingerprint, 5)
    assert ctl.next_candidate("a", snapshot, work_signal="no-due") is None
    assert ctl.next_candidate("a", snapshot, work_signal="todo-now-due")
    ctl.set_paused("a", True)
    assert ctl.next_candidate("a", snapshot, work_signal="another-change") is None


def test_project_layout_survives_day_and_restart_without_context_dump(tmp_path):
    path = tmp_path / "work.json"
    store = WorkStore(path)
    goal = store.add_goal("a", "Redesign farm", source="user", constraints={"keep": "orchard"})
    project = {"phase": "path", "summary": "Keep central access", "layout": {"tiles": list(range(400))}, "openQuestions": []}
    store.revise_goal("a", goal.id, project=project)
    store.settle_game_day("a", year=1, season="spring", day=2)
    store.settle_game_day("a", year=1, season="spring", day=3)
    restarted = WorkStore(path)
    assert restarted.state("a").goals[0].project == project
    context = build_decision_context({"world": {"year": 1}}, work=restarted.overview("a"))
    assert "layout" not in context["goals"][0]["project"]
    assert context["goals"][0]["constraints"] == {"keep": "orchard"}


def test_layout_dispatch_keeps_partial_native_result_and_command_identity():
    async def run():
        sched = CompanionScheduler.__new__(CompanionScheduler)
        partial = {"outcome": "partial", "effects": [{"x": 2, "y": 3}], "failedCount": 1}
        sched._execute_native_action = AsyncMock(return_value=partial)
        result = await sched.place_items([{ "x": 2, "y": 3}], "(O)328", command_id="stable")
        assert result == partial
        assert sched._execute_native_action.call_args.kwargs["command_id"] == "stable"
        with pytest.raises(PolicyViolationError):
            await sched.remove_items([{ "x": 2, "y": 3}], "")
        assert sched._execute_native_action.await_count == 1
    asyncio.run(run())


def test_layout_adoption_is_cross_day_authorization(tmp_path):
    from stardew_ai_runtime.companion_milestones import CompanionMilestoneStore
    store = CompanionMilestoneStore(tmp_path / "milestones.json")
    work = WorkStore(tmp_path / "work.json")
    date = {"year": 1, "season": "spring", "day": 2}
    node = store.propose("a", title="Whole farm", summary="Keep orchard", target_date="1:spring:2", preparation=["layout"], game_date=date)
    adopted = store.adopt("a", node["id"], work_store=work, game_date=date)
    work.settle_game_day("a", year=1, season="spring", day=2)
    work.settle_game_day("a", year=1, season="spring", day=3)
    due = work.evaluate_todos("a", snapshot={}, game_date={"year": 1, "season": "spring", "day": 3})
    assert due[0]["id"] == adopted["todoIds"]["layout"]
    assert due[0]["expiry"] is None
    goal = work.state("a").goals[0]
    assert goal.source == "user" and goal.project["phase"] == "observe"


def test_resource_and_weather_changes_wake_but_movement_does_not(tmp_path):
    ctl = AutonomyController(tmp_path / "autonomy.json")
    ctl.set_enabled("a", True)
    snapshot = {"world": {"year": 1, "season": "spring", "dayOfMonth": 2}, "companion": {"tileX": 1}}
    candidate = ctl.next_candidate("a", snapshot)
    ctl.record_world_event("a", ctl.fingerprint("a", snapshot, candidate, ctl.state("a")), 1)
    snapshot["companion"]["tileX"] = 4
    assert ctl.next_candidate("a", snapshot) is None
    snapshot["inventory"] = {"slots": [{"itemId": "(T)Axe", "isTool": True}]}
    assert ctl.next_candidate("a", snapshot)


def test_map_image_is_mcp_content_and_rejects_arbitrary_file(tmp_path, monkeypatch):
    import base64
    from unittest.mock import MagicMock

    from mcp.server.fastmcp.exceptions import ToolError

    import stardew_ai_runtime.mcp_server as mcp_module
    from stardew_ai_runtime.mcp_server import create_mcp_server

    monkeypatch.setattr(mcp_module.tempfile, "gettempdir", lambda: str(tmp_path))
    image_path = tmp_path / "StardewAI.Companion" / "map-images" / "map.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="))
    scheduler = MagicMock()
    scheduler.query_map_image = AsyncMock(return_value={"path": str(image_path)})
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="life")
        result = await server.call_tool("observe_map_image", {})
        assert result[0].type == "image" and result[0].mimeType == "image/png"
        scheduler.query_map_image.return_value = {"path": str(tmp_path / "private.png")}
        with pytest.raises(ToolError, match="controlled image directory"):
            await server.call_tool("observe_map_image", {})
        names = {tool.name for tool in await server.list_tools()}
        assert "place_items" not in names and "remove_items" not in names
    asyncio.run(run())


def test_project_scope_rejects_unrelated_goals_and_binds_direct_actions(tmp_path):
    from stardew_ai_runtime.work_state import WorkStateError
    work = WorkStore(tmp_path / "work.json")
    project = work.add_goal("a", "Layout", source="user")
    other = work.add_goal("a", "Old job", source="user")
    work.begin_decision("a", "turn", goal_scope=project.id)
    tasks = [{"title": "place", "steps": [{"operation": "place_items", "params": {"tiles": [{"x": 1, "y": 2}], "item_id": "(O)328"}}]}]
    with pytest.raises(WorkStateError, match="GOAL_SCOPE_MISMATCH"):
        work.submit_plan("a", tasks=tasks, goal_id=other.id, decision_token="turn")
    chosen = work.submit_plan("a", tasks=tasks, goal_text="direct action", decision_token="turn")
    assert chosen["tasks"][0]["goal_id"] == project.id


def test_explicit_player_notice_survives_restart_and_deduplicates(tmp_path):
    path = tmp_path / "work.json"
    work = WorkStore(path)
    goal = work.add_goal("a", "Layout")
    notice = work.request_player_decision("a", goal.id, "Which entrance should stay?")
    restarted = WorkStore(path)
    assert restarted.pending_player_notice("a")["id"] == notice["id"]
    restarted.acknowledge_player_notice("a", notice["id"])
    restarted.request_player_decision("a", goal.id, "Which entrance should stay?")
    assert WorkStore(path).pending_player_notice("a") is None


def test_project_completion_requires_settled_native_evidence(tmp_path):
    from stardew_ai_runtime.work_state import WorkStateError
    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("a", "Layout")
    store.begin_decision("a", "turn", goal_scope=goal.id)
    job = store.submit_plan("a", goal_id=goal.id, decision_token="turn", tasks=[{
        "title": "place", "steps": [{"operation": "place_items", "params": {"tiles": [{"x": 1, "y": 2}], "item_id": "(O)328"}}]}])
    task = job["tasks"][0]
    with pytest.raises(WorkStateError, match="PROJECT_UNRESOLVED"):
        store.complete_goal("a", goal.id)
    step = task["steps"][0]
    store.claim_next_step("a", "worker")
    store.assign_command_id("a", task["id"], step["id"], "native-1")
    store.commit_step_result("a", task_id=task["id"], step_id=step["id"], command_id="native-1", outcome="completed", effects=[{"x": 1, "y": 2}])
    before = store.execution_log("a")
    closed = store.complete_goal("a", goal.id)
    assert closed.status == "completed"
    assert store.execution_log("a") == before


def test_space_region_preserves_absolute_coordinates_and_full_map_default():
    from unittest.mock import MagicMock

    from mcp.server.fastmcp.exceptions import ToolError

    from stardew_ai_runtime.mcp_server import create_mcp_server

    async def run():
        sched = CompanionScheduler.__new__(CompanionScheduler)
        region = {"x": 10, "y": 20, "width": 4, "height": 3}
        native = {"width": 4, "height": 3, "offset": {"x": 10, "y": 20},
                  "mapWidth": 80, "mapHeight": 65, "occupants": [{"x": 11, "y": 21}]}
        sched._execute_native_action = AsyncMock(return_value={"details": {"farmSpace": native}})
        assert await sched.query_farm_space(region=region) == native
        assert sched._execute_native_action.call_args.args[1]["region"] == region
        await sched.query_farm_space()
        assert "region" not in sched._execute_native_action.call_args.args[1]
        with pytest.raises(PolicyViolationError):
            await sched.query_farm_space(region={**region, "width": 0})
        wrapper = MagicMock()
        wrapper.query_farm_space = AsyncMock(side_effect=sched.query_farm_space)
        server = create_mcp_server(scheduler=wrapper, surface="light")
        _, result = await server.call_tool("observe_farm_space", {"region": region})
        assert result == native
        with pytest.raises(ToolError):
            await server.call_tool("observe_farm_space", {"region": {**region, "x": True}})
    asyncio.run(run())


def test_player_work_log_projects_bounded_native_records_into_wire(tmp_path):
    from stardew_ai_runtime.chat_bridge import ChatBridge
    from stardew_ai_runtime.protocol import Envelope
    from stardew_ai_runtime.work_state import ExecutionEntry, Task

    bridge = ChatBridge(run_dir=tmp_path)
    store = bridge._work_store
    def seed(state):
        state.tasks.append(Task(id="task", goal_id="g", title="铺设东侧道路"))
        for i in range(10):
            state.executions.append(ExecutionEntry(command_id=f"c{i}", task_id="task", step_id=f"s{i}",
                operation="place_items", outcome="completed", effects=[{"x": i, "y": 2}],
                game_date="1:spring:3" if i else None))
        state.executions.append(state.executions[-1])
    store._mutate("a", seed)
    work = bridge._life_work_projection("a")
    rows = work["recentExecutions"]
    assert len(rows) == 8
    assert [row["commandId"] for row in rows] == [f"c{i}" for i in range(2, 10)]
    assert rows[-1]["taskTitle"] == "铺设东侧道路"
    assert rows[-1]["gameDate"] == "1:spring:3"
    assert "任务继续中" in rows[-1]["summary"]
    assert len(rows[-1]["summary"]) <= 180
    assert bridge._life_work_projection("another-save")["recentExecutions"] == []
    envelope = Envelope.create_life_profile_state(bridge.instance_id, "request", "a", {}, 0, work=work)
    assert envelope.to_mapping()["payload"]["work"]["recentExecutions"] == rows


def test_notice_transport_failure_keeps_pending_and_success_has_distinct_status(tmp_path):
    from unittest.mock import MagicMock

    from stardew_ai_runtime.chat_bridge import ChatBridge
    from stardew_ai_runtime.protocol import Envelope
    bridge = ChatBridge(run_dir=tmp_path)
    goal = bridge._work_store.add_goal("a", "Layout")
    notice = bridge._work_store.request_player_decision("a", goal.id, "请选一个入口")
    snapshot = Envelope.from_mapping({"protocolVersion": "0.1", "messageType": "world.snapshot", "messageId": "m",
        "senderInstanceId": "mod", "sequenceNumber": 1, "worldRevision": 1,
        "sentAt": "2026-01-01T00:00:00Z", "saveId": "a", "gameSessionId": "g", "payload": {}})
    async def run():
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=OSError("offline"))
        await bridge._maybe_schedule_autonomy(snapshot, "a", set(), ws, force=True)
        assert bridge._work_store.pending_player_notice("a")["id"] == notice["id"]
        ws.send_text = AsyncMock()
        await bridge._maybe_schedule_autonomy(snapshot, "a", set(), ws, force=True)
        import json
        assert json.loads(ws.send_text.call_args.args[0])["payload"]["status"] == "player-decision"
        assert bridge._work_store.pending_player_notice("a") is None
    asyncio.run(run())


def test_standby_project_notes_do_not_wake_loop_but_changed_goal_does(tmp_path):
    from stardew_ai_runtime.chat_bridge import ChatBridge
    from stardew_ai_runtime.protocol import Envelope

    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._autonomy.set_enabled("a", True)
    goal = bridge._work_store.add_goal("a", "Layout", source="user")
    bridge.handle_chat_submit = AsyncMock()
    snapshot = Envelope.from_mapping({"protocolVersion": "0.1", "messageType": "world.snapshot", "messageId": "m",
        "senderInstanceId": "mod", "sequenceNumber": 1, "worldRevision": 1,
        "sentAt": "2026-01-01T00:00:00Z", "saveId": "a", "gameSessionId": "g",
        "payload": {"world": {"year": 1, "season": "spring", "dayOfMonth": 2}}})
    async def run():
        tracked = set()
        await bridge._maybe_schedule_autonomy(snapshot, "a", tracked, None, force=True)
        await asyncio.gather(*tracked)
        assert bridge.handle_chat_submit.await_count == 1
        bridge._work_store.revise_goal("a", goal.id, project={"phase": "waiting", "summary": "Need materials"})
        await bridge._maybe_schedule_autonomy(snapshot, "a", tracked, None, force=True)
        await asyncio.gather(*tracked)
        assert bridge.handle_chat_submit.await_count == 1
        bridge._work_store.revise_goal("a", goal.id, text="Prioritize the east entrance")
        await bridge._maybe_schedule_autonomy(snapshot, "a", tracked, None, force=True)
        await asyncio.gather(*tracked)
        assert bridge.handle_chat_submit.await_count == 2
    asyncio.run(run())


def test_worker_releases_native_owner_after_terminal_before_external_observation(tmp_path):
    from stardew_ai_runtime.chat_bridge import PlanWorker

    store = WorkStore(tmp_path / "work.json")
    store.begin_decision("a", "decision")
    store.submit_plan("a", goal_text="Layout", decision_token="decision", tasks=[{
        "title": "Path", "steps": [{"operation": "place_items", "params": {"item_id": "(O)328", "tiles": [{"x": 1, "y": 2}]}}]}])
    class SingleOwner:
        def __init__(self):
            self.connected = False
            self.close_count = 0
            self.external_observations = 0
        async def call_tool(self, name, arguments):
            self.connected = True
            return {"terminalState": "succeeded", "completedCount": 1, "effects": [{"x": 1, "y": 2}], "commandId": arguments["command_id"]}
        async def close(self):
            assert store.state("a").tasks[0].status == "completed"
            self.connected = False
            self.close_count += 1
        def external_observe(self):
            if self.connected:
                raise ConnectionError("HTTP 409: active socket exists")
            self.external_observations += 1
    async def run():
        client = SingleOwner()
        worker = PlanWorker(store, client)
        worker.supplied_save_id = "a"
        await worker.evaluate()
        client.external_observe()
        assert client.external_observations == 1 and client.close_count == 1
        assert store.execution_log("a")[0]["outcome"] == "completed"
    asyncio.run(run())


def test_settled_partial_can_be_replanned_without_erasing_native_effects(tmp_path):
    from stardew_ai_runtime.work_state import WorkStateError
    store = WorkStore(tmp_path / "work.json")
    store.begin_decision("a", "first")
    tasks = [{"title": "Place", "steps": [{"operation": "place_items", "params": {"tiles": [{"x": 1, "y": 2}], "item_id": "(O)328"}}]}]
    job = store.submit_plan("a", goal_text="Layout", decision_token="first", tasks=tasks)
    task, step = job["tasks"][0], job["tasks"][0]["steps"][0]
    store.claim_next_step("a", "worker")
    store.assign_command_id("a", task["id"], step["id"], "native")
    store.commit_step_result("a", task_id=task["id"], step_id=step["id"], command_id="native", outcome="partial", effects=[{"x": 1, "y": 2}])
    with pytest.raises(WorkStateError, match="GAME_BUSY"):
        store.begin_decision("a", "too-early", require_idle=True)
    store.finish_job("a", {"outcome": "partial"}, task_id=task["id"])
    store.begin_decision("a", "second", require_idle=True)
    store.submit_plan("a", goal_id=job["goalId"], decision_token="second", tasks=tasks, replace=True)
    old = store.state("a").tasks[0]
    assert old.status == "cancelled" and old.steps[0].outcome == "partial"
    assert store.execution_log("a")[0]["effects"] == [{"x": 1, "y": 2}]


def test_closed_internal_transport_is_bounded_wait_with_nonempty_diagnostic(tmp_path):
    from anyio import ClosedResourceError

    from stardew_ai_runtime.plan_executor import PlanExecutor
    store = WorkStore(tmp_path / "work.json")
    store.begin_decision("a", "first")
    store.submit_plan("a", goal_text="Layout", decision_token="first", tasks=[{
        "title": "Place", "steps": [{"operation": "place_items", "params": {"tiles": [{"x": 1, "y": 2}], "item_id": "(O)328"}}]}])
    async def dispatch(*args):
        raise ClosedResourceError()
    result = asyncio.run(PlanExecutor(store, dispatch).run_once("a", "worker"))
    assert result.outcome == "waiting"
    assert result.reason_code == "TRANSPORT_RETRY_PENDING"
    assert result.message == "ClosedResourceError"
