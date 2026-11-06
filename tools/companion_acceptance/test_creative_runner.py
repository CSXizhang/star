import asyncio
import copy
import hashlib
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import creative_runner as monitor

CRITERIA = {"minPathTiles": 24, "minChests": 2, "minRestObjects": 1, "minLights": 2}


def test_closeups_choose_real_campfire_instead_of_repeating_workshop_brazier():
    farm = {"workArea": {"width": 80, "height": 65}, "layout": [
        {"x": 50, "y": 17, "categories": ["chest"], "origin": "agent-or-native", "itemId": "(BC)130"},
        {"x": 52, "y": 18, "categories": ["rest", "light"], "origin": "agent-or-native", "itemId": "(BC)144"},
        {"x": 70, "y": 36, "categories": ["rest", "light"], "origin": "agent-or-native", "itemId": "(BC)146"},
        {"x": 2, "y": 2, "categories": ["flower"], "origin": "baseline", "itemId": "(BC)108"},
    ]}
    original = copy.deepcopy(farm)
    regions = dict(monitor.detail_regions(farm))
    assert set(regions) == {"workshop", "rest"}
    rest = regions["rest"]
    assert rest["x"] <= 70 < rest["x"] + rest["width"]
    assert rest["y"] <= 36 < rest["y"] + rest["height"]
    assert not (rest["x"] <= 52 < rest["x"] + rest["width"] and rest["y"] <= 18 < rest["y"] + rest["height"])
    assert farm == original


@pytest.mark.parametrize("signal", ["takeover", "completed", "provider-error"])
def test_terminal_local_receipt_wakes_long_progress_interval_without_game_queries(tmp_path, monkeypatch, signal):
    runner = setup_run(tmp_path)
    runner.save_id = "Farm_1"
    runner.baseline_goals = set()
    runner.goal_id = "project"
    runner.monitor_seconds = 900
    runner.command_offset = 0
    if signal == "takeover":
        monitor.write(runner.evidence / "creative-farm-control.json", {"runId": runner.run.name, "active": True, "agentOwns": False})
    elif signal == "completed":
        monitor.write(runner.mod / "data/work-state.json", {"Farm_1": {"goals": [{"id": "project", "status": "completed"}]}})
    else:
        monitor.append(runner.mod / "chat_commands.jsonl", {"saveId": "Farm_1", "status": "failed"})
    sleeps = []
    async def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) == 1, "A terminal receipt must not wait for the 15-minute interval"
    monkeypatch.setattr(monitor.asyncio, "sleep", sleep)
    asyncio.run(runner.wait_for_monitor_signal())
    assert sleeps == [2]


def test_raw_landscape_and_native_clearing_cannot_be_replaced_by_decoration_counts():
    before, after = sample(), sample(True)
    initial = [{"kind": kind, "x": index, "y": 2} for index, kind in enumerate(("Tree", "Grass", "resource-clump", "object"))]
    before["creativeFarm"]["space"] = {"occupants": initial}
    after["creativeFarm"]["space"] = {"occupants": initial[:2]}
    criteria = {**CRITERIA, "requireRawGround": True, "requireNativeClearing": True, "requirePreservedVegetation": True}
    checks = monitor.world_checks(before, after, criteria, ledger())
    assert checks["raw_source_ground_observed"] and checks["original_tree_retained"] and checks["original_grass_retained"]
    assert not checks["native_clearing_recorded"]
    cleared = {"type": "native-result", "payload": {"skillId": "clear-debris", "completedCount": 1, "terminalState": "succeeded"}}
    checks = monitor.world_checks(before, after, criteria, [*ledger(), cleared])
    assert checks["native_clearing_recorded"]
    after["creativeFarm"]["space"]["occupants"] = []
    checks = monitor.world_checks(before, after, criteria, [*ledger(), cleared])
    assert not checks["original_tree_retained"] and not checks["original_grass_retained"]


