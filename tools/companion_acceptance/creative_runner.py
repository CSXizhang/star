"""One brief, one persistent farm project, real native construction evidence.

The monitor observes the ordinary agent. It never sends continuation prompts,
edits work state, chooses a layout, or dispatches a construction action.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import struct
import time
from collections import Counter
from pathlib import Path

from runner import (
    Runner,
    native_finished,
    provider_trace,
    read,
    rows,
    summarize_trace,
    verify_agy_binding,
    write,
)

MINIMUMS = {"pathTiles": "minPathTiles", "chests": "minChests", "restObjects": "minRestObjects", "lights": "minLights"}
CATEGORIES = {"pathTiles": "path", "chests": "chest", "restObjects": "rest", "lights": "light"}


def detail_regions(farm):
    """Frame built scenes, keeping flower/rest views away from duplicate workshops."""
    bounds = farm.get("workArea") or {}
    width, height = min(24, bounds.get("width", 0)), min(18, bounds.get("height", 0))
    if width <= 0 or height <= 0:
        return []
    items = [item for item in farm.get("layout", []) if item.get("origin") == "agent-or-native"]
    selected = []
    for name, category, preferred in (("flowers", "flower", None), ("workshop", "chest", None),
                                      ("cultivation", "farming", None), ("rest", "rest", "(BC)146"),
                                      ("scenery", "path", None), ("scene", None, None)):
        candidates = [item for item in items if category is None or category in item.get("categories", [])]
        preferred_items = [item for item in candidates if item.get("itemId") == preferred]
        candidates = preferred_items or candidates
        if not candidates:
            continue
        center_x = sum(item["x"] for item in candidates) / len(candidates)
        center_y = sum(item["y"] for item in candidates) / len(candidates)
        for item in sorted(candidates, key=lambda item: (item["x"]-center_x)**2 + (item["y"]-center_y)**2):
            region = {"x": max(0, min(item["x"] - width // 2, bounds["width"] - width)),
                      "y": max(0, min(item["y"] - height // 2, bounds["height"] - height)),
                      "width": width, "height": height}
            if any(max(0, min(region["x"]+width, old["x"]+width)-max(region["x"], old["x"]))
                   * max(0, min(region["y"]+height, old["y"]+height)-max(region["y"], old["y"]))
                   > width * height * .65 for _, old in selected):
                continue
            selected.append((name, region))
            break
    return selected
CHAT_MENU = "StardewAI.Companion.Mod.Menus.CompanionConversationMenu"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def append(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def lower_keys(value):
    return {key.lower(): item for key, item in value.items()} if isinstance(value, dict) else {}


def goal_operation(call):
    args = call.get("input") or {}
    if isinstance(args, str) and re.search(r"\n<truncated \d+ bytes>$", args):
        # agy's human-readable transcript truncates long Arguments, while the
        # paired native receipt is complete. Recover only the visible operation
        # and receipt's identity, never invent missing layout/coordinates.
        prefix = r'^\s*\{\s*"change"\s*:\s*\{\s*"manage_goal"\s*:\s*\{\s*"action"\s*:\s*"(create|revise|update|complete|finish)"'
        match = re.match(prefix, args) if str(call.get("name")).endswith("__manage_arrangements") else None
        result = call.get("result")
        if match and not call.get("error") and not call.get("nonterminal") and isinstance(result, dict):
            goal = result.get("goal")
            goal_id = goal.get("id") if isinstance(goal, dict) else result.get("goalId")
            if isinstance(goal_id, str) and goal_id:
                return {"action": match.group(1), "goal_id": goal_id}
    if not isinstance(args, dict):
        return {}
    if str(call.get("name")).endswith("__manage_arrangements"):
        change = args.get("change") or {}
        if isinstance(change, str):
            try:
                change = json.loads(change)
            except ValueError:
                return {}
        operation = change.get("manage_goal") if isinstance(change, dict) else None
        return operation if isinstance(operation, dict) else {}
    if str(call.get("name")).endswith("__manage_goal"):
        return args
    return {}


def initial_turn_trace(trace, request_id, prompt):
    """mcode labels requests; agy's public transcript brackets USER_INPUT steps."""
    if not any(row.get("_provider") == "agy" for row in trace):
        return [row for row in trace if row.get("requestId") == request_id]
    selected, started, conversation = [], False, None
    for row in trace:
        if row.get("type") == "USER_INPUT":
            if started:
                break
            if prompt in str(row.get("content") or ""):
                started, conversation = True, row.get("conversationId")
        elif not started and row.get("type") == "GENERIC":
            # F9 may start autonomy before Enter. The one player brief then
            # arrives at a tool boundary, not as a provider USER_INPUT step.
            content = row.get("_externalOutput", {}).get("text") or row.get("content") or ""
            body = "\n".join(line for line in content.splitlines()
                             if not line.startswith(("Created At:", "Completed At:"))).strip()
            try:
                result = json.loads(body)
            except ValueError:
                result = None
            messages = result.get("playerMessages", []) if isinstance(result, dict) else []
            if any(isinstance(message, dict) and message.get("requestId") == request_id
                   and message.get("text") == prompt for message in messages):
                started, conversation = True, row.get("conversationId")
        if started and row.get("conversationId") == conversation:
            selected.append(row)
    return selected


