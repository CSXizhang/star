import json
from dataclasses import asdict
from pathlib import Path

from stardew_ai_runtime.decision_context import build_decision_context
from stardew_ai_runtime.work_state import ExecutionEntry, SaveWorkState, Step, Task, WorkStore


def effect(kind, x, y=25, **extra):
    return {"state": kind, "tile": {"x": x, "y": y}, **extra}


def test_committed_map_provenance_survives_archive_pruning_and_counts_all_batches(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    for index in range(10):
        identity = f"batch-{index}"
        native_task = Task(identity, "goal", identity, steps=[
            Step("plant", "plant_seeds", {"location_id": "Farm"},
                 status="running", command_id=f"plant-{index}"),
            Step("water", "water_zone", status="running", command_id=f"water-{index}"),
        ])
        store._mutate("save", lambda state, task=native_task: state.tasks.append(task))
        store.commit_step_result("save", task_id=identity, step_id="plant", outcome="completed",
                                 command_id=f"plant-{index}",
                                 effects=[effect("planted", index, itemId="seed")])
        store.commit_step_result("save", task_id=identity, step_id="water", outcome="completed",
                                 command_id=f"water-{index}", effects=[effect("watered", index)])
    def prune(state):
        state.tasks.clear()
        state.archive.clear()
    store._mutate("save", prune)
    loaded = WorkStore(store.state_path).state("save")
    assert all(e.location_id == "Farm" for e in loaded.executions)
    projection = WorkStore._planting_evidence(loaded)
    assert projection["confirmedWateredCount"] == 10
    assert projection["pendingCount"] == 0
    assert len(projection["recentBatches"]) == 8
    assert "ledgerConfirmed" in projection["countProvenance"]


def test_entry_maps_unknown_foreign_duplicate_and_legacy_json_are_safe(tmp_path):
    plant = entry("plant", "plant_seeds", [effect("planted", 1, itemId="seed")])
    plant.location_id = "Farm"
    foreign = entry("foreign", "water_zone", [effect("watered", 1)])
    foreign.location_id = "Greenhouse"
    unknown = entry("unknown", "water_zone", [effect("watered", 1)], outcome="unknown")
    unknown.location_id = "Farm"
    legacy = entry("legacy", "water_zone", [effect("watered", 1)])
    skipped = entry("skip", "water_zone", [effect("skipped", 1)])
    skipped.location_id = "Farm"
    state = SaveWorkState(executions=[plant, plant, foreign, unknown, legacy, skipped])
    path = tmp_path / "work.json"
    rows = [asdict(e) for e in state.executions]
    rows[4].pop("location_id")  # Old JSON schema remains loadable and mapless.
    path.write_text(json.dumps({"save": {"executions": rows}}), encoding="utf-8")
    loaded = WorkStore(path).state("save")
    assert loaded.executions[4].location_id is None
    assert WorkStore._planting_evidence(loaded)["confirmedWateredCount"] == 0
    water = entry("water", "water_zone", [effect("watered", 1, locationId="Farm")])
    water.location_id = "Greenhouse"  # Native per-effect map takes precedence.
    loaded.executions.extend([water, water, plant])
    result = WorkStore._planting_evidence(loaded)
    assert result["confirmedWateredCount"] == 1 and result["pendingCount"] == 0
    unmapped = task("new", "custom_action")
    assert WorkStore._execution_location(loaded, unmapped, unmapped.steps[0], "completed", []) is None
    assert WorkStore._execution_location(loaded, unmapped, unmapped.steps[0], "unknown",
                                         [effect("watered", 1, locationId="Farm")]) is None
    nav = entry("new", "navigate_to", [{"location": "Farm"}])
    loaded.executions.append(nav)
    assert WorkStore._execution_location(loaded, unmapped, unmapped.steps[0], "completed", []) == "Farm"
    loaded.executions.append(entry("new", "navigate_to", [], outcome="unknown", command="uncertain"))
    assert WorkStore._execution_location(loaded, unmapped, unmapped.steps[0], "completed", []) is None


def test_legacy_same_day_navigation_anchor_expires_on_unknown_navigation_and_rollover():
    nav = entry("nav", "navigate_to", [{"location": "Farm"}])
    nav.game_date = "1:spring:18"
    plant = entry("plant", "plant_seeds", [effect("planted", 1, itemId="seed")])
    plant.game_date = nav.game_date
    water = entry("water", "water_zone", [effect("watered", 1)])
    water.game_date = nav.game_date
    state = SaveWorkState(executions=[nav, plant, water])
    assert WorkStore._planting_evidence(state)["confirmedWateredCount"] == 1
    step_task = task("fresh", "custom_action")
    assert WorkStore._execution_location(state, step_task, step_task.steps[0], "completed", [], nav.game_date) == "Farm"
    uncertain = entry("uncertain", "navigate_to", [], outcome="unknown")
    uncertain.game_date = nav.game_date
    state.executions = [nav, plant, uncertain, water]
    assert WorkStore._planting_evidence(state)["confirmedWateredCount"] == 0
    assert WorkStore._execution_location(state, step_task, step_task.steps[0], "completed", [], nav.game_date) is None
    water.game_date = "1:spring:19"
    state.executions = [nav, plant, water]
    assert WorkStore._planting_evidence(state)["confirmedWateredCount"] == 0
    assert WorkStore._execution_location(state, step_task, step_task.steps[0], "completed", [], water.game_date) is None


def test_verified_dispatch_default_needs_real_step_and_does_not_default_water_auto():
    plant = entry("plant", "plant_seeds", [effect("planted", 1, itemId="seed")])
    water = entry("water", "water_zone", [effect("watered", 1)])
    state = SaveWorkState(executions=[plant, water])
    assert WorkStore._planting_evidence(state)["confirmedWateredCount"] == 0
    state.tasks = [task("plant", "plant_seeds"), task("water", "water_zone")]
    assert WorkStore._planting_evidence(state)["confirmedWateredCount"] == 1
    state.tasks[1].steps[0].params["location_id"] = "Greenhouse"
    assert WorkStore._planting_evidence(state)["confirmedWateredCount"] == 0
    state.tasks[1] = task("water", "water_auto")
    water.operation = "water_auto"
    assert WorkStore._planting_evidence(state)["confirmedWateredCount"] == 0
    assert WorkStore._step_location("plant_seeds", {"location_id": None}) is None


def task(identity, operation, *, location=None):
    params = {"location_id": location} if location else {}
    return Task(identity, "goal", identity, steps=[Step("step", operation, params)])


def entry(identity, operation, effects, *, outcome="completed", command=None):
    return ExecutionEntry(command or identity, identity, "step", operation, outcome, effects)


def test_archived_batch_retains_exact_missing_water_tiles_in_compact_context(tmp_path: Path):
    planted = [effect("planted", x, y, itemId="(O)483")
               for y in range(25, 30) for x in range(45, 48) if (x, y) != (47, 28)]
    planting = task("original", "plant_seeds", location="Farm")
    navigation = task("later", "navigate_to")
    navigation.steps.append(Step("water", "water_zone"))
    nav = entry("later", "navigate_to", [{"location": "Farm"}])
    watered = ExecutionEntry("water-command", "later", "water", "water_zone", "completed",
                             [effect("watered", 45, 25), effect("watered", 45, 26),
                              effect("watered", 43, 24)])
    state = SaveWorkState(tasks=[navigation], archive=[{"task": asdict(planting)}],
                          executions=[entry("original", "plant_seeds", planted), nav, watered])
    store = WorkStore(tmp_path / "work.json")
    store._mutate("save", lambda s: s.__dict__.update(state.__dict__))
    before = store.state_path.read_bytes()
    overview = store.overview("save")
    projection = overview["plantingEvidence"]
    assert projection["pendingCount"] == 12
    batch = projection["batches"][0]
    assert batch["taskId"] == "original"
    assert batch["locationId"] == "Farm"
    assert batch["seedItemId"] == "(O)483"
    assert batch["pendingCount"] == 12
    assert batch["originalPlantedCount"] == 14
    assert batch["confirmedWateredCount"] == 2
    assert batch["removedBeforeWaterCount"] == 0
    assert {tuple(t.values()) for t in batch["tiles"]} == {
        (e["tile"]["x"], e["tile"]["y"]) for e in planted
    } - {(45, 25), (45, 26)}
    for snapshot in ({}, {"world": {"isRaining": True}}):
        context = build_decision_context(snapshot, work=overview)
        assert context["plantingEvidence"] == projection
    assert store.overview("save")["plantingEvidence"] == projection
    assert store.state_path.read_bytes() == before


def test_only_later_confirmed_same_map_water_pairs_and_duplicate_results_are_idempotent():
    plant = entry("plant", "plant_seeds", [effect("planted", 1, itemId="seed")])
    state = SaveWorkState(tasks=[task("plant", "plant_seeds", location="Farm"),
                                task("water", "water_zone", location="Farm"),
                                task("foreign", "water_zone", location="Greenhouse")],
                          executions=[entry("water", "water_zone", [effect("watered", 1)]),
                                      plant, plant,
                                      entry("foreign", "water_zone", [effect("watered", 1)]),
                                      entry("unmapped", "water_zone", [effect("watered", 1)]),
                                      entry("water", "water_zone", [effect("watered", 1)],
                                            outcome="unknown", command="unknown"),
                                      entry("water", "water_zone", [effect("skipped", 1)],
                                            command="skipped")])
    assert WorkStore._planting_evidence(state)["pendingCount"] == 1
    counts = WorkStore._planting_evidence(state)["batches"][0]
    assert counts["originalPlantedCount"] == 1
    assert counts["confirmedWateredCount"] == 0
    assert counts["removedBeforeWaterCount"] == 0
    state.executions.append(entry("water", "water_zone", [effect("watered", 1)], command="actual"))
    assert WorkStore._planting_evidence(state)["pendingCount"] == 0
    # Replayed original result cannot recreate a crop after it was paired.
    state.executions.append(plant)
    assert WorkStore._planting_evidence(state)["pendingCount"] == 0
    recent = WorkStore._planting_evidence(state)["recentBatches"]
    assert recent == [{"taskId": "plant", "seedItemId": "seed", "locationId": "Farm",
                       "originalPlantedCount": 1, "confirmedWateredCount": 1,
                       "removedBeforeWaterCount": 0, "pendingCount": 0}]


def test_harvest_and_native_crop_removal_retire_pending_tiles_and_replant_starts_new_batch():
    state = SaveWorkState(tasks=[task("plant", "plant_seeds", location="Farm"),
                                task("remove", "cut_grass", location="Farm")],
                          executions=[entry("plant", "plant_seeds", [
                              effect("planted", 1, itemId="seed"),
                              effect("planted", 2, itemId="seed"),
                              effect("skipped", 3, itemId="seed")]),
                                      entry("remove", "cut_grass", [
                                          effect("cleared-dead-crop", 1), effect("harvested", 2)])])
    assert WorkStore._planting_evidence(state)["pendingCount"] == 0
    state.executions.append(entry("plant", "plant_seeds", [effect("planted", 1, itemId="new-seed")],
                                  command="replant"))
    assert WorkStore._planting_evidence(state)["batches"][0]["seedItemId"] == "new-seed"


def test_projection_caps_coordinates_without_losing_counts_or_modifying_ledger():
    state = SaveWorkState(tasks=[task("plant", "plant_seeds", location="Farm")],
                          executions=[entry("plant", "plant_seeds", [
                              effect("planted", x, itemId="seed") for x in range(70)])])
    original = asdict(state)
    projection = WorkStore._planting_evidence(state)
    assert projection["pendingCount"] == 70
    assert projection["truncated"] is True
    assert projection["batches"][0]["pendingCount"] == 70
    assert projection["batches"][0]["originalPlantedCount"] == 70
    assert projection["batches"][0]["truncated"] is True
    assert len(projection["batches"][0]["tiles"]) == 64
    assert asdict(state) == original


def test_batch_counters_use_actual_lifecycle_matches_not_pending_subtraction():
    planted = entry("plant", "plant_seeds", [
        effect("planted", x, itemId="seed") for x in range(6)
    ] + [effect("planted", 5, itemId="seed")])
    native = entry("change", "cut_grass", [
        effect("watered", 0), effect("watered", 0),
        effect("harvested", 0),  # Already watered: not removed-before-water.
        effect("cleared-dead-crop", 1), effect("cleared-dead-crop", 1),
        effect("harvested", 2), effect("watered", 2),  # Removed crop cannot pair.
        effect("skipped", 3), effect("watered", 99),
    ])
    state = SaveWorkState(tasks=[task("plant", "plant_seeds", location="Farm"),
                                task("change", "cut_grass", location="Farm")],
                          executions=[planted, native, native, planted])
    batch = WorkStore._planting_evidence(state)["batches"][0]
    assert batch["originalPlantedCount"] == 6
    assert batch["confirmedWateredCount"] == 1
    assert batch["removedBeforeWaterCount"] == 2
    assert batch["pendingCount"] == 3


def test_recent_completed_batch_summaries_are_bounded_and_ranked_by_actual_update():
    state = SaveWorkState()
    for index in range(10):
        identity = f"plant-{index}"
        state.tasks.append(task(identity, "plant_seeds", location="Farm"))
        state.executions.extend([
            entry(identity, "plant_seeds", [effect("planted", index, itemId="seed")]),
            entry(identity, "water_zone", [effect("watered", index)], command=f"water-{index}"),
        ])
    projection = WorkStore._planting_evidence(state)
    assert projection["batches"] == []
    assert projection["pendingCount"] == 0
    assert projection["recentBatchesTruncated"] is True
    recent = projection["recentBatches"]
    assert len(recent) == 8
    assert [b["taskId"] for b in recent] == [f"plant-{i}" for i in range(9, 1, -1)]
    assert all(b["originalPlantedCount"] == b["confirmedWateredCount"] == 1 for b in recent)
    assert all("tiles" not in b and b["pendingCount"] == 0 for b in recent)
    state.executions.append(entry("plant-0", "plant_seeds", [effect("planted", 0, itemId="seed")],
                                  command="replant-old-batch"))
    recent = WorkStore._planting_evidence(state)["recentBatches"]
    assert recent[0]["taskId"] == "plant-0"
    assert recent[0]["originalPlantedCount"] == 2
    assert recent[0]["confirmedWateredCount"] == 1
    assert recent[0]["pendingCount"] == 1
