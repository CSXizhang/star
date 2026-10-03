"""Recovery uses the C# receipt shape and never replays unconfirmed actions."""
import asyncio
import time

import pytest

from stardew_ai_runtime.chat_bridge import PlanWorker
from stardew_ai_runtime.job_feedback import compact_job_feedback
from stardew_ai_runtime.work_state import WorkStore


def _tiles(count=100):
    return [{"x": x, "y": 20} for x in range(count)]


def _select(store, steps, goal_id=None):
    store.begin_decision("farm", "decision")
    selected = store.submit_plan("farm", goal_text="种下并浇水", tasks=[{
        "title": "播种后浇水", "steps": steps,
    }], decision_token="decision", goal_id=goal_id)
    return selected["tasks"][0]["id"]


def _raw_water_receipt(command_id, *, completed=40, terminal="partially-succeeded"):
    receipt = {
        "commandId": command_id, "terminalState": terminal,
        "completedCount": completed, "skippedCount": 0, "failedCount": 0,
        "finalWorldRevision": 77,
        "resources": {"staminaUsed": completed * 2, "waterUsed": completed,
                      "gameMinutesElapsed": 20},
        "effects": [{"state": "watered", "tile": tile} for tile in _tiles(completed)],
    }
    if terminal == "partially-succeeded":
        receipt["error"] = {"code": "OUT_OF_WATER", "message": "The watering can ran empty."}
    return receipt


def test_raw_receipt_preserves_native_error_revision_and_explicit_remaining():
    raw = _raw_water_receipt("water")
    tiles = _tiles()
    feedback = compact_job_feedback(raw, operation="water_tiles", status="partial",
                                    params={"tiles": [*tiles, tiles[0]]})
    assert "targetCount" not in raw
    assert feedback["targetCount"] == 100
    assert feedback["targetCountSource"] == "explicitStepTiles"
    assert feedback["unprocessedTargetCount"] == 60
    assert feedback["unprocessedTargetCountSource"] == "nativeCounters"
    assert feedback["finalWorldRevision"] == 77
    assert feedback["reasonCode"] == "OUT_OF_WATER"
    assert feedback["resources"] == raw["resources"]
    assert feedback["actualSummary"] == "已浇水 40 格"
    assert "还有 60 个目标未处理" in feedback["progressSummary"]
    # WaterZoneModels does not emit skip reasons; the harness must not invent them.
    assert "skipReasons" not in feedback


def test_timeout_placeholder_zero_counters_do_not_prove_unprocessed_targets():
    # Scheduler timeout placeholders can outlive a command that actually did work.
    raw = {"commandId": "unconfirmed", "status": "unknown", "terminalState": "unknown",
           "completedCount": 0, "skippedCount": 0, "failedCount": 0, "effects": [],
           "reasonCode": "NATIVE_TERMINAL_UNCONFIRMED"}
    feedback = compact_job_feedback(raw, operation="water_tiles", status="unknown",
                                    params={"tiles": _tiles()})
    assert feedback["actualSummary"] == "未确认实际变化"
    assert "unprocessedTargetCount" not in feedback
    assert "还有 100 个目标未处理" not in feedback["progressSummary"]