def test_decoration_counts_do_not_hide_blocked_or_unobserved_chest_access():
    after = sample(True)
    criteria = {**CRITERIA, "requireAccessibleFixtures": True}
    access = [{**item, "reachable": True} for item in after["creativeFarm"]["layout"] if "chest" in item["categories"]]
    after["creativeFarm"]["operationAccess"] = access
    assert monitor.world_checks(sample(), after, criteria, ledger())["chests_and_processing_accessible"]
    access[1]["reachable"] = False
    assert not monitor.world_checks(sample(), after, criteria, ledger())["chests_and_processing_accessible"]
    access.pop()
    assert not monitor.world_checks(sample(), after, criteria, ledger())["chests_and_processing_accessible"]


def test_design_must_precede_first_clearing_attempt_for_the_same_project(monkeypatch):
    plan = call({"id": "project", "project": {"summary": "Keep the grove", "layout": "Route around the trees"}})
    clear = {"name": "mcp__game__submit_work", "input": {"action": {"clear_debris": {"tiles": []}}}}
    monkeypatch.setattr(monitor, "summarize_trace", lambda calls: {"toolCalls": calls})
    assert monitor.design_precedes_clearing([plan, clear], "project")
    assert not monitor.design_precedes_clearing([clear, plan], "project")
    assert not monitor.design_precedes_clearing([plan, clear], "another-project")


def test_rejected_brief_stops_instead_of_monitoring_unrelated_autoplay(tmp_path):
    runner = setup_run(tmp_path)
    runner.save_id, runner.prompt, runner.baseline_requests = "Farm_1", "full design brief", set()
    actions = []
    async def request(action, **params):
        actions.append(action)
        return {"chatStatus": "Failed", "conversation": [{"IsPlayer": False, "Text": "text too long"}]}
    runner.request = request
    with pytest.raises(RuntimeError, match="Initial design brief rejected: text too long"):
        asyncio.run(runner.wait_for_brief_receipt())
    assert actions == ["snapshot"]  # Never resend the brief or hand control back.


def layout():
    items = [{"kind": "floor", "itemId": "(O)328", "x": x, "y": 0, "width": 1, "height": 1,
              "origin": "agent-or-native", "categories": ["path"]} for x in range(24)]
    items += [{"kind": "object", "itemId": item, "x": x, "y": 1, "width": 1, "height": 1,
               "origin": "agent-or-native", "categories": categories}
              for item, x, categories in [("(BC)130", 2, ["chest"]), ("(BC)130", 3, ["chest"]),
                                           ("(BC)146", 20, ["rest", "light"]), ("(BC)147", 21, ["light"])]]
    return items


def sample(finished=False):
    return {"year": 1, "season": "spring", "day": 1, "timeOfDay": 1000,
            "companionMachineExecuting": None, "companionActiveTaskId": None, "companionRestState": "awake",
            "creativeFarm": {"active": True, "error": None, "agentOwns": True, "harnessTimeFrozen": True, "fixedTimeOfDay": 1000,
                             "buildingsPreserved": True, "entrancesReachable": True,
                             "waterTilesPreserved": True, "extraBuildings": [],
                             "entrances": [{"id": "farmhouse", "initiallyReachable": True, "reachable": True}],
                             "counts": {"pathTiles": 24, "chests": 2, "restObjects": 1, "lights": 2} if finished
                             else {"pathTiles": 0, "chests": 0, "restObjects": 0, "lights": 0},
                             "layout": layout() if finished else []}}


def ledger():
    result = []
    for item_id, count in {"(O)328": 24, "(BC)130": 2, "(BC)146": 1, "(BC)147": 1}.items():
        result.append({"type": "native-result", "payload": {"CommandId": "craft-" + item_id,
                      "TerminalState": "succeeded", "SkillId": "craft-items", "CompletedCount": count,
                      "Effects": [{"state": "crafted", "itemId": item_id, "stack": count}]}})
    for index, item in enumerate(layout()):
        result.append({"type": "native-result", "payload": {"commandId": f"place-{index}",
                      "terminalState": "succeeded", "skillId": "place-items", "completedCount": 1,
                      "effects": [{"state": "placed", "itemId": item["itemId"], "stack": 1,
                                   "tile": {"x": item["x"], "y": item["y"]}}]}})
    return result


def call(goal, action="create"):
    return {"name": "mcp__game__manage_arrangements", "input": {"change": {"manage_goal": {"action": action}}},
            "result": {"saveId": "Farm_1", "goal": goal}, "error": False, "nonterminal": False}