def blocked_completion(trace, goal_id):
    if not goal_id:
        return None
    for call in reversed(summarize_trace(trace)["toolCalls"]):
        operation = goal_operation(call)
        if operation.get("action") not in {"complete", "finish"} or operation.get("goal_id") != goal_id:
            continue
        result = call.get("result") or {}
        return result if isinstance(result, dict) and result.get("blocked") is True else None
    return None


def bind_goal(state, request, baseline_ids, calls):
    """Bind a real project root, never the latest goal or an arbitrary child."""
    goals = {goal["id"]: goal for goal in state.get("goals", [])}
    direct = request.get("goalId")
    if direct and direct in goals and direct not in baseline_ids:
        return direct, "request.goalId"
    created = {}
    for call in calls:
        if goal_operation(call).get("action") != "create" or call.get("error") or call.get("nonterminal"):
            continue
        result = call.get("result")
        goal = result.get("goal") if isinstance(result, dict) else None
        if not isinstance(goal, dict):
            continue
        if goal.get("id") in goals and goal["id"] not in baseline_ids:
            created[goal["id"]] = goal
    new_ids = set(goals) - baseline_ids
    roots = [key for key, goal in created.items()
             if (goal.get("parent_id") or goal.get("parentId")) not in new_ids]
    if len(roots) > 1:
        raise RuntimeError("Initial player turn created multiple unassociated project roots")
    return (roots[0], "initial-turn manage_goal(create)") if roots else (None, None)


def construction_evidence(ledger):
    """Only genuine successful item effects count; supplied stock proves no craft."""
    crafted, placed, commands = Counter(), set(), set()
    for row in ledger:
        if row.get("type") != "native-result":
            continue
        receipt = lower_keys(row.get("payload"))
        command = receipt.get("commandid")
        if not command or command in commands:
            continue
        commands.add(command)
        if receipt.get("terminalstate") not in {"succeeded", "partially-succeeded"} or receipt.get("completedcount", 0) <= 0:
            continue
        for raw in receipt.get("effects") or []:
            effect = lower_keys(raw)
            item_id = effect.get("itemid")
            if not item_id:
                continue
            if receipt.get("skillid") == "craft-items" and effect.get("state") == "crafted":
                crafted[item_id] += max(0, effect.get("stack", 0))
            if receipt.get("skillid") == "place-items" and effect.get("state") == "placed":
                tile = lower_keys(effect.get("tile"))
                if type(tile.get("x")) is int and type(tile.get("y")) is int:
                    placed.add((item_id, tile["x"], tile["y"]))
    return crafted, placed


def world_checks(before, after, criteria, ledger):
    farm = after.get("creativeFarm") or {}
    layout = farm.get("layout") or []
    added = [item for item in layout if item.get("origin") == "agent-or-native"]
    counts = {key: sum(CATEGORIES[key] in item.get("categories", []) for item in added) for key in MINIMUMS}
    crafted, placed = construction_evidence(ledger)
    completed_skills = {receipt.get("skillid") for row in ledger if row.get("type") == "native-result"
                        for receipt in [lower_keys(row.get("payload"))] if receipt.get("completedcount", 0) > 0}
    required_items = [item for item in added if item.get("categories")]
    placed_counts = Counter(item.get("itemId") for item in required_items)
    checks = {
        "creative_fixture_active": farm.get("active") is True and not farm.get("error"),
        "native_action_finished": native_finished(after),
        "fixed_daylight": farm.get("harnessTimeFrozen") is True and farm.get("fixedTimeOfDay") == 1000
        and all(key in before and before[key] == after.get(key) for key in ("year", "season", "day", "timeOfDay")),
        "buildings_preserved": farm.get("buildingsPreserved") is True,
        "entrances_reachable": farm.get("entrancesReachable") is True
        and any(entry.get("initiallyReachable") is True for entry in farm.get("entrances", []))
        and all(entry.get("reachable") is True for entry in farm.get("entrances", []) if entry.get("initiallyReachable") is True),
        "water_tiles_preserved": farm.get("waterTilesPreserved") is True,
        "no_buildings_added": farm.get("extraBuildings") == [],
        "no_purchase_or_building_work": not completed_skills.intersection({
            "purchase-items", "build-building", "upgrade-building", "move-building", "demolish-building"}),
        "native_counts_match_layout": all((farm.get("counts") or {}).get(key) == value for key, value in counts.items()),
        "autonomous_construction_recorded": bool(completed_skills.intersection({"place-items", "remove-items", "hoe-tiles", "clear-debris", "chop-tree", "cut-grass"})),
        "placed_items_have_native_receipts": all(
            (item.get("itemId"), item.get("x"), item.get("y")) in placed for item in required_items),
        "placed_items_were_crafted": all(crafted[item] >= count for item, count in placed_counts.items()),
    }
    checks.update({"minimum_" + key: counts[key] >= criteria[criterion] for key, criterion in MINIMUMS.items() if criterion in criteria})
    initial_space = ((before.get("creativeFarm") or {}).get("space") or {}).get("occupants", [])
    final_space = (farm.get("space") or {}).get("occupants", [])
    if criteria.get("requireRawGround"):
        checks["raw_source_ground_observed"] = all(any(item.get("kind") == kind for item in initial_space)
                                                  for kind in ("Tree", "Grass", "resource-clump", "object"))
    if criteria.get("requireNativeClearing"):
        checks["native_clearing_recorded"] = bool(completed_skills.intersection({"clear-debris", "cut-grass", "chop-tree"}))
    if criteria.get("requirePreservedVegetation"):
        for kind in ("Tree", "Grass"):
            original = {(item.get("x"), item.get("y")) for item in initial_space if item.get("kind") == kind}
            retained = {(item.get("x"), item.get("y")) for item in final_space if item.get("kind") == kind}
            checks["original_" + kind.lower() + "_retained"] = bool(original & retained)
    if criteria.get("requireProcessingAndCultivation"):
        checks["processing_facilities_built"] = any("production" in item.get("categories", []) for item in added)
        checks["cultivation_space_built"] = any(item.get("kind") == "HoeDirt" for item in final_space)
    if criteria.get("requireAccessibleFixtures"):
        access = farm.get("operationAccess") or []
        expected = {(item.get("itemId"), item.get("x"), item.get("y")) for item in added
                    if {"chest", "production"}.intersection(item.get("categories", []))}
        observed = {(item.get("itemId"), item.get("x"), item.get("y")) for item in access}
        checks["chests_and_processing_accessible"] = expected == observed and all(
            item.get("reachable") is True for item in access)
    return checks