@pytest.mark.parametrize("terminal,completed,steps_completed", [
    ("partially-succeeded", 40, 1), ("succeeded", 100, 2),
])
def test_expired_recovery_reloads_task_and_keeps_prior_and_recovered_facts(
    tmp_path, terminal, completed, steps_completed,
):
    async def scenario():
        store = WorkStore(tmp_path / "work.json")
        task_id = _select(store, [
            {"operation": "plant_seeds", "params": {"seed_item_id": "(O)475", "tiles": _tiles()}},
            {"operation": "water_tiles", "params": {"tiles": _tiles()}},
        ])
        planting = store.claim_next_step("farm", "prior-worker")
        store.assign_command_id("farm", task_id, planting["stepId"], "plant-command")
        planted = {"commandId": "plant-command", "terminalState": "succeeded",
                   "completedCount": 100, "skippedCount": 0, "failedCount": 0,
                   "effects": [{"state": "planted", "tile": tile} for tile in _tiles()]}
        store.commit_step_result(
            "farm", task_id=task_id, step_id=planting["stepId"], command_id="plant-command",
            outcome="completed", effects=planted["effects"],
            feedback=compact_job_feedback(planted, operation="plant_seeds", status="completed",
                                          params={"tiles": _tiles()}),
        )
        watering = store.claim_next_step("farm", "expired-worker", now=time.monotonic() - 10,
                                         lease_seconds=1)
        store.assign_command_id("farm", task_id, watering["stepId"], "water-command")
        raw = _raw_water_receipt("water-command", completed=completed, terminal=terminal)
        calls = []

        class Client:
            async def current_save_id(self):
                return "farm"

            async def reconcile(self, command_id):
                calls.append(("reconcile", command_id))
                return {"found": True, "native": raw}

            async def call_tool(self, name, arguments):
                raise AssertionError("Recovery must never dispatch another native command")

        worker = PlanWorker(store, Client())
        await worker.evaluate()
        await worker.evaluate()
        restored = WorkStore(store.state_path).state("farm")
        task = next(task for task in restored.tasks if task.id == task_id)
        feedback = restored.last_job
        assert calls == [("reconcile", "water-command")]
        assert len(restored.executions) == 2
        assert restored.executions[-1].snapshot_revision == 77
        assert task.steps[0].effects == planted["effects"]
        assert task.steps[1].effects == raw["effects"]
        assert task.steps[1].feedback["targetCount"] == 100
        assert task.steps[1].feedback["unprocessedTargetCount"] == 100 - completed
        assert feedback["stepsCompleted"] == steps_completed
        assert feedback["effectCount"] == 100 + completed
        assert feedback["actualSummary"] == f"已种下 100 格，已浇水 {completed} 格"
        assert [row["completedCount"] for row in feedback["stepResults"]] == [100, completed]
        if terminal == "partially-succeeded":
            assert feedback["status"] == "partial"
            assert feedback["reasonCode"] == "OUT_OF_WATER"
            assert len(feedback["remainingSteps"]) == 1
            assert "还有 60 个目标未处理" in feedback["progressSummary"]
        else:
            assert feedback["status"] == "completed"
            assert feedback["remainingSteps"] == []
    asyncio.run(scenario())


@pytest.mark.parametrize("scope_tiles", [[], _tiles(200)])
def test_auto_dispatch_binds_full_saved_scope_without_changing_atomic_plan(tmp_path, scope_tiles):
    async def scenario():
        store = WorkStore(tmp_path / "work.json")
        goal = store.add_goal("farm", "指定格子浇水", constraints={
            "objectiveScope": {"executionScope": {"locationId": "Farm", "tiles": scope_tiles}},
        })
        task_id = _select(store, [{"operation": "water_auto", "params": {"max_tiles": 200}}], goal.id)
        dispatched = []

        class Client:
            async def current_save_id(self):
                return "farm"

            async def call_tool(self, name, arguments):
                dispatched.append(arguments)
                return {"commandId": arguments["command_id"], "status": "completed",
                        "terminalState": "succeeded", "effects": [],
                        "targetCount": 0, "completedCount": 0, "skippedCount": 0, "failedCount": 0}

        worker = PlanWorker(store, Client())
        await worker.evaluate()
        await worker.evaluate()
        assert len(dispatched) == 1
        assert dispatched[0]["params"] == {"max_tiles": 200, "location_id": "Farm", "target_tiles": scope_tiles}
        task = next(task for task in store.state("farm").tasks if task.id == task_id)
        assert task.steps[0].params == {"max_tiles": 200}
    asyncio.run(scenario())


def test_expired_unknown_has_no_effects_or_invented_native_counts(tmp_path):
    async def scenario():
        store = WorkStore(tmp_path / "work.json")
        task_id = _select(store, [{"operation": "water_tiles", "params": {"tiles": _tiles()}}])
        claim = store.claim_next_step("farm", "expired-worker", now=time.monotonic() - 10,
                                     lease_seconds=1)
        store.assign_command_id("farm", task_id, claim["stepId"], "unconfirmed")
        calls = []

        class Client:
            async def current_save_id(self):
                return "farm"

            async def reconcile(self, command_id):
                calls.append(command_id)
                return None

            async def call_tool(self, name, arguments):
                raise AssertionError("Unknown commands must never be replayed")

        worker = PlanWorker(store, Client())
        await worker.evaluate()
        await worker.evaluate()
        restored = WorkStore(store.state_path).state("farm")
        feedback = restored.last_job
        assert calls == ["unconfirmed"]
        assert restored.tasks[0].steps[0].status == "unknown"
        assert restored.tasks[0].steps[0].effects == []
        assert restored.executions == []
        assert feedback["status"] == "unknown"
        assert feedback["actualSummary"] == "未确认实际变化"
        assert feedback["stepsCompleted"] == 0
        assert not any(key in feedback for key in (
            "completedCount", "skippedCount", "failedCount", "unprocessedTargetCount",
        ))
    asyncio.run(scenario())