def event(request_id, goal, action="create"):
    tool = call(goal, action)
    return {"requestId": request_id, "event": {"item": {"type": "tool_call", "toolCall": {
        "name": tool["name"], "input": tool["input"], "status": 2,
        "output": {"details": {"mcp": {"structuredContent": tool["result"]}}}}}}}


def test_world_requires_craft_and_placement_receipts_for_each_real_item():
    assert all(monitor.world_checks(sample(), sample(True), CRITERIA, ledger()).values())
    missing_craft = [row for row in ledger() if row["payload"].get("SkillId") != "craft-items"]
    missing_craft += [{"type": "material-supply", "payload": {"items": ["finished decorations"]}}]
    checks = monitor.world_checks(sample(), sample(True), CRITERIA, missing_craft)
    assert not checks["placed_items_were_crafted"]
    assert checks["placed_items_have_native_receipts"]
    checks = monitor.world_checks(sample(), sample(True), CRITERIA, ledger()[:-1])
    assert not checks["placed_items_have_native_receipts"]


def test_duplicate_failed_and_skipped_receipts_do_not_inflate_production():
    first = ledger()[0]
    failed = copy.deepcopy(first)
    failed["payload"].update(CommandId="failed-craft", TerminalState="failed")
    skipped = copy.deepcopy(first)
    skipped["payload"].update(CommandId="skipped-craft", CompletedCount=0)
    crafted, placed = monitor.construction_evidence([first, first, failed, skipped])
    assert crafted == {"(O)328": 24}
    assert not placed


def test_existing_decorations_missing_observations_and_changed_day_do_not_pass():
    after = sample(True)
    after["creativeFarm"]["layout"][0]["origin"] = "baseline"
    after["creativeFarm"]["entrances"] = []
    after["day"] = 2
    checks = monitor.world_checks(sample(), after, CRITERIA, ledger())
    assert not checks["minimum_pathTiles"]
    assert not checks["native_counts_match_layout"]
    assert not checks["fixed_daylight"]
    assert not checks["entrances_reachable"]
    empty = monitor.world_checks({}, {}, CRITERIA, [])
    assert not empty["creative_fixture_active"] and not empty["native_action_finished"]


def test_root_binding_uses_initial_real_receipt_and_excludes_child_and_old_goal():
    root = {"id": "root", "parent_id": "old", "project": {"zones": ["storage", "rest"]}}
    child = {"id": "child", "parent_id": "root", "status": "completed"}
    old = {"id": "old", "status": "completed"}
    state = {"goals": [old, child, root]}
    assert monitor.bind_goal(state, {}, {"old"}, [call(child), call(root), call(old)]) == ("root", "initial-turn manage_goal(create)")
    assert monitor.bind_goal(state, {}, {"old"}, [call(child)]) == (None, None)
    assert monitor.bind_goal(state, {}, {"old"}, []) == (None, None)
    assert monitor.bind_goal(state, {"goalId": "old"}, {"old"}, []) == (None, None)
    with pytest.raises(RuntimeError, match="multiple"):
        monitor.bind_goal({"goals": [root, {"id": "other"}]}, {}, set(), [call(root), call({"id": "other"})])


def test_trace_filter_excludes_unrelated_continuation_goals():
    initial = event("player-1", {"id": "project"})
    later = event("autonomy-2", {"id": "unrelated"})
    assert monitor.initial_turn_trace([initial, later], "player-1", "brief") == [initial]
    agy = [{"_provider": "agy", "conversationId": "same", "type": "USER_INPUT", "content": "system\nbrief"},
           {"_provider": "agy", "conversationId": "same", "type": "PLANNER_RESPONSE"},
           {"_provider": "agy", "conversationId": "same", "type": "USER_INPUT", "content": "continue"},
           {"_provider": "agy", "conversationId": "same", "type": "PLANNER_RESPONSE"}]
    assert monitor.initial_turn_trace(agy, "player-1", "brief") == agy[:2]