def round_settled(lease, commands, request_id, save_id):
    return (isinstance(lease, dict) and lease.get("saveId") == save_id and "requestId" in lease
            and lease["requestId"] != request_id and bool(request_id)
            and any(row.get("requestId") == request_id and row.get("saveId") == save_id
                    and row.get("status") == "completed" for row in commands))


def design_recorded(project):
    if not isinstance(project, dict):
        return False
    summary, layout = project.get("summary"), project.get("layout")
    return (isinstance(summary, str) and bool(summary.strip()) and isinstance(layout, (str, list, dict))
            and bool(layout.strip() if isinstance(layout, str) else layout))


def design_precedes_clearing(trace, goal_id):
    planned = False
    for call in summarize_trace(trace)["toolCalls"]:
        result = call.get("result") or {}
        goal = result.get("goal") if isinstance(result, dict) else None
        if (goal_operation(call).get("action") in {"create", "revise", "update"} and not call.get("error")
                and isinstance(goal, dict) and goal.get("id") == goal_id and design_recorded(goal.get("project"))):
            planned = True
        args = call.get("input")
        action = args.get("action") if isinstance(args, dict) else None
        clearing = (isinstance(action, dict) and {"clear_debris", "cut_grass", "chop_tree"}.intersection(action)
                    or isinstance(result, dict) and result.get("skill") in {"clear-debris", "cut-grass", "chop-tree"})
        if str(call.get("name")).endswith("__submit_work") and clearing:
            return planned
    # A design may use existing free ground without clearing vegetation.
    return planned


