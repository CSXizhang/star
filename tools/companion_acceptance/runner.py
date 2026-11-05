"""Repeatable real-game acceptance. TestDriver arranges; the real model acts.

No MCP action calls, work-store edits, model-output completion heuristics, or
retries of failed cases. All output stays in the isolated run's evidence folder.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def rows(path):
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except ValueError:
            continue  # A writer can still be appending the final line.
    return result


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def count(items, item_id):
    return sum(item.get("stack", 0) for item in items or [] if item.get("id") == item_id)


def summarize_trace(trace):
    if any(row.get("_provider") == "agy" for row in trace):
        return summarize_agy_trace(trace)
    calls = []
    messages = 0
    for row in trace:
        item = (row.get("event") or {}).get("item") or {}
        messages += item.get("type") == "agent_message"
        if item.get("type") != "tool_call":
            continue
        call = item.get("toolCall") or {}
        output = call.get("output") or {}
        text = "\n".join(c.get("text", "") for c in output.get("content", []) if isinstance(c, dict))
        details = output.get("details") or {}
        result = (details.get("mcp") or {}).get("structuredContent")
        if result is None:
            try:
                result = json.loads(text.removeprefix("[MCP structured content]\n"))
            except ValueError:
                result = None
        calls.append({"name": call.get("name"), "input": call.get("input"), "result": result,
                      "error": call.get("status") == 3 or bool(details.get("is_error")) or text.startswith("Error executing tool"),
                      "nonterminal": "IN_PROGRESS" in text})
    return {"toolCalls": calls, "toolCount": len(calls), "errorCount": sum(c["error"] for c in calls),
            "publicMessages": messages}


def decoded(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


def summarize_agy_trace(trace):
    """Pair single-call planner steps with native transcript receipts; ambiguity stays unknown."""
    calls, pending = [], None
    messages = 0
    for row in trace:
        if row.get("type") == "PLANNER_RESPONSE":
            pending = None
            planned = row.get("tool_calls") or []
            messages += bool(row.get("content")) and not planned
            for call in planned:
                args = call.get("args") or {}
                mcp = call.get("name") == "call_mcp_tool"
                record = {"name": (f"mcp__{decoded(args.get('ServerName'))}__{decoded(args.get('ToolName'))}"
                                   if mcp else call.get("name")),
                          "input": decoded(args.get("Arguments")) if mcp else args,
                          "result": None, "error": False, "nonterminal": True,
                          "step": row.get("step_index"), "conversationId": row.get("conversationId")}
                calls.append(record)
                if len(planned) == 1:
                    pending = record
        elif row.get("type") == "GENERIC" and pending is not None:
            content = row.get("_externalOutput", {}).get("text") or row.get("content") or ""
            body = "\n".join(line for line in content.splitlines()
                             if not line.startswith(("Created At:", "Completed At:"))).strip()
            result = decoded(body)
            pending.update(result=result if isinstance(result, dict) else None,
                           error=row.get("status") in {"ERROR", "FAILED"} or "Error executing tool" in body,
                           nonterminal=row.get("status") not in {"DONE", "ERROR", "FAILED"} or "IN_PROGRESS" in body,
                           resultStep=row.get("step_index"))
            pending = None
        elif row.get("type") == "USER_INPUT":
            pending = None
    return {"toolCalls": calls, "toolCount": len(calls), "errorCount": sum(c["error"] for c in calls),
            "publicMessages": messages}


def attach_agy_output(row):
    """Archive only the provider-owned output named by this exact session and step."""
    content = row.get("content") or ""
    marker = "The output was large and was saved to: "
    cid, step = row.get("conversationId"), row.get("step_index")
    inline_truncated = re.search(r"<truncated \d+ bytes>", content) is not None
    if (marker not in content and not inline_truncated) or not isinstance(cid, str) or not cid or type(step) is not int:
        return row
    if any(c not in "0123456789abcdefABCDEF-" for c in cid):
        return row
    expected = Path.home() / ".gemini/antigravity-cli/brain" / cid / ".system_generated/steps" / str(step) / "output.txt"
    uri = expected.as_uri()
    if marker in content:
        uri = content.split(marker, 1)[1].strip()
        parsed = urlsplit(uri)
        path = unquote(parsed.path)
        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        if parsed.scheme != "file" or parsed.netloc or Path(path).resolve() != expected.resolve():
            return row
    try:
        raw = expected.read_bytes()
        text = raw.decode("utf-8-sig")
        if inline_truncated and not isinstance(json.loads(text), dict):
            return row
    except (OSError, UnicodeError, ValueError):
        return row
    return {**row, "_externalOutput": {"source": uri, "sha256": hashlib.sha256(raw).hexdigest(),
                                      "text": text}}


def provider_trace(mod):
    backend = (read(mod / "config/chat-backend.json") or {}).get("backend", "mcode")
    if backend != "agy":
        return rows(mod / "data/mcode-public-events.jsonl")
    ids = dict.fromkeys(r.get("conversationId") for r in rows(mod / "data/agy-turns.jsonl"))
    result = []
    for cid in ids:
        if not isinstance(cid, str) or not cid or any(c not in "0123456789abcdefABCDEF-" for c in cid):
            continue
        path = Path.home() / ".gemini/antigravity-cli/brain" / cid / ".system_generated/logs/transcript.jsonl"
        result.extend(attach_agy_output({**row, "_provider": "agy", "conversationId": cid}) for row in rows(path))
    return result


async def verify_agy_binding(run, mod, before):
    if (read(mod / "config/chat-backend.json") or {}).get("backend") != "agy":
        return None
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    binding = read(run / "evidence/agy-game-binding.json")
    if not binding:
        raise RuntimeError("Missing isolated agy game-service binding")
    params = StdioServerParameters(command=binding["command"], args=binding["args"],
                                   env={**os.environ, **binding["env"]})
    async with asyncio.timeout(45), stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        result = await session.call_tool("get_status", {"detail": True})
        status = result.structuredContent
        if not isinstance(status, dict):
            status = decoded("\n".join(c.text for c in result.content if hasattr(c, "text")))
    expected = (read(run / "evidence/harness-fixture-startup.json") or {}).get("saveName")
    if result.isError or not expected or not isinstance(status, dict) or status.get("saveId") != expected:
        detail = "\n".join(c.text for c in result.content if hasattr(c, "text"))
        raise RuntimeError("Isolated agy MCP observation does not match the native save: " + detail[:400])
    proof = {"serverName": binding["serverName"], "saveId": expected, "passed": True}
    write(run / "evidence/agy-binding-verified.json", proof)
    return proof


def native_finished(sample):
    # ActiveMachine is null when the coordinator has no executing skill.
    return all(key in sample for key in ("companionMachineExecuting", "companionActiveTaskId", "companionRestState")) and (
        sample["companionMachineExecuting"] in (None, False)
        and sample["companionActiveTaskId"] is None and sample["companionRestState"] == "awake")


def evaluate(case, before, after, samples):
    """Fail closed on missing native evidence; never infer success from prose."""
    target_count = before.get("harnessFixtureTargetCount")
    checks = {
        "targets_preserved": bool(target_count) and after.get("harnessFixtureTargets") is not None
        and [(x["x"], x["y"]) for x in before.get("harnessFixtureTargets", [])]
        == [(x["x"], x["y"]) for x in after.get("harnessFixtureTargets", [])],
        "same_game_day": all(after.get(k) == before.get(k) and k in before for k in ("year", "season", "day")),
        "native_action_finished": native_finished(after),
    }
    if case in {"walk-pickup", "walk-pickup-full"}:
        checks.pop("targets_preserved")
        checks["ground_drop_matches_capacity"] = after.get("acceptanceGroundDropRemaining") is (case == "walk-pickup-full")
        checks["wood_in_backpack"] = count(after.get("companionItems"), "(O)388") == (0 if case == "walk-pickup-full" else 3)
    elif case == "mature-trees":
        checks.pop("targets_preserved")
        checks["two_mature_trees_cut"] = before.get("acceptanceMatureTreesRemaining") == 2 and after.get("acceptanceMatureTreesRemaining") == 0
        checks["sapling_preserved"] = before.get("acceptanceSaplingStage") == after.get("acceptanceSaplingStage") == 2
        checks["wood_stored"] = count(after.get("acceptanceChestItems"), "(O)388") > 0 and count(after.get("companionItems"), "(O)388") == 0
    elif case in {"water-refill", "rest-water"}:
        checks["all_target_crops_watered"] = bool(target_count) and (
            after.get("harnessFixtureDryCropCount") == 0
            and after.get("harnessFixtureCropCount") == target_count
            and after.get("harnessFixtureWateredCount") == target_count
        )
        if case == "water-refill":
            checks["native_water_refilled"] = any(
                (s.get("companionWater") or 0) > (before.get("companionWater") or 0) for s in samples
            )
        else:
            checks["rested_on_real_bed"] = any(s.get("companionOnRealBed") is True for s in samples)
            checks["recovered_then_worked_without_full_recovery"] = (
                60 <= (after.get("companionStamina") or 0) < 100
                and any((s.get("companionStamina") or 0) >= 80 for s in samples)
            )
    else:
        checks["harvested_all_targets"] = bool(target_count) and after.get("harnessFixtureCropCount") == 0
        checks["harvest_in_chest"] = count(after.get("acceptanceChestItems"), "(O)24") == target_count
        checks["wood_in_chest"] = count(after.get("acceptanceChestItems"), "(O)388") == 3
        checks["ground_drop_collected"] = after.get("acceptanceGroundDropRemaining") is False
        checks["backpack_unloaded"] = after.get("companionItems") is not None and all(
            count(after["companionItems"], item_id) == 0 for item_id in ("(O)24", "(O)388")
        )
    return checks


class Runner:
    def __init__(self, run, case, timeout, cases_file=None, *, keep_open=False, reset_fixture=False):
        self.run = run.resolve()
        self.evidence = self.run / "evidence"
        self.mod = self.run / "mods" / "StardewAI.Companion.Mod"
        self.case, self.timeout = case, timeout
        self.cases_file = cases_file or Path(__file__).with_name("cases.json")
        self.token = uuid.uuid4().hex[:8]
        self.keep_open, self.reset_fixture = keep_open, reset_fixture
        self.number = 0
        self.samples = []
        self.report = {"case": case, "status": "error", "checks": {}, "realGame": True}

    async def wait(self, predicate, message, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(.5)
        raise TimeoutError(message)

    async def request(self, action, **params):
        self.number += 1
        label = f"preset-{self.token}-{self.number}"
        path = self.evidence / "life-ui-request.json"
        await self.wait(lambda: not path.exists(), "TestDriver request still pending")
        write(path, {"label": label, "action": action, **params})
        result = await self.wait(lambda: read(self.evidence / f"life-ui-{label}.json"), f"TestDriver {action} did not respond")
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    async def native(self):
        self.number += 1
        label = f"preset-{self.token}-{self.number}"
        path = self.evidence / "free-mode-native-snapshot-request.txt"
        await self.wait(lambda: not path.exists(), "Native snapshot request still pending")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(label, encoding="utf-8")
        tmp.replace(path)
        value = await self.wait(lambda: read(self.evidence / f"free-mode-native-{label}.json"), "No native snapshot")
        if value.get("runId") != self.run.name or not value.get("companionObserved"):
            raise RuntimeError("Native observation belongs to another run or companion is absent")
        self.samples.append(value)
        return value

    async def execute(self):
        started = time.time()
        try:
            coord = await self.wait(lambda: read(self.evidence / "harness-coordination.json"), "No isolated game", 180)
            await self.wait(lambda: (read(self.evidence / "harness-coordination.json") or {}).get("state") == "READY_FOR_ACT", "Isolated fixture not ready", 180)
            if coord.get("scenario") != "free-mode-control" or coord.get("runId") != self.run.name:
                raise RuntimeError("Runner requires its own isolated free-mode-control game")
            if self.reset_fixture:
                spec = read(self.cases_file)[self.case]
                await self.request("close")
                await self.request("harness-fixture", mode=spec.get("fixture", self.case), stamina=spec["stamina"], water=spec["water"])
                fixture_file = max(self.evidence.glob("harness-fixture-preset-*.json"), key=lambda p: p.stat().st_mtime)
                fixture = read(fixture_file)
            else:
                fixture = read(self.evidence / "harness-fixture-startup.json") or {}
            if fixture.get("mode") != read(self.cases_file)[self.case].get("fixture", self.case):
                raise RuntimeError("Startup fixture does not match requested case")
            actual_saves = Path(fixture["isolatedSavesPath"]).resolve()
            if not actual_saves.is_relative_to(self.run):
                raise RuntimeError("Save root is outside this isolated run")
            self.report["fixture"] = fixture
            spec_path = self.cases_file
            spec = read(spec_path)[self.case]
            self.report["caseDefinitionSha256"] = hashlib.sha256(spec_path.read_bytes()).hexdigest()
            dll = self.mod / "StardewAI.Companion.Mod.dll"
            self.report["modSha256"] = hashlib.sha256(dll.read_bytes()).hexdigest()
            self.report["backend"] = read(self.mod / "config" / "chat-backend.json")
            # Only normal chat/menu input; no test-side action dispatch.
            await self.request("close")
            before = await self.native()
            self.report["before"] = before
            self.report["agyBindingCheck"] = (read(self.evidence / "agy-binding-verified.json") if self.reset_fixture
                                              else await verify_agy_binding(self.run, self.mod, before))
            if self.report["backend"]["backend"] == "agy" and not self.report["agyBindingCheck"]:
                raise RuntimeError("Missing initial agy binding proof for continued suite")
            await self.request("harness-observe-contracts")
            probe_path = max(self.evidence.glob("observation-contract-preset-*.json"), key=lambda p: p.stat().st_mtime)
            probe = read(probe_path)
            self.report["observationContract"] = probe
            if spec.get("fixture", self.case) in {"harvest-store", "mature-trees", "walk-pickup", "walk-pickup-full"}:
                player = before.get("playerTile") or {}
                targets = fixture.get("nativeReachability") or []
                if before.get("playerLocation") != "Farm" or not targets or any(
                    (player.get("x", -1000) - t["target"]["x"]) ** 2
                    + (player.get("y", -1000) - t["target"]["y"]) ** 2 < 100 for t in targets):
                    raise RuntimeError("Observer did not arrive outside the task area; refuse a blocked or pre-collected fixture")
            instruction = spec["instruction"]
            self.report["instruction"] = instruction
            deadline = time.monotonic() + self.timeout
            while True:
                ui = await self.request("snapshot")
                if ui.get("productionChatConnected") and ui.get("chatStatus") in {"Idle", "Completed", "Failed"}:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Production bridge did not become ready")
                await asyncio.sleep(1)
            command_offset = len(rows(self.mod / "chat_commands.jsonl"))
            trace_offset = len(provider_trace(self.mod))
            self.report["traceOffset"] = trace_offset
            await self.request("open-conversation")
            await self.request("text", field="_input", text=instruction)
            await self.request("key", key="Enter")
            await self.request("close")
            action_started = time.time()
            self.report["setupSeconds"] = round(action_started - started, 1)
            deadline = time.monotonic() + self.timeout
            terminal = None
            guidance_sent = False
            while time.monotonic() < deadline:
                sample = await self.native()
                if spec.get("duringAction") and not guidance_sent and sample.get("companionMachineExecuting"):
                    await self.request("open-conversation")
                    await self.request("text", field="_input", text=spec["duringAction"])
                    await self.request("key", key="Enter")
                    await self.request("close")
                    guidance_sent = True
                    self.report["guidanceSentAt"] = time.time()
                new_commands = rows(self.mod / "chat_commands.jsonl")[command_offset:]
                terminal = next((r for r in new_commands if r.get("prompt") == instruction), None)
                if terminal:
                    break
                await asyncio.sleep(1)
            if not terminal:
                raise TimeoutError("Model round did not finish within the case budget")
            self.report["turn"] = terminal
            self.report["taskSeconds"] = round(time.time() - action_started, 1)
            await self.request("close")
            await self.request("harness-capture")
            after = await self.native()
            self.report["after"] = after
            checks = evaluate(spec.get("fixture", self.case), before, after, self.samples)
            activities = probe.get("activities") or {}
            checks["companion_public_observation_uses_own_actor"] = (
                activities.get("actorMode") == "companion" and activities.get("actorTile") == probe.get("actorTile")
                and activities.get("playerTile") is None and activities.get("playerInteractionSupported") is False
                and activities.get("nativeUi") is None and activities.get("menuChoices") is None
            )
            checks["mailbox_blocked_in_observation_and_planning"] = (
                probe.get("mailboxPassable") is False and probe.get("mailboxEdgePassable") is False
            )
            if spec.get("then"):
                self.report["intermediate"] = after
                await self.request("open-conversation")
                await self.request("text", field="_input", text=spec["then"])
                await self.request("key", key="Enter")
                await self.request("close")
                followup = None
                while time.monotonic() < deadline:
                    await self.native()
                    followup = next((r for r in rows(self.mod / "chat_commands.jsonl")[command_offset:]
                                     if r.get("prompt") == spec["then"]), None)
                    if followup:
                        break
                    await asyncio.sleep(1)
                after = await self.native()
                self.report["after"] = after
                checks["shipping_round_completed"] = bool(followup and followup.get("status") == "completed")
                checks["harvest_shipped"] = count(after.get("acceptanceShippedItems"), "(O)24") - count(before.get("acceptanceShippedItems"), "(O)24") == before.get("harnessFixtureTargetCount")
                checks["wood_retained"] = count(after.get("acceptanceChestItems"), "(O)388") == 3
                checks["shipping_native_finished"] = native_finished(after)
            if self.case == "rest-forestry":
                checks["rested_to_target"] = any(s.get("companionOnRealBed") is True for s in self.samples) and any((s.get("companionStamina") or 0) >= 80 for s in self.samples)
                checks["worked_before_full_recovery"] = (after.get("companionStamina") or 0) < 100
            checks["real_provider_round_completed"] = terminal.get("provider") == self.report["backend"]["backend"] and terminal.get("status") == "completed"
            trace = provider_trace(self.mod)[trace_offset:]
            summary = summarize_trace(trace)
            self.report["traceSummary"] = summary
            rest_results = [(c.get("result") or {}).get("details") or {} for c in summary["toolCalls"]
                            if "rest" in ((c.get("input") or {}).get("action") or {})]
            if self.case in {"rest-forestry", "rest-water"}:
                checks["rest_receipt_has_final_location"] = bool(rest_results) and all(
                    r.get("finalLocation") == "FarmHouse" and isinstance(r.get("finalTile"), dict)
                    and isinstance(r["finalTile"].get("x"), int) and isinstance(r["finalTile"].get("y"), int)
                    for r in rest_results)
            if self.case == "harvest-logistics":
                self.report["extraClearingCalls"] = sum(
                    "clear_debris" in ((c.get("input") or {}).get("action") or {})
                    for c in summary["toolCalls"])
                # This fixture provides a reachable chest and shipping route.
                checks["no_unrequested_clearing"] = self.report["extraClearingCalls"] == 0
            if spec.get("duringAction"):
                checks["player_message_delivered_at_tool_boundary"] = guidance_sent and any(
                    spec["duringAction"] in json.dumps(c.get("result"), ensure_ascii=False)
                    for c in summary["toolCalls"])
            operations = {"water_auto", "water_zone", "water_tiles", "refill_watering_can", "harvest_auto", "harvest_crop", "chop_tree",
                          "pickup_items", "deposit_to_chest", "withdraw_from_chest", "ship_items", "rest", "navigate_to"}
            actions = [c for c in summary["toolCalls"] if str(c["name"]).endswith("__submit_work")
                       and operations.intersection((c.get("input") or {}).get("action") or {})]
            checks["real_action_tools_observed"] = any(not c["error"] and isinstance(c["result"], dict) for c in actions)
            checks["action_calls_returned_terminal_results"] = bool(actions) and all(
                not c["nonterminal"] and (c["error"] or isinstance(c["result"], dict)
                    and (c["result"].get("status") in {"executed", "completed", "failed", "cancelled", "rejected", "player_update", "world_update"}
                         or c["result"].get("status") == "no-work" and c["result"].get("targetCount") == 0
                         or c["result"].get("terminalState") in {"succeeded", "failed", "cancelled"})) for c in actions
            )
            if self.case in {"walk-pickup", "walk-pickup-full"}:
                checks["no_explicit_pickup_tool"] = not any("pickup_items" in str(c.get("input")) for c in summary["toolCalls"])
            frames = after.get("animationFrames") or {}
            checks["walking_animation_changes_in_same_direction"] = any(
                key.startswith("walking-facing:") and len(set(value)) >= 2 for key, value in frames.items()
            )
            checks["no_solid_overlap_while_moving"] = (after.get("movementSamples") or 0) > 0 and after.get("solidOverlaps") == []
            self.report["checks"] = checks
            self.report["status"] = "passed" if all(checks.values()) else "failed"
        except Exception as error:  # noqa: BLE001 - preserve evidence and close the isolated game on any failure.
            self.report["error"] = f"{type(error).__name__}: {error}"
        finally:
            self.report["elapsedSeconds"] = round(time.time() - started, 1)
            self.report["samples"] = self.samples
            trace = provider_trace(self.mod)[self.report.get("traceOffset", 0):]
            write(self.evidence / "preset-trace.json", trace)
            self.report["traceEvents"] = len(trace)
            write(self.evidence / "preset-report.json", self.report)
            failed = [name for name, passed in self.report["checks"].items() if not passed]
            (self.evidence / "preset-report.md").write_text(
                f"# {self.case}: {self.report['status']}\n\n耗时 {self.report['elapsedSeconds']} 秒；公开 trace {len(trace)} 条。\n\n"
                + "\n".join(f"- {'通过' if ok else '失败'}：{name}" for name, ok in self.report["checks"].items())
                + f"\n\n{self.report.get('error', '')}\n\n证据：preset-report.json、preset-trace.json。\n",
                encoding="utf-8",
            )
            print(json.dumps({"status": self.report["status"], "failed": failed, "error": self.report.get("error"), "report": str(self.evidence / "preset-report.md")}, ensure_ascii=False), flush=True)
            # Native TestDriver exit, not a broad process kill. Save copies remain.
            if not self.keep_open:
                write(self.evidence / "free-mode-control-done.json", {"success": self.report["status"] == "passed"})
        return 0 if self.report["status"] == "passed" else 1


async def run_suite(run, cases, timeout, cases_file):
    import shutil
    reports = []
    try:
        for index, case in enumerate(cases):
            runner = Runner(run, case, timeout, cases_file, keep_open=True, reset_fixture=index > 0)
            code = await runner.execute()
            reports.append(runner.report)
            folder = run / "evidence" / case
            folder.mkdir(exist_ok=True)
            for filename in ("preset-report.json", "preset-report.md", "preset-trace.json"):
                shutil.copy2(run / "evidence" / filename, folder / filename)
            write(run / "summary.json", reports)
            if code and not (native_finished(runner.report.get("after") or {})
                             and (runner.report.get("turn") or {}).get("status") == "completed"):
                break  # Archive a completed failed case, but never reset an unresolved action.
    finally:
        success = len(reports) == len(cases) and all(r["status"] == "passed" for r in reports)
        write(run / "evidence/free-mode-control-done.json", {"success": success})
        (run / "summary.md").write_text("# 伙伴实机回归\n\n一次启动、同一存档和会话，场景间只重置测试布置。\n\n"
            + "\n".join(f"- {r['case']}: {r['status']} ({r['elapsedSeconds']} 秒)" for r in reports), encoding="utf-8")
    return 0 if success else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--case", choices=list(read(Path(__file__).with_name("cases.json"))))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--cases-file", type=Path, help="Frozen suite definitions")
    parser.add_argument("--suite", nargs="+")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run_suite(args.run_dir, args.suite, args.timeout, args.cases_file) if args.suite else Runner(args.run_dir, args.case, args.timeout, args.cases_file).execute()))