def test_agy_midflight_brief_binds_only_after_matching_delivery_receipt():
    def delivery(request_id, text):
        return {"_provider": "agy", "conversationId": "same", "type": "GENERIC",
                "content": "Created At: now\nCompleted At: now\n" + json.dumps({
                    "playerMessages": [{"requestId": request_id, "text": text}]})}

    prompt = "garden\nfinish the whole project"
    delivered = delivery("player-1", prompt)
    planner = {"_provider": "agy", "conversationId": "same", "type": "PLANNER_RESPONSE",
               "tool_calls": [{"name": "call_mcp_tool", "args": {"ServerName": "game",
                   "ToolName": "manage_arrangements", "Arguments": {"change": {"manage_goal": {"action": "create"}}}}}]}
    result = {"_provider": "agy", "conversationId": "same", "type": "GENERIC", "status": "DONE",
              "content": json.dumps({"goal": {"id": "root"}})}
    trace = [delivery("someone-else", prompt), delivery("player-1", "other text"), delivered, planner, result,
             {"_provider": "agy", "conversationId": "same", "type": "USER_INPUT", "content": "continue"}, planner]
    selected = monitor.initial_turn_trace(trace, "player-1", prompt)
    assert selected == [delivered, planner, result]
    calls = monitor.summarize_trace(selected)["toolCalls"]
    assert monitor.bind_goal({"goals": [{"id": "root"}]}, {}, set(), calls)[0] == "root"


def test_blocked_main_completion_ends_with_evidence_instead_of_idle_rounds(tmp_path):
    runner = setup_run(tmp_path)
    runner.goal_id = "locked-main"
    runner.commands = list
    runner.requested_stop = lambda: False
    runner.observe_project = lambda trace: {"id": runner.goal_id, "status": "active"}
    rejected = event("turn", {}, "complete")
    tool = rejected["event"]["item"]["toolCall"]
    tool["input"]["change"]["manage_goal"]["goal_id"] = runner.goal_id
    tool["output"]["details"]["mcp"]["structuredContent"] = {
        "goalId": runner.goal_id, "blocked": True, "reasonCode": "MAIN_GOAL_LOCKED"}
    runner.trace = lambda: [rejected]

    async def native():
        return sample(True)

    runner.native = native
    with pytest.raises(RuntimeError, match="MAIN_GOAL_LOCKED"):
        asyncio.run(runner.monitor())
    assert runner.report["completionBlocked"]["goalId"] == "locked-main"
    assert monitor.blocked_completion([rejected], "different-project") is None


def test_only_actual_completion_receipt_settles_even_if_followup_autonomy_started():
    commands = [{"requestId": "initial", "saveId": "Farm_1", "status": "completed"}]
    assert not monitor.round_settled(None, commands, "initial", "Farm_1")
    assert monitor.round_settled({"saveId": "Farm_1", "requestId": "autonomy-2"}, commands, "initial", "Farm_1")
    assert not monitor.round_settled({"saveId": "Farm_1", "requestId": "initial"}, commands, "initial", "Farm_1")
    assert not monitor.round_settled({"saveId": "Farm_1", "requestId": None}, commands, "autonomy-2", "Farm_1")
    commands.append({"requestId": "autonomy-2", "saveId": "Farm_1", "status": "completed"})
    assert monitor.round_settled({"saveId": "Farm_1", "requestId": None}, commands, "autonomy-2", "Farm_1")


def test_completion_metadata_alone_is_not_a_design():
    assert not monitor.design_recorded({"phase": "completed", "completionAssessment": "model-reviewed"})
    assert not monitor.design_recorded({"summary": "wood and stone", "layout": []})
    assert monitor.design_recorded({"summary": "wood and stone", "layout": {"zones": ["storage", "rest"]}})


def test_monitor_started_before_launcher_does_not_claim_run_directory(tmp_path):
    async def check():
        runner = monitor.CreativeRunner(tmp_path / "not-yet-launched")
        task = asyncio.create_task(runner.prepare())
        try:
            await asyncio.sleep(0.05)
            assert not task.done()
            assert not runner.run.exists(), "Launcher must be able to allocate its fresh run directory"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(check())


