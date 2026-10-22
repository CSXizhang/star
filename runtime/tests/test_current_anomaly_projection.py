from dataclasses import asdict

from stardew_ai_runtime.work_state import ExecutionEntry, Step, Task, WorkStore


def test_current_projection_omits_settled_history_and_orders_native_results(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("Save", "grow crops", source="user")

    def seed(state):
        for index, status in enumerate(["cancelled", "completed", "partial", "partial", "unknown"]):
            task = Task(id=f"t{index}", goal_id=goal.id, title=str(index), status=status,
                        created_at="2026-09-30T01:00:00+00:00",
                        updated_at="2026-09-30T23:00:00+00:00")
            step_status = "unknown" if index == 4 else "failed"
            outcome = "unknown" if index == 4 else "failed"
            task.steps = [Step(id=f"s{index}", operation="navigate_to", status=step_status,
                               command_id=f"c{index}", outcome=outcome)]
            state.tasks.append(task)
            state.executions.append(ExecutionEntry(command_id=f"c{index}", task_id=task.id,
                step_id=f"s{index}", operation="navigate_to", outcome=outcome,
                at=f"2026-09-30T0{index + 1}:00:00+00:00"))
        # t2's advisory edit is newer than t3, but its actual native result isn't.
        state.tasks[2].updated_at = "2026-09-30T23:59:00+00:00"

    store._mutate("Save", seed)
    before = asdict(store.state("Save"))
    assert [row["taskId"] for row in store.overview("Save")["anomalies"]] == ["t4", "t3", "t2"]
    assert asdict(store.state("Save")) == before


def test_cancel_preserves_unconfirmed_command_and_legacy_uncertainty(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("Save", "grow crops", source="user")

    def seed(state):
        state.tasks.extend([
            Task(id="live", goal_id=goal.id, title="live", status="running", steps=[
                Step(id="live-step", operation="navigate_to", status="running", command_id="live-command")]),
            Task(id="legacy", goal_id=goal.id, title="legacy", status="cancelled", steps=[
                Step(id="legacy-step", operation="navigate_to", status="cancelled", command_id="legacy-command")]),
        ])

    store._mutate("Save", seed)
    store.revise_goal("Save", goal.id, status="cancelled")
    state = store.state("Save")
    assert state.tasks[0].status == "cancelled"
    assert state.tasks[0].steps[0].status == "unknown"
    assert state.tasks[0].steps[0].command_id == "live-command"
    rows = store.overview("Save")["anomalies"]
    assert {r["commandId"] for r in rows} == {"live-command", "legacy-command"}
    assert all(r["status"] == "unknown" for r in rows)
    assert store.claim_next_step("Save", "worker") is None
    store.recover("Save")
    assert store.state("Save").tasks[1].steps[0].status == "unknown"
    # A real terminal callback can still reconcile the preserved command.
    store.commit_step_result("Save", task_id="live", step_id="live-step", outcome="completed",
                             command_id="live-command")
    assert [r["commandId"] for r in store.overview("Save")["anomalies"]] == ["legacy-command"]