def test_cross_map_navigation_failure_stops_planting_and_watering(tmp_path):
    async def scenario():
        store = WorkStore(tmp_path / "work.json")
        _select(store, [
            {"operation": "navigate_to", "params": {"location_id": "FarmHouse", "tile": {"x": 1, "y": 1}}},
            {"operation": "withdraw_from_chest", "params": {"location_id": "FarmHouse",
             "chest_x": 1, "chest_y": 1, "items": [{"itemId": "(O)475", "count": 100}]}},
            {"operation": "navigate_to", "params": {"location_id": "Farm", "tile": {"x": 10, "y": 10}}},
            {"operation": "plant_seeds", "params": {"location_id": "Farm", "seed_item_id": "(O)475", "tiles": _tiles()}},
            {"operation": "water_tiles", "params": {"location_id": "Farm", "tiles": _tiles()}},
        ])
        calls = []

        class Client:
            async def current_save_id(self):
                return "farm"

            async def call_tool(self, name, arguments):
                operation, params = arguments["operation"], arguments["params"]
                calls.append((operation, params["location_id"]))
                if operation == "navigate_to" and params["location_id"] == "Farm":
                    return {"commandId": arguments["command_id"], "terminalState": "failed",
                            "completedCount": 0, "skippedCount": 0, "failedCount": 1,
                            "effects": [], "finalWorldRevision": 42,
                            "error": {"code": "NO_ROUTE", "message": "No route to Farm."}}
                effects = ([{"location": "FarmHouse", "pathLength": 5}]
                           if operation == "navigate_to" else [{"state": "withdrawn", "stack": 100}])
                return {"commandId": arguments["command_id"], "terminalState": "succeeded",
                        "completedCount": 1, "skippedCount": 0, "failedCount": 0, "effects": effects}

        worker = PlanWorker(store, Client())
        await worker.evaluate()
        await worker.evaluate()
        assert calls == [("navigate_to", "FarmHouse"), ("withdraw_from_chest", "FarmHouse"),
                         ("navigate_to", "Farm")]
        restored = WorkStore(store.state_path).state("farm")
        assert [step.status for step in restored.tasks[0].steps] == [
            "completed", "completed", "partial", "pending", "pending",
        ]
        feedback = restored.last_job
        assert feedback["status"] == "partial" and feedback["reasonCode"] == "NO_ROUTE"
        assert feedback["stepsCompleted"] == 2
        assert "取出 100" in feedback["actualSummary"]
        assert "已种下" not in feedback["actualSummary"] and "已浇水" not in feedback["actualSummary"]
    asyncio.run(scenario())


@pytest.mark.parametrize("initial_running", [False, True])
def test_normal_dispatch_commits_terminal_revision_not_initial_snapshot(tmp_path, initial_running):
    async def scenario():
        store = WorkStore(tmp_path / "work.json")
        _select(store, [{"operation": "water_tiles", "params": {"tiles": _tiles(1)}}])
        calls = []

        class Client:
            async def current_save_id(self):
                return "farm"

            async def call_tool(self, name, arguments):
                calls.append(("dispatch", arguments["command_id"]))
                if initial_running:
                    return {"status": "running", "commandId": arguments["command_id"],
                            "snapshotRevision": 3}
                return {**_raw_water_receipt(arguments["command_id"], completed=1, terminal="succeeded"),
                        "worldRevision": 4, "snapshotRevision": 3}

            async def reconcile(self, command_id):
                calls.append(("reconcile", command_id))
                return {"found": True, "native": _raw_water_receipt(
                    command_id, completed=1, terminal="succeeded",
                )}

        worker = PlanWorker(store, Client())
        await worker.evaluate()
        await worker.evaluate()
        restored = WorkStore(store.state_path).state("farm")
        assert len(restored.executions) == 1
        assert restored.executions[0].snapshot_revision == 77
        assert restored.tasks[0].steps[0].feedback["finalWorldRevision"] == 77
        assert [kind for kind, _ in calls] == (["dispatch", "reconcile"] if initial_running else ["dispatch"])
        assert len({command_id for _, command_id in calls}) == 1
    asyncio.run(scenario())
