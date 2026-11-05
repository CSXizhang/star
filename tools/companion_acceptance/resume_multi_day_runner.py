"""Resume an interrupted isolated evaluation through the normal game controls."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from multi_day_runner import MultiDayRunner, sha, total
from runner import provider_trace, read, rows, verify_agy_binding, write

RESUME_TEXT = "继续任务。"


def resume_preflight(sample, fixture, sessions, previous):
    errors = []
    current = sample.get("multiDay") or {}
    expected = previous["after"]["multiDay"]
    expected_sessions = set(previous["providerSessionTimeline"][-1]["sessions"])
    if set(sessions.values()) != expected_sessions or len(expected_sessions) != 1:
        errors.append("Original provider session binding changed")
    if tuple(sample.get(k) for k in ("year", "season", "day")) != tuple(previous["after"].get(k) for k in ("year", "season", "day")):
        errors.append("Loaded date differs from the saved evaluation")
    if sample.get("timeOfDay", -1) < previous["after"].get("timeOfDay", 0):
        errors.append("Loaded clock moved backwards")
    if current.get("money") != expected.get("money"):
        errors.append("Saved money differs")
    chest = fixture["chest"]
    current_chest = next((x for x in current.get("chests", []) if x.get("tile") == chest), {})
    expected_chest = next((x for x in expected.get("chests", []) if x.get("tile") == chest), {})
    ids = {x.get("id") for x in current_chest.get("items", []) + expected_chest.get("items", [])}
    if not current_chest or not expected_chest or any(total(current_chest.get("items"), item) != total(expected_chest.get("items"), item) for item in ids):
        errors.append("Saved chest resources differ")
    trees = {x.get("identity"): x for x in current.get("trees", [])}
    protected = fixture.get("protectedTrees") or []
    if not protected or any(x["identity"] not in trees or trees[x["identity"]].get("stump") for x in protected):
        errors.append("Original protected tree identity lost")
    if current.get("crops") != expected.get("crops") or current.get("shippedItems") != expected.get("shippedItems"):
        errors.append("Saved crops or settled shipping differ")
    if current.get("actorLocation") != expected.get("actorLocation"):
        errors.append("Loaded actor location changed")
    if current.get("harnessTimeFrozen") is not False:
        errors.append("Harness clock frozen")
    return {"passed": not errors, "errors": errors, "expectedSessions": sorted(expected_sessions)}


class ResumeMultiDayRunner(MultiDayRunner):
    def __init__(self, run, spec_path, timeout, segment):
        super().__init__(run, spec_path, timeout)
        self.report_dir = Path(segment).resolve()
        if not self.report_dir.is_relative_to(self.ev.resolve()):
            raise ValueError("Resume report directory escaped isolated evidence")
        self.previous = read(self.report_dir / "previous-multi-day-report.json")
        if not self.previous:
            raise ValueError("Archived original report is required")
        self.report.update({"scope": "quota-interrupted save/session continuation",
                            "uninterruptedSample": False, "resumeControlText": None,
                            "priorTaskSeconds": (self.previous.get("priorTaskSeconds") or 0) + (self.previous.get("taskSeconds") or 0),
                            "resumeLineage": read(self.report_dir / "resume-lineage.json")})

    async def prepare(self):
        await self.r.wait(lambda: (read(self.ev / "harness-coordination.json") or {}).get("state") == "READY_FOR_ACT",
                          "No resumed isolated world", 240)
        cfg = read(self.run / "config/test-session.json") or {}
        save = Path(cfg.get("isolatedSavesPath", "")).resolve()
        if cfg.get("runId") != self.run.name or not save.is_relative_to(self.run):
            raise ValueError("Invalid isolated resume configuration")
        before = await self.native()
        self.report["before"] = before
        fixture = read(self.ev / "harness-fixture-startup.json")
        sessions = read(self.r.mod / "chat_sessions.json") or {}
        validation = resume_preflight(before, fixture, sessions, self.previous)
        self.report["resumePreflight"] = validation
        self.report["fixture"] = fixture
        self.report["saveHashes"] = {str(p.relative_to(save)): sha(p) for p in save.rglob("*") if p.is_file()}
        if not self.report["saveHashes"]:
            raise ValueError("No saved game hashes")
        origin = read(self.ev / "multi-day-report.json") or {}
        expected_spec_hash = self.previous.get("specSha256") or origin.get("specSha256")
        expected_dll_hash = self.previous.get("productionDllSha256") or origin.get("productionDllSha256")
        self.report.update({"specSha256": sha(self.spec_path),
                            "productionDllSha256": sha(self.r.mod / "StardewAI.Companion.Mod.dll"),
                            "testDriverSha256": sha(self.run / "mods/StardewAI.Companion.TestDriver/StardewAI.Companion.TestDriver.dll")})
        if self.report["specSha256"] != expected_spec_hash:
            validation["errors"].append("Original task specification changed")
        if self.report["productionDllSha256"] != expected_dll_hash:
            validation["errors"].append("Production DLL changed")
        validation["passed"] = not validation["errors"]
        write(self.report_dir / "multi-day-preflight.json", self.report)
        if not validation["passed"]:
            raise ValueError("Resume preflight rejected: " + "; ".join(validation["errors"]))
        self.report["agyBindingCheck"] = await verify_agy_binding(self.run, self.r.mod, before)
        ui = await self.r.request("snapshot")
        deadline = time.monotonic() + 120
        while not ui.get("productionChatConnected") or ui.get("chatStatus") not in ("Idle", "Completed", "Failed"):
            if time.monotonic() > deadline:
                raise TimeoutError("Resumed bridge not ready")
            await asyncio.sleep(1)
            ui = await self.r.request("snapshot")
        if ui.get("actorMode") != "player":
            raise ValueError("Expected player mode")
        offset = len(rows(self.r.mod / "chat_commands.jsonl"))
        self.report["traceOffset"] = len(provider_trace(self.r.mod))
        self.report["usageOffset"] = len(rows(self.r.mod / "data/context-usage.jsonl"))
        await self.restore_controls(ui, fixture["saveName"])
        write(self.report_dir / "multi-day-preflight.json", self.report)
        return offset

    def pause_flags(self, save_id):
        flags = {}
        for name in ("work-state", "autonomy-state"):
            value = (read(self.r.mod / "data" / (name + ".json")) or {}).get(save_id, {}).get("paused")
            if not isinstance(value, bool):
                raise TypeError("Cannot establish saved pause state: " + name)
            flags[name] = value
        return flags

    async def restore_controls(self, ui, save_id):
        flags = self.pause_flags(save_id)
        self.report["pauseStateBeforeControls"] = flags
        self.report["savedPauseState"] = read(self.report_dir / "saved-pause-flags.json")
        # Clear persisted pause while the human still owns control. F9 can then
        # start one round without a second chat superseding its first tool call.
        if flags["work-state"] or (ui.get("playerAuthorityAgentOwns") and flags["autonomy-state"]):
            await self.r.request("open-conversation")
            chat = await self.r.request("snapshot")
            menu = chat.get("menuType")
            if menu != "StardewAI.Companion.Mod.Menus.CompanionConversationMenu":
                raise ValueError("Resume conversation menu identity differs")
            await self.r.request("text", field="_input", text=RESUME_TEXT)
            await self.r.request("key", key="Enter", expectedMenuType=menu)
            self.report["resumeControlText"] = RESUME_TEXT
            if ui.get("playerAuthorityAgentOwns"):
                self.dispatched = True
            try:
                await self.r.request("key", key="Escape", expectedMenuType=menu)
            except RuntimeError as error:
                self.report["initialChatCloseSkipped"] = {"error": str(error)}
            deadline = time.monotonic() + 30
            while any(self.pause_flags(save_id).values()):
                if time.monotonic() > deadline:
                    raise TimeoutError("Formal resume control did not clear persisted pause")
                await asyncio.sleep(.5)
        ui = await self.r.request("snapshot")
        if not ui.get("playerAuthorityAgentOwns"):
            await self.r.request("takeover-key")
            self.dispatched = True
            ui = await self.r.request("snapshot")
        if ui.get("playerAuthorityAgentOwns") is not True:
            raise ValueError("Normal F9 handoff did not grant authority")
        self.dispatched = True
        self.report["handoff"] = ui
        deadline = time.monotonic() + 30
        while any(self.pause_flags(save_id).values()):
            if time.monotonic() > deadline:
                raise TimeoutError("Formal handoff did not clear runtime pause")
            await asyncio.sleep(.5)
        self.report["pauseStateAfterControls"] = self.pause_flags(save_id)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--spec-file", type=Path, required=True)
    parser.add_argument("--segment-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(ResumeMultiDayRunner(args.run_dir, args.spec_file, args.timeout, args.segment_dir).execute()))