class CreativeRunner(Runner):
    def __init__(self, run, case="creative-farm", cases_file=None):
        # This subclass never invokes Runner.execute or its case time budget.
        super().__init__(run, case, None, cases_file)
        self.dispatched = False
        self.handoff_attempted = False
        self.goal_id = None
        self.request_record = None
        self.last_project = None
        self.command_offset = self.trace_offset = self.usage_offset = 0
        self.poll_seconds = 1
        self.monitor_seconds = 15 * 60
        self.last_progress_capture = time.monotonic()
        self.progress_capture_count = 0
        self.last_captured_work_count = 0
        self.report["humanReview"] = {
            "status": "pending", "criteria": ["符合本轮约定的风格", "各区与主路连贯", "保留实用空地", "整体美感"],
            "note": "技术验收与审美评审分别记录；请结合实际农场图评价设计。",
        }

    async def native(self):
        # Dense raw-farm navigation can occupy the game thread beyond the
        # ordinary preset's 30-second receipt window. Wait for this same label;
        # never retry actions or stop a healthy project for that short delay.
        return await super().native(timeout=120)

    def state(self):
        return (read(self.mod / "data/work-state.json") or {}).get(self.save_id) or {}

    def commands(self):
        return [row for row in rows(self.mod / "chat_commands.jsonl")[self.command_offset:]
                if row.get("saveId") == self.save_id]

    def trace(self):
        return provider_trace(self.mod)[self.trace_offset:]

    async def capture(self, name, region=None):
        response = await self.request("harness-farm-capture", **({"region": region} if region else {}))
        label = response["label"]
        metadata = read(self.evidence / f"harness-farm-{label}.json") or {}
        source = self.evidence / f"harness-farm-{label}.png"
        raw = source.read_bytes()
        if (metadata.get("captured") is not True or metadata.get("runId") != self.run.name
                or metadata.get("sha256") != hashlib.sha256(raw).hexdigest()):
            raise RuntimeError("Farm capture did not produce a verified real PNG")
        if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24:
            raise RuntimeError("Farm capture is not a PNG")
        width, height = struct.unpack(">II", raw[16:24])
        if width <= 0 or height <= 0 or not isinstance(metadata.get("layout"), list):
            raise RuntimeError("Farm capture lacks dimensions or native layout")
        destination = self.evidence / f"creative-farm-{name}.png"
        shutil.copyfile(source, destination)
        result = {**metadata, "artifact": destination.name, "width": width, "height": height}
        self.report.setdefault("captures", {})[name] = result
        return result

    async def prepare(self):
        # The launcher owns run-directory allocation and rejects existing paths.
        # Wait for it before writing anything, including the evidence directory.
        await self.wait(lambda: (read(self.evidence / "harness-coordination.json") or {}).get("state") == "READY_FOR_ACT",
                        "Isolated creative fixture is not ready", 240)
        coord = read(self.evidence / "harness-coordination.json") or {}
        cfg = read(self.run / "config/test-session.json") or {}
        if any(value.get("runId") != self.run.name or value.get("scenario") != "free-mode-control" for value in (coord, cfg)):
            raise RuntimeError("Creative runner requires its isolated free-mode-control run")
        fixture = read(self.evidence / "harness-fixture-startup.json") or {}
        if fixture.get("mode") != "creative-farm" or fixture.get("actorMode") != "player":
            raise RuntimeError("Creative fixture requires a player-bound actor")
        self.save_root = Path(fixture["isolatedSavesPath"]).resolve()
        if not self.save_root.is_relative_to(self.run) or self.save_root != Path(cfg["isolatedSavesPath"]).resolve():
            raise RuntimeError("Creative save must stay within this isolated run")
        self.save_id = fixture["saveName"]
        self.spec = (read(self.cases_file) or {})[self.case]
        self.criteria = self.spec["criteria"]
        if any(type(self.criteria[key]) is not int or self.criteria[key] < 0 for key in MINIMUMS.values() if key in self.criteria):
            raise ValueError("Optional legacy quantity criteria must be nonnegative integers")
        rendered_instruction = self.spec["instruction"].strip()
        self.prompt = self.spec["designBrief"].strip() + "\n\n" + rendered_instruction
        self.report.update({"fixture": fixture, "designBrief": self.spec["designBrief"], "instruction": self.spec["instruction"],
                            "renderedInstruction": rendered_instruction, "fullPrompt": self.prompt, "prompt": self.prompt,
                            "criteria": self.criteria, "wallBudgetSeconds": None, "monitorIntervalSeconds": self.monitor_seconds,
                            "caseDefinitionSha256": sha(self.cases_file),
                            "modSha256": sha(self.mod / "StardewAI.Companion.Mod.dll"),
                            "testDriverSha256": sha(self.run / "mods/StardewAI.Companion.TestDriver/StardewAI.Companion.TestDriver.dll"),
                            "backend": read(self.mod / "config/chat-backend.json")})
        references = read(self.mod / "config/reference-images/manifest.json") or {}
        self.report["referenceImages"] = [{key: entry[key] for key in ("id", "title", "author", "source", "sha256")}
                                          for entry in references.get("images", [])]
        write(self.evidence / "creative-farm-brief.json", {key: self.report[key] for key in (
            "designBrief", "instruction", "renderedInstruction", "fullPrompt", "criteria", "referenceImages")})
        await self.request("close")
        before = await self.native()
        self.report["before"] = before
        farm = before.get("creativeFarm") or {}
        if farm.get("active") is not True or farm.get("error") or not native_finished(before):
            raise RuntimeError("Creative fixture is not observable and idle")
        if any((farm.get("counts") or {}).get(key) != 0 for key in MINIMUMS):
            raise RuntimeError("Creative fixture must start without supplied finished decorations")
        self.report["agyBindingCheck"] = await verify_agy_binding(self.run, self.mod, before)
        if self.criteria.get("requireRawGround"):
            occupants = (farm.get("space") or {}).get("occupants", [])
            if fixture.get("groundPrecleared") is not False or not all(
                    any(item.get("kind") == kind for item in occupants) for kind in ("Tree", "Grass", "resource-clump", "object")):
                raise RuntimeError("Creative farm must retain the source's raw vegetation and obstacles")
        await self.capture("before")
        await self.save_current("initial")
        deadline = time.monotonic() + 120
        while True:
            ui = await self.request("snapshot")
            if ui.get("productionChatConnected") and ui.get("chatStatus") in {"Idle", "Completed", "Failed"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Production chat did not become ready")
            await asyncio.sleep(self.poll_seconds)
        if ui.get("actorMode") != "player" or ui.get("playerAuthorityAgentOwns") is not False:
            raise RuntimeError("Creative run must await the initial player handoff")
        baseline = self.state()
        self.baseline_goals = {goal["id"] for goal in baseline.get("goals", [])}
        self.baseline_requests = {request["id"] for request in baseline.get("requests", [])}
        self.command_offset = len(rows(self.mod / "chat_commands.jsonl"))
        self.trace_offset = len(provider_trace(self.mod))
        self.usage_offset = len(rows(self.mod / "data/context-usage.jsonl"))
        # The UI may consume F9 even if its receipt is lost. Cleanup must run
        # after this point independently of whether chat submission succeeds.
        self.handoff_attempted = True
        await self.request("takeover-key")
        handoff = await self.request("snapshot")
        if handoff.get("playerAuthorityAgentOwns") is not True:
            raise RuntimeError("Normal F9 handoff did not grant agent authority")
        self.report["initialHandoff"] = handoff
        await self.request("open-conversation")
        menu = await self.request("snapshot")
        if menu.get("menuType") != CHAT_MENU:
            raise RuntimeError("Initial conversation menu identity differs")
        await self.request("text", field="_input", text=self.prompt)
        await self.request("key", key="Enter", expectedMenuType=CHAT_MENU)
        self.dispatched = True
        await self.wait_for_brief_receipt()
        try:
            await self.request("key", key="Escape", expectedMenuType=CHAT_MENU)
        except RuntimeError as error:
            self.report["initialChatCloseSkipped"] = str(error)

    async def wait_for_brief_receipt(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            matches = [request for request in self.state().get("requests", [])
                       if request.get("id") not in self.baseline_requests and request.get("origin") in {"player", "player-midflight"}
                       and request.get("text") == self.prompt]
            if len(matches) > 1:
                raise RuntimeError("More than one initial player brief was recorded")
            if matches:
                self.request_record = dict(matches[0])
                self.report["initialRequest"] = self.request_record
                return
            ui = await self.request("snapshot")
            if ui.get("chatStatus") == "Failed":
                explanation = next((entry.get("Text") for entry in reversed(ui.get("conversation") or [])
                                    if entry.get("IsPlayer") is False), "No production request receipt")
                raise RuntimeError("Initial design brief rejected: " + str(explanation))
            await asyncio.sleep(self.poll_seconds)
        raise TimeoutError("Initial design brief has no production request receipt; it was not resubmitted")

    def observe_project(self, trace):
        state = self.state()
        if self.request_record is None:
            matches = [request for request in state.get("requests", []) if request.get("id") not in self.baseline_requests
                       and request.get("origin") in {"player", "player-midflight"} and request.get("text") == self.prompt]
            if len(matches) > 1:
                raise RuntimeError("More than one initial player brief was recorded")
            if not matches:
                return None
            self.request_record = dict(matches[0])
            self.report["initialRequest"] = self.request_record
        current_request = next((request for request in state.get("requests", []) if request.get("id") == self.request_record["id"]), self.request_record)
        calls = summarize_trace(initial_turn_trace(trace, self.request_record["id"], self.prompt))["toolCalls"]
        bound, source = bind_goal(state, current_request, self.baseline_goals, calls)
        if self.goal_id is None and bound:
            self.goal_id = bound
            self.report["projectBinding"] = {"goalId": bound, "source": source, "requestId": self.request_record["id"]}
        elif bound and bound != self.goal_id:
            raise RuntimeError("Initial brief changed its project binding")
        goal = next((goal for goal in state.get("goals", []) if goal.get("id") == self.goal_id), None)
        self.record_project(goal)
        return goal

    def record_project(self, goal):
        if goal is not None:
            serialized = json.dumps(goal, ensure_ascii=False, sort_keys=True)
            if serialized != self.last_project:
                append(self.evidence / "creative-farm-project-history.jsonl", {"at": time.time(), "goal": goal})
                self.last_project = serialized
            if design_recorded(goal.get("project")):
                self.report["recordedDesignObserved"] = True
            self.report["project"] = goal

    def completion_turn(self, trace, commands, lease):
        """Keep the actual completion turn, including when its lease already cleared."""
        if self.report.get("completionRequestId"):
            return self.report["completionRequestId"]
        for row in reversed(trace):
            if row.get("_provider") == "agy":
                break
            for call in summarize_trace([row])["toolCalls"]:
                operation = goal_operation(call)
                result_goal = (call.get("result") or {}).get("goal") or {}
                if (operation.get("action") in {"complete", "finish"} and result_goal.get("id") == self.goal_id
                        and result_goal.get("status") == "completed" and not call.get("error")):
                    return row.get("requestId")
        # The raw goal changed during the one current executor, or just after it
        # returned. agy has no per-event request ID; record this observed boundary.
        return (lease or {}).get("requestId") or (commands[-1].get("requestId") if commands else None)

    async def freeze_for_delivery(self):
        ui = await self.request("snapshot")
        if ui.get("actorMode") != "player":
            raise RuntimeError("Final takeover requires the fixture's player actor")
        if ui.get("playerAuthorityAgentOwns") is True:
            await self.request("takeover-key")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ui = await self.request("snapshot")
            sample = await self.native()
            lease_path = self.mod / "data/life-turn.json"
            lease = read(lease_path)
            lease_idle = (isinstance(lease, dict) and lease.get("saveId") == self.save_id
                          and "requestId" in lease and lease["requestId"] is None)
            if not self.dispatched and not lease_path.exists():
                lease_idle = True  # F9 was returned before any provider turn started.
            if ui.get("playerAuthorityAgentOwns") is False and native_finished(sample) and lease_idle:
                self.report["finalTakeover"] = {"purpose": "termination and evidence capture after project monitoring", "ui": ui}
                return sample
            await asyncio.sleep(self.poll_seconds)
        raise TimeoutError("Final takeover did not settle the agent and native action")

    async def save_current(self, stage="final"):
        label = f"creative-{stage}-save-{self.token}"
        path = self.evidence / "lifecycle-request.json"
        await self.wait(lambda: not path.exists(), "Lifecycle request pending")
        write(path, {"label": label, "action": "save-current"})
        receipt = await self.wait(lambda: read(self.evidence / f"lifecycle-{label}.json"), "Final isolated save did not finish", 90)
        if receipt.get("error") or receipt.get("saveName") != self.save_id or receipt.get("action") != "save-current":
            raise RuntimeError("Final native save receipt differs from this isolated farm")
        folder = (self.save_root / self.save_id).resolve()
        if not folder.is_relative_to(self.save_root):
            raise RuntimeError("Final save folder escaped the isolated run")
        files = [{"path": str(path.relative_to(self.run)), "sha256": sha(path), "bytes": path.stat().st_size}
                 for path in sorted(folder.rglob("*")) if path.is_file() and path.resolve().is_relative_to(folder)]
        if not (folder / self.save_id).is_file() or not files:
            raise RuntimeError("Final native save file is missing")
        if stage == "initial":
            initial_folder = self.run / "initial-save" / self.save_id
            shutil.copytree(folder, initial_folder)
            files = [{**item, "path": str(initial_folder.relative_to(self.run) / Path(item["path"]).name)} for item in files]
            folder = initial_folder
        self.report["initialSavedFarm" if stage == "initial" else "savedFarm"] = {"receipt": receipt, "directory": str(folder.relative_to(self.run)), "files": files,
                                    "modelMarkedProjectComplete": (self.report.get("project") or {}).get("status") == "completed"}

    async def monitor(self):
        completion_deadline = None
        while True:
            if self.requested_stop():
                return
            sample = await self.native()
            # Runner.native retains samples for other presets. This preset has
            # the full disk journal below and can run overnight without keeping
            # every farm layout alive in memory as well.
            self.samples.clear()
            self.report["after"] = sample
            append(self.evidence / "creative-farm-samples.jsonl", sample)
            farm = sample.get("creativeFarm") or {}
            if farm.get("error") or farm.get("active") is not True:
                raise RuntimeError("Creative fixture stopped: " + str(farm.get("error") or "inactive"))
            if type(farm.get("agentOwns")) is not bool:
                raise RuntimeError("Creative native snapshot lacks player authority evidence")
            trace, commands = self.trace(), self.commands()
            goal = self.observe_project(trace)
            rejection = blocked_completion(trace, self.goal_id) if goal and goal.get("status") == "active" else None
            if rejection:
                self.report["completionBlocked"] = rejection
                raise RuntimeError("Project completion was rejected: " + str(rejection.get("reasonCode") or rejection))
            lease = read(self.mod / "data/life-turn.json")
            sessions = {row.get("conversationId") for row in commands if row.get("conversationId")}
            self.report["providerSessions"] = sorted(sessions)
            if len(sessions) > 1:
                raise RuntimeError("Creative project changed provider session")
            ledger = rows(self.evidence / "creative-farm-ledger.jsonl")
            checks = world_checks(self.report["before"], sample, self.criteria, ledger)
            if self.report.get("referenceImages"):
                returned = set()
                for call in summarize_trace(trace)["toolCalls"]:
                    args = call.get("input")
                    request = args.get("request") if isinstance(args, dict) else None
                    operation = request.get("read_reference_image") if isinstance(request, dict) else None
                    if (str(call.get("name")).endswith("__consult") and isinstance(operation, dict)
                            and not call.get("error") and not call.get("nonterminal")):
                        if operation.get("image_id") is not None:
                            returned.add(operation["image_id"])
                        else:
                            # The current no-ID contract returns all staged pixels.
                            # AGY may expose successful image output as attachments,
                            # so parsed JSON alone is not a delivery prerequisite.
                            returned.update(item["id"] for item in self.report["referenceImages"])
                checks["selected_reference_images_returned"] = all(item["id"] in returned for item in self.report["referenceImages"])
            checks["same_project_completed"] = bool(goal and goal.get("status") == "completed")
            checks["project_design_recorded"] = bool(goal and design_recorded(goal.get("project"))
                                                      and self.report.get("recordedDesignObserved"))
            if self.criteria.get("requireRawGround"):
                checks["design_precedes_clearing"] = design_precedes_clearing(trace, self.goal_id)
            if checks["same_project_completed"]:
                self.report["completionRequestId"] = self.completion_turn(trace, commands, lease)
                completion_deadline = completion_deadline or time.monotonic() + 60
            else:
                # Reopening the same project resumes work, not its old closing
                # deadline. Only an actual completion is allowed a close timer.
                completion_deadline = None
                self.report.pop("completionRequestId", None)
            checks["completion_round_settled"] = round_settled(lease, commands, self.report.get("completionRequestId"), self.save_id)
            checks["same_provider_session"] = len(sessions) == 1
            checks["real_provider_rounds"] = bool(commands) and all(
                command.get("provider") == (self.report.get("backend") or {}).get("backend") for command in commands)
            self.report["checks"] = checks
            write(self.evidence / "creative-farm-status.json", {"status": "running", "goalId": self.goal_id,
                  "goalStatus": goal.get("status") if goal else None, "completedRounds": len(commands), "checks": checks})
            if checks["same_project_completed"] and checks["completion_round_settled"] and checks["native_action_finished"]:
                # A model's premature completion ends as incomplete evidence;
                # never turn it into success or send a corrective prompt.
                # The completed executor's terminal receipt is sufficient even
                # if normal free-play already opened a follow-up turn. Delivery
                # cancels that turn instead of waiting to catch a brief idle gap.
                # Recheck world/ledger after final takeover: the TestDriver may
                # append its last native receipt on the next game tick.
                self.report["stopReason"] = "project-completed"
                return
            if farm["agentOwns"] is False:
                self.report.update(stopReason="player-takeover", authorityReleasedWhileMonitoring=True)
                return
            if goal and goal.get("status") == "cancelled":
                self.report["stopReason"] = "project-cancelled"
                return
            failed = next((command for command in commands if command.get("status") in {"failed", "cancelled", "error"}), None)
            if failed:
                self.report["failedTurn"] = failed
                raise RuntimeError(f"Provider turn {failed.get('requestId')} ended {failed.get('status')}: {failed.get('error') or ''}")
            if completion_deadline and time.monotonic() >= completion_deadline:
                self.report["stopReason"] = "completed-project-did-not-settle"
                return
            work_count = sum(receipt.get("completedcount", 0) > 0
                             and receipt.get("skillid") in {"clear-debris", "chop-tree", "cut-grass", "place-items", "remove-items", "hoe-tiles"}
                             for row in ledger if row.get("type") == "native-result" for receipt in [lower_keys(row.get("payload"))])
            if (self.progress_capture_count < 4 and time.monotonic() - self.last_progress_capture >= 15 * 60
                    and work_count > self.last_captured_work_count
                    and farm.get("actorLocation") == "Farm"):
                self.last_progress_capture = time.monotonic()
                try:
                    await self.capture(f"progress-{self.progress_capture_count + 1}")
                    self.progress_capture_count += 1
                    self.last_captured_work_count = work_count
                except (RuntimeError, TimeoutError) as error:
                    self.report.setdefault("optionalCaptureSkipped", []).append(str(error))
            # Progress/world captures remain infrequent. Only existing local
            # control, goal and terminal receipts are checked between them.
            await self.wait_for_monitor_signal()

    async def wait_for_monitor_signal(self):
        deadline = time.monotonic() + self.monitor_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
            control = read(self.evidence / "creative-farm-control.json") or {}
            if control.get("runId") == self.run.name and (control.get("error") or control.get("active") is False
                                                          or control.get("agentOwns") is False):
                return
            if self.requested_stop():
                return
            goals = self.state().get("goals", [])
            if any(goal.get("id") not in self.baseline_goals and goal.get("status") in {"completed", "cancelled"}
                   for goal in goals if self.goal_id is None or goal.get("id") == self.goal_id):
                return
            if any(command.get("status") in {"failed", "cancelled", "error"} for command in self.commands()):
                return

    def requested_stop(self):
        request = read(self.evidence / "reviewer-stop.json")
        if (isinstance(request, dict) and request.get("runId") == self.run.name
                and request.get("reviewerRequestedStop") is True):
            self.report.update(stopReason="reviewer-stop", reviewerStopRequest=request)
            return True
        return False

    def write_design(self):
        history = [{"action": goal_operation(call).get("action"), "project": goal.get("project")}
                   for call in (self.report.get("traceSummary") or {}).get("toolCalls", [])
                   for goal in [((call.get("result") or {}).get("goal") or {}) if isinstance(call.get("result"), dict) else {}]
                   if goal.get("id") == self.goal_id and goal_operation(call) and goal.get("project")]
        design = {"designBrief": self.report.get("designBrief"), "projectBinding": self.report.get("projectBinding"),
                  "goal": self.report.get("project"), "actualLayout": (self.report.get("after", {}).get("creativeFarm") or {}).get("layout"),
                  "projectHistory": history, "captures": self.report.get("captures"), "humanReview": self.report["humanReview"],
                  "savedFarm": self.report.get("savedFarm")}
        write(self.evidence / "creative-farm-design.json", design)
        lines = ["# 自主装修实景设计说明", "", str(design["designBrief"] or ""), "",
                 "以下保留模型完整项目记录；真实布局、全农场截图与隔离存档另存于同目录的 JSON 报告中。",
                 "", "## 初始方案与施工调整", "", "```json", json.dumps(history, ensure_ascii=False, indent=2), "```", "",
                 "## 最终项目记录", "", "```json", json.dumps(design["goal"], ensure_ascii=False, indent=2), "```", "",
                 "## 实际农场", ""]
        for name, capture in (design["captures"] or {}).items():
            label = {"before": "开工前", "final": "本轮结束时"}.get(name, name)
            lines.extend([f"### {label}", "", f"![{label}]({capture['artifact']})", ""])
        lines.extend(["## 评审", "", "整体美感、各区衔接与风格由玩家查看实景图评审；技术检查见 creative-farm-report.json。", ""])
        (self.evidence / "creative-farm-design.md").write_text("\n".join(lines), encoding="utf-8")

    async def deliver(self):
        """Preserve the actual work on completion, an explicit stop, or error."""
        after = await self.freeze_for_delivery()
        self.report["after"] = after
        checks = self.report["checks"]
        checks.update(world_checks(self.report["before"], after, self.criteria, rows(self.evidence / "creative-farm-ledger.jsonl")))
        if self.goal_id is not None:
            goal = next((goal for goal in self.state().get("goals", []) if goal.get("id") == self.goal_id), None)
            self.record_project(goal)
            checks["same_project_completed"] = bool(goal and goal.get("status") == "completed")
            checks["project_design_recorded"] = bool(goal and design_recorded(goal.get("project"))
                                                      and self.report.get("recordedDesignObserved"))
        capture_error = None
        try:
            capture = await self.capture("final")
            checks["captured_layout_matches_final"] = capture["layout"] == (after.get("creativeFarm") or {}).get("layout")
        except Exception as error:  # noqa: BLE001 - A failed image must not discard the native saved work.
            capture_error = error
            checks["captured_layout_matches_final"] = False
            self.report["captureError"] = f"{type(error).__name__}: {error}"
        checks["before_and_final_real_images"] = all(key in self.report.get("captures", {}) for key in ("before", "final"))
        await self.save_current()
        checks["final_isolated_save_written"] = True
        if capture_error is not None:
            raise RuntimeError("Final capture failed; the isolated farm was still saved") from capture_error
        # Native close-ups of actual built fixtures, after saving and takeover.
        # Framing never supplies construction coordinates or edits the image.
        for name, region in detail_regions(after.get("creativeFarm") or {}):
            try:
                await self.capture("detail-" + name, region)
            except (RuntimeError, TimeoutError) as error:
                self.report.setdefault("optionalCaptureSkipped", []).append(str(error))

    async def execute(self):
        started = time.time()
        try:
            await self.prepare()
            await self.monitor()
            self.report["status"] = ("stopped" if self.report.get("stopReason") in {
                "player-takeover", "reviewer-stop", "project-cancelled"} else "incomplete")
        except Exception as error:  # noqa: BLE001 - Always preserve evidence and release the owned run.
            self.report.update(status="error" if self.dispatched else "setup-error", error=f"{type(error).__name__}: {error}")
        finally:
            if self.handoff_attempted and not self.dispatched:
                try:
                    async with asyncio.timeout(60):
                        await self.freeze_for_delivery()
                except Exception as error:  # noqa: BLE001 - Retain the setup failure and failed authority cleanup separately.
                    self.report["cleanupError"] = f"{type(error).__name__}: {error}"
            if self.dispatched and hasattr(self, "save_root") and "before" in self.report:
                try:
                    async with asyncio.timeout(180):
                        await self.deliver()
                    if not self.report.get("error") and self.report.get("stopReason") == "project-completed" and all(self.report["checks"].values()):
                        self.report["status"] = "passed"
                    elif self.report.get("stopReason") == "project-completed":
                        self.report["stopReason"] = "completed-project-misses-native-criteria"
                except Exception as error:  # noqa: BLE001 - Retain the original failure as well as cleanup evidence.
                    self.report.update(status="error", deliveryError=f"{type(error).__name__}: {error}")
            self.report.update(dispatched=self.dispatched, elapsedSeconds=round(time.time() - started, 1),
                               handoffAttempted=self.handoff_attempted, technicalPassed=self.report["status"] == "passed")
            try:
                self.report["rounds"] = self.commands() if hasattr(self, "save_id") else []
                self.report["contextUsage"] = rows(self.mod / "data/context-usage.jsonl")[self.usage_offset:]
                self.report["traceSummary"] = summarize_trace(self.trace())
                ledger = self.evidence / "creative-farm-ledger.jsonl"
                self.report["nativeLedger"] = {"path": ledger.name, "sha256": sha(ledger) if ledger.exists() else None}
                self.write_design()
                write(self.evidence / "creative-farm-report.json", self.report)
                write(self.evidence / "creative-farm-trace.json", self.trace())
                lines = ["# 自主装修技术验收", "", f"技术状态：{self.report['status']}", "",
                         f"结束原因：{self.report.get('stopReason', self.report.get('error', self.report.get('deliveryError', '')))}", "",
                         "| 技术检查 | 结果 |", "| --- | --- |"]
                lines.extend(f"| {key} | {'通过' if value else '未满足'} |" for key, value in self.report["checks"].items())
                lines.extend(["", "审美评审：待玩家查看。请打开 creative-farm-design.md 及开工前／结束时的实景图。技术通过不替代审美评价。", ""])
                (self.evidence / "creative-farm-report.md").write_text("\n".join(lines), encoding="utf-8")
            finally:
                write(self.evidence / "free-mode-control-done.json", {"success": self.report["status"] == "passed",
                      "taskSuccess": self.report["status"] == "passed", "humanReviewPending": True,
                      "reason": self.report.get("stopReason", self.report.get("error"))})
            print(json.dumps({"status": self.report["status"], "report": str(self.evidence / "creative-farm-report.json")}, ensure_ascii=False), flush=True)
        return 0 if self.report["status"] == "passed" else 2 if self.report["status"] == "stopped" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--case", choices=["creative-farm"], default="creative-farm")
    parser.add_argument("--cases-file", type=Path)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(CreativeRunner(args.run_dir, args.case, args.cases_file).execute()))