def setup_run(tmp_path):
    run = tmp_path / "creative-test"
    evidence = run / "evidence"
    evidence.mkdir(parents=True)
    saves = run / "saves"
    saves.mkdir()
    session = {"runId": run.name, "scenario": "free-mode-control", "isolatedSavesPath": str(saves)}
    monitor.write(run / "config/test-session.json", session)
    monitor.write(evidence / "harness-coordination.json", {**session, "state": "READY_FOR_ACT"})
    monitor.write(evidence / "harness-fixture-startup.json", {**session, "mode": "creative-farm", "actorMode": "player", "saveName": "Farm_1"})
    cases = run / "cases.json"
    monitor.write(cases, {"creative-farm": {"designBrief": "a wood and stone garden", "instruction": "finish this one project", "criteria": CRITERIA}})
    runner = monitor.CreativeRunner(run, cases_file=cases)
    runner.poll_seconds = 0
    runner.monitor_seconds = 0
    for path in (runner.mod / "StardewAI.Companion.Mod.dll", run / "mods/StardewAI.Companion.TestDriver/StardewAI.Companion.TestDriver.dll"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test fixture")
    monitor.write(runner.mod / "config/chat-backend.json", {"backend": "mcode"})
    return runner


def png_capture(runner, label, actual):
    # Only the PNG header is needed by the receipt verifier, not image decoding.
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 1280, 1024)
    (runner.evidence / f"harness-farm-{label}.png").write_bytes(raw)
    monitor.write(runner.evidence / f"harness-farm-{label}.json", {"runId": runner.run.name, "captured": True, "sha256": hashlib.sha256(raw).hexdigest(),
                  "dimensions": {"width": 1280, "height": 1024}, "layout": actual["creativeFarm"]["layout"]})


