from stardew_ai_runtime.job_feedback import compact_job_feedback
from stardew_ai_runtime.work_state import ExecutionEntry, WorkStore


def test_confirmed_navigation_does_not_complete_unknown_pickup_job(tmp_path):
    from stardew_ai_runtime.work_state import Step, Task

    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("farm", "拾取积蛋")
    task = Task("pickup49", goal.id, "拾49枚积蛋", status="unknown", steps=[
        Step("nav", "navigate_to", {}, status="completed"),
        Step("pickup", "pickup_items", {}, status="unknown", command_id="unconfirmed"),
    ])
    store._mutate("farm", lambda state: (
        state.tasks.append(task),
        state.executions.append(ExecutionEntry("nav-confirmed", task.id, "nav", "navigate_to",
            "completed", [{"location": "Coop", "pathLength": 5}], game_date="1:summer:19")),
    ))
    row = store.recent_execution_summary("farm")[-1]
    assert row["outcome"] == "unknown"
    assert row["summary"].startswith("结果待核实")
    assert "已完成" not in row["summary"]
    assert store.state("farm").executions[0].outcome == "completed"


def test_purchase_job_projection_counts_all_commands_and_keeps_native_ledger(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("farm", "养十只鸡")
    entries = [ExecutionEntry(command_id=f"c{i}", task_id="buy", step_id=f"s{i}", operation="purchase_animal",
                                    outcome="completed", effects=[{"state": "animal-purchased", "stack": 1}],
                                    game_date="1:summer:4") for i in range(10)]
    store._mutate("farm", lambda state: state.executions.extend(entries))
    rows = store.recent_execution_summary("farm")
    assert len(rows) == 1 and "10 只" in rows[0]["summary"]
    assert len(store.state("farm").executions) == 10
    feedback = compact_job_feedback({"effects": [effect for e in entries for effect in e.effects]}, operation="purchase_animal", status="completed")
    assert len(feedback["effects"]) == 8
    assert feedback["effectCount"] == 10
    assert feedback["effectsTruncated"] is True
    assert feedback["actualSummary"] == "已买入 10 只动物"
    assert goal.status == "active"


def test_plant_then_water_result_reports_both_confirmed_counts():
    effects = [{"state": "planted"}] * 12 + [{"state": "watered"}] * 12
    assert WorkStore.effect_summary(effects) == "已种下 12 格，已浇水 12 格"


def test_failed_pet_receipt_never_becomes_an_actual_change():
    effects = [{"state": "failed", "reason": "FarmAnimal.pet did not register"}]
    feedback = compact_job_feedback({"effects": effects, "completedCount": 0,
                                     "failedCount": 1}, operation="pet_animal", status="partial")
    assert feedback["actualSummary"] == "未确认实际变化"
    assert feedback["effects"] == effects
    assert feedback["failedCount"] == 1
    assert WorkStore.effect_summary([{"state": "fed", "stack": 12}, *effects]) == "已放入 12 份饲料"
    assert WorkStore.effect_summary([{"state": "petted"}, *effects]) == "已抚摸 1 只动物"


def test_completed_job_invalidates_pre_execution_next_action(tmp_path):
    from stardew_ai_runtime.chat_bridge import ChatBridge
    from stardew_ai_runtime.work_state import Task

    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    store = bridge._work_store
    goal = store.add_goal("farm", "加工入箱")
    store.revise_goal("farm", goal.id, project={"nextAction": "收取两份蛋黄酱"})
    store._mutate("farm", lambda state: (
        setattr(state.goals[0], "updated_at", "2026-09-30T10:00:00+00:00"),
        state.tasks.append(Task(id="collect", goal_id=goal.id, title="收取两份蛋黄酱",
                                status="completed", updated_at="2026-09-30T10:01:00+00:00")),
        setattr(state, "last_job", {"taskId": "collect", "status": "completed"}),
    ))
    result = bridge._player_activity("farm")
    assert "收取两份蛋黄酱" in result["summary"]
    assert "收取两份蛋黄酱" not in result["nextStep"]
    store.revise_goal("farm", goal.id, project={"nextAction": "将成品放入既有箱子"})
    assert bridge._player_activity("farm")["nextStep"] == "将成品放入既有箱子"


def test_chain_prompt_keeps_full_batch_accounting_when_receipt_coordinates_are_truncated(tmp_path):
    import json

    from stardew_ai_runtime.chat_bridge import ChatBridge
    from stardew_ai_runtime.work_state import Step, Task

    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    store = bridge._work_store
    goal = store.add_goal("farm", "累计种下并随后浇水200颗")
    plants = [{"state": "planted", "tile": {"x": x, "y": 25}, "itemId": "seed"}
              for x in range(14)]
    waters = [{"state": "watered", "tile": {"x": x, "y": 25}} for x in range(14)]
    store._mutate("farm", lambda state: (
        state.tasks.extend([
            Task("plant", goal.id, "播种", steps=[Step("p", "plant_seeds", {"location_id": "Farm"})]),
            Task("water", goal.id, "补浇", steps=[Step("w", "water_zone", {"location_id": "Farm"})]),
        ]),
        state.executions.extend([
            ExecutionEntry("p-command", "plant", "p", "plant_seeds", "completed", plants),
            ExecutionEntry("w-command", "water", "w", "water_zone", "completed", waters),
        ]),
        setattr(state, "last_job", compact_job_feedback({"effects": waters}, status="completed")),
    ))
    for snapshot in (None, {"world": {"season": "summer", "dayOfMonth": 16}}):
        bridge._latest_snapshot_payload = snapshot
        prompt = bridge._format_chain_prompt("继续原目标", "farm")
        context = json.loads(prompt.split("实时上下文：", 1)[1].split("\n", 1)[0])
        evidence = context["plantingEvidence"]
        assert evidence == store.overview("farm")["plantingEvidence"]
        assert evidence["pendingCount"] == 0
        assert evidence["recentBatches"][0]["confirmedWateredCount"] == 14
        assert context["lastResult"]["effectCount"] == 14
        assert len(context["lastResult"]["effects"]) == 8