@pytest.mark.parametrize("round_seconds", [0, 4000], ids=["ordinary-rounds", "continues-beyond-one-hour"])
def test_full_monitor_sends_one_brief_continues_multiple_rounds_and_saves(tmp_path, monkeypatch, round_seconds):
    runner = setup_run(tmp_path)
    trace, actions = [], []
    native_count = 0
    owns = False
    actual = sample()
    goal = {"id": "project", "status": "active", "project": {"summary": "wood/stone garden", "layout": layout(), "zones": ["storage", "rest"]}}
    request_record = None
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(monitor, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now))

    async def binding(*args):
        return None

    monkeypatch.setattr(monitor, "verify_agy_binding", binding)
    monkeypatch.setattr(monitor, "provider_trace", lambda mod: copy.deepcopy(trace))

    async def request(action, **params):
        nonlocal owns, request_record
        actions.append((action, params))
        if action == "takeover-key":
            owns = not owns
        if action == "key" and params.get("key") == "Enter":
            request_record = {"id": "initial", "text": runner.prompt, "origin": "player", "goalId": None}
            monitor.write(runner.mod / "data/work-state.json", {"Farm_1": {"requests": [request_record], "goals": []}})
        if action == "harness-farm-capture":
            label = f"capture-{len(actions)}"
            png_capture(runner, label, actual)
            return {"label": label}
        return {"actorMode": "player", "playerAuthorityAgentOwns": owns, "productionChatConnected": True,
                "chatStatus": "Completed", "menuType": monitor.CHAT_MENU}

    async def native():
        nonlocal native_count, actual
        native_count += 1
        if 2 <= native_count <= 4:
            clock.now += round_seconds
        if native_count >= 2:
            actual = sample(True)
            if native_count == 2:
                trace.append(event("initial", copy.deepcopy(goal)))
                monitor.append(runner.mod / "chat_commands.jsonl", {"requestId": "initial", "saveId": "Farm_1",
                               "status": "completed", "conversationId": "same-session", "provider": "mcode"})
            if native_count == 4:
                goal["status"] = "completed"
                goal["project"]["phase"] = "completed"
                trace.append(event("autonomy-2", copy.deepcopy(goal), "complete"))
                monitor.append(runner.mod / "chat_commands.jsonl", {"requestId": "autonomy-2", "saveId": "Farm_1",
                               "status": "completed", "conversationId": "same-session", "provider": "mcode"})
                for row in ledger():
                    monitor.append(runner.evidence / "creative-farm-ledger.jsonl", row)
            monitor.write(runner.mod / "data/work-state.json", {"Farm_1": {"requests": [request_record], "goals": [goal]}})
            monitor.write(runner.mod / "data/life-turn.json", {"saveId": "Farm_1", "requestId": None})
        actual["creativeFarm"]["agentOwns"] = owns
        runner.samples.append(copy.deepcopy(actual))  # Same retention as the inherited native() implementation.
        return copy.deepcopy(actual)

    original_wait = runner.wait

    async def wait(predicate, message, timeout=30):
        path = runner.evidence / "lifecycle-request.json"
        if path.exists():
            save_request = monitor.read(path)
            folder = runner.run / "saves/Farm_1"
            folder.mkdir(exist_ok=True)
            (folder / "Farm_1").write_text("native saved farm")
            monitor.write(runner.evidence / f"lifecycle-{save_request['label']}.json",
                          {**save_request, "saveName": "Farm_1", "worldReady": True})
            path.unlink()
        return await original_wait(predicate, message, timeout)

    runner.request, runner.native, runner.wait = request, native, wait
    assert asyncio.run(runner.execute()) == 0
    assert native_count == 5  # Initial, three project samples, then frozen final.
    assert sum(action == "text" for action, _ in actions) == 1
    assert sum(action == "key" and params.get("key") == "Enter" for action, params in actions) == 1
    assert sum(action == "takeover-key" for action, _ in actions) == 2
    report = monitor.read(runner.evidence / "creative-farm-report.json")
    assert report["status"] == "passed" and all(report["checks"].values())
    assert report["technicalPassed"] is True
    assert report["wallBudgetSeconds"] is None
    assert report["elapsedSeconds"] == round_seconds * 3
    assert len(runner.samples) <= 1  # Full history is on disk, not retained throughout an overnight run.
    assert report["fullPrompt"] == "a wood and stone garden\n\nfinish this one project"
    assert (runner.run / report["initialSavedFarm"]["directory"] / "Farm_1").is_file()
    assert report["instruction"] == "finish this one project"
    assert report["humanReview"]["status"] == "pending"
    assert report["completionRequestId"] == "autonomy-2"
    assert len(report["rounds"]) == 2
    assert report["savedFarm"]["files"][0]["sha256"] == hashlib.sha256(b"native saved farm").hexdigest()
    assert (runner.evidence / "creative-farm-before.png").is_file()
    assert (runner.evidence / "creative-farm-final.png").is_file()
    design = monitor.read(runner.evidence / "creative-farm-design.json")
    assert design["goal"]["project"]["layout"] == layout()
    assert len(monitor.rows(runner.evidence / "creative-farm-project-history.jsonl")) == 2
    assert monitor.read(runner.evidence / "free-mode-control-done.json")["success"] is True


@pytest.mark.parametrize("stop_source", ["takeover", "request", "project-cancelled", "provider-error"])
def test_explicit_stop_and_provider_error_preserve_work_without_waiting_for_completion(tmp_path, monkeypatch, stop_source):
    runner = setup_run(tmp_path)
    monkeypatch.setattr(monitor, "provider_trace", lambda mod: [])
    actions = []
    owns = stop_source != "takeover"
    saved = False
    observed = sample()
    commands = [{"requestId": "round-1", "saveId": "Farm_1", "conversationId": "same-session", "provider": "mcode",
                 "status": "cancelled" if stop_source == "takeover" else "failed", "error": "provider failed"}]
    runner.commands = lambda: commands if stop_source in {"provider-error", "takeover"} else []
    runner.observe_project = lambda trace: {"id": "project", "status": "cancelled"} if stop_source == "project-cancelled" else None

    async def prepare():
        runner.dispatched = True
        runner.handoff_attempted = True
        runner.save_id = "Farm_1"
        runner.save_root = runner.run / "saves"
        runner.criteria = CRITERIA
        runner.report.update(before=sample(), backend={"backend": "mcode"})
        monitor.write(runner.mod / "data/life-turn.json", {"saveId": "Farm_1", "requestId": None})
        await runner.capture("before")
        if stop_source == "request":
            monitor.write(runner.evidence / "reviewer-stop.json", {"runId": runner.run.name,
                          "reviewerRequestedStop": True, "reason": "save this version for review"})

    async def native():
        observed["creativeFarm"]["agentOwns"] = owns
        return copy.deepcopy(observed)

    async def request(action, **params):
        nonlocal owns
        actions.append(action)
        if action == "takeover-key":
            assert owns is True, "Monitor must not give the agent control back after manual F9"
            owns = False
        if action == "harness-farm-capture":
            label = f"stop-{len(actions)}"
            png_capture(runner, label, observed)
            return {"label": label}
        return {"actorMode": "player", "playerAuthorityAgentOwns": owns}

    async def save():
        nonlocal saved
        saved = True
        runner.report["savedFarm"] = {"modelMarkedProjectComplete": False}

    runner.prepare, runner.native, runner.request, runner.save_current = prepare, native, request, save
    assert asyncio.run(runner.execute()) == (1 if stop_source == "provider-error" else 2)
    report = monitor.read(runner.evidence / "creative-farm-report.json")
    assert saved and report["savedFarm"]["modelMarkedProjectComplete"] is False
    assert report["status"] == ("error" if stop_source == "provider-error" else "stopped")
    assert report["technicalPassed"] is False
    assert actions.count("takeover-key") == (0 if stop_source == "takeover" else 1)
    assert (runner.evidence / "creative-farm-final.png").is_file()
    assert monitor.read(runner.evidence / "free-mode-control-done.json")["success"] is False
    if stop_source == "takeover":
        assert report["stopReason"] == "player-takeover" and "failedTurn" not in report
    if stop_source == "request":
        assert report["reviewerStopRequest"]["reason"] == "save this version for review"


def test_stop_request_requires_matching_run_and_explicit_confirmation(tmp_path):
    runner = setup_run(tmp_path)
    monitor.write(runner.evidence / "reviewer-stop.json", {"runId": "another-run", "reviewerRequestedStop": True})
    assert not runner.requested_stop()
    monitor.write(runner.evidence / "reviewer-stop.json", {"runId": runner.run.name, "reviewerRequestedStop": False})
    assert not runner.requested_stop()
    assert "stopReason" not in runner.report


def test_reopened_project_discards_old_completion_deadline(tmp_path, monkeypatch):
    runner = setup_run(tmp_path)
    runner.save_id = "Farm_1"
    runner.criteria = CRITERIA
    runner.goal_id = "project"
    runner.report.update(before=sample(), backend={"backend": "mcode"}, recordedDesignObserved=True)
    clock = SimpleNamespace(now=0, round=0)
    monkeypatch.setattr(monitor, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now))
    runner.trace = list
    runner.observe_project = lambda trace: {"id": "project", "status": "active" if clock.round == 2 else "completed",
                                           "project": {"summary": "revised garden", "layout": layout()}}
    runner.commands = lambda: ([{"requestId": "finished-later", "saveId": "Farm_1", "status": "completed",
                                "conversationId": "same-session", "provider": "mcode"}] if clock.round == 3 else [])

    async def native():
        clock.round += 1
        assert clock.round <= 3
        clock.now = (clock.round - 1) * 4000
        monitor.write(runner.mod / "data/life-turn.json", {"saveId": "Farm_1",
                      "requestId": None if clock.round == 3 else "still-working"})
        return sample(True)

    runner.native = native
    asyncio.run(runner.monitor())
    assert clock.round == 3 and clock.now == 8000
    assert runner.report["stopReason"] == "project-completed"
    assert runner.report["completionRequestId"] == "finished-later"


def test_capture_rejects_metadata_without_written_matching_png(tmp_path):
    runner = setup_run(tmp_path)

    async def request(*args, **kwargs):
        png_capture(runner, "bad", sample())
        (runner.evidence / "harness-farm-bad.png").write_bytes(b"not the captured image")
        return {"label": "bad"}

    runner.request = request
    with pytest.raises(RuntimeError, match="verified real PNG"):
        asyncio.run(runner.capture("final"))
    assert not (runner.evidence / "creative-farm-final.png").exists()


def test_setup_failure_always_writes_done_and_preserves_failure(tmp_path, monkeypatch):
    runner = setup_run(tmp_path)
    monkeypatch.setattr(monitor, "provider_trace", lambda mod: [])

    async def prepare():
        raise RuntimeError("wrong actor")

    runner.prepare = prepare
    assert asyncio.run(runner.execute()) == 1
    report = monitor.read(runner.evidence / "creative-farm-report.json")
    assert report["status"] == "setup-error"
    assert "wrong actor" in report["error"]
    assert report["dispatched"] is False
    assert monitor.read(runner.evidence / "free-mode-control-done.json")["success"] is False


def test_handoff_receipt_failure_still_reclaims_authority_before_exit(tmp_path, monkeypatch):
    runner = setup_run(tmp_path)
    monkeypatch.setattr(monitor, "provider_trace", lambda mod: [])
    reclaimed = False

    async def prepare():
        runner.save_root = runner.run / "saves"
        runner.report["before"] = sample()
        runner.handoff_attempted = True  # F9 was consumed, its UI receipt was lost.
        raise TimeoutError("handoff UI receipt missing")

    async def freeze():
        nonlocal reclaimed
        reclaimed = True

    runner.prepare, runner.freeze_for_delivery = prepare, freeze
    assert asyncio.run(runner.execute()) == 1
    report = monitor.read(runner.evidence / "creative-farm-report.json")
    assert reclaimed and report["handoffAttempted"] is True and report["dispatched"] is False
    assert "handoff UI receipt missing" in report["error"]
    assert not monitor.read(runner.evidence / "free-mode-control-done.json")["success"]


def test_failed_save_never_reports_pass(tmp_path, monkeypatch):
    runner = setup_run(tmp_path)
    monkeypatch.setattr(monitor, "provider_trace", lambda mod: [])

    async def prepare():
        runner.dispatched = True
        runner.save_root = runner.run / "saves"
        runner.criteria = CRITERIA
        runner.report["before"] = sample()

    async def observe():
        runner.report.update(stopReason="project-completed", checks={"same_project_completed": True})

    async def freeze():
        return sample(True)

    async def capture(name):
        runner.report["captures"] = {"before": {}, "final": {}}
        return {"layout": layout()}

    async def save():
        raise RuntimeError("native save failed")

    runner.prepare, runner.monitor, runner.freeze_for_delivery, runner.capture, runner.save_current = prepare, observe, freeze, capture, save
    monkeypatch.setattr(runner, "write_design", lambda: None)
    for row in ledger():
        monitor.append(runner.evidence / "creative-farm-ledger.jsonl", row)
    assert asyncio.run(runner.execute()) == 1
    report = monitor.read(runner.evidence / "creative-farm-report.json")
    assert report["status"] == "error" and "native save failed" in report["deliveryError"]
    assert monitor.read(runner.evidence / "free-mode-control-done.json")["success"] is False


def test_failed_monitor_still_saves_partial_work_without_erasing_original_error(tmp_path, monkeypatch):
    runner = setup_run(tmp_path)
    monkeypatch.setattr(monitor, "provider_trace", lambda mod: [])
    saved = False

    async def prepare():
        runner.dispatched = True
        runner.save_root = runner.run / "saves"
        runner.criteria = CRITERIA
        runner.report["before"] = sample()

    async def observe():
        raise RuntimeError("provider disconnected")

    async def freeze():
        return sample()

    async def capture(name):
        raise RuntimeError("image unavailable")

    async def save():
        nonlocal saved
        saved = True
        runner.report["savedFarm"] = {"modelMarkedProjectComplete": False}

    runner.prepare, runner.monitor, runner.freeze_for_delivery, runner.capture, runner.save_current = prepare, observe, freeze, capture, save
    assert asyncio.run(runner.execute()) == 1
    report = monitor.read(runner.evidence / "creative-farm-report.json")
    assert saved and report["savedFarm"]["modelMarkedProjectComplete"] is False
    assert "provider disconnected" in report["error"]
    assert "image unavailable" in report["captureError"]
    assert report["technicalPassed"] is False
    assert monitor.read(runner.evidence / "free-mode-control-done.json")["success"] is False
