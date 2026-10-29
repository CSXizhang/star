"""Operate only an already running isolated TestDriver; never fabricate game outcomes."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "companion_acceptance"))
from runner import Runner, native_finished, read, rows, write


class Probe:
    def __init__(self, run):
        self.run = Path(run).resolve()
        if not self.run.is_relative_to(ROOT / ".test-runs"):
            raise ValueError("Only a repository .test-runs clone is permitted")
        cfg = read(self.run / "config/test-session.json") or {}
        if cfg.get("scenario") != "free-mode-control" or cfg.get("runId") != self.run.name:
            raise ValueError("Expected isolated free-mode-control session")
        if not Path(cfg["isolatedSavesPath"]).resolve().is_relative_to(self.run):
            raise ValueError("Refusing a nonisolated save")
        self.r = Runner(self.run, "harvest-store", 900, keep_open=True, reset_fixture=True)

    async def lifecycle(self, action):
        label = "continuity-" + uuid.uuid4().hex[:12]
        path = self.r.evidence / "lifecycle-request.json"
        await self.r.wait(lambda: not path.exists(), "Lifecycle request pending")
        write(path, {"label": label, "action": action})
        result = await self.r.wait(lambda: read(self.r.evidence / f"lifecycle-{label}.json"), action, 90)
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    async def source(self, source):
        before = await self.r.request("snapshot")
        native_before = await self.r.native()
        await self.r.request("open-connection")
        chooser = await self.r.request("snapshot")
        await asyncio.sleep(.5)  # Allow the genuine choice-menu frame to be presented/captured.
        await self.r.request("options-button", text={"builtin": "在游戏内启动伙伴", "external": "等待已有 AI 伙伴连接"}[source])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            after = await self.r.request("snapshot")
            if (after.get("connectionSource") == after.get("persistedConnectionSource") == source
                    and after.get("persistedConnectionSourceChosen") is True
                    and after.get("connectionPendingRequestId") is None
                    and after.get("connectionNeedsPersistence") is False):
                if before.get("actorMode") == "player" and before.get("playerAuthorityAgentOwns") is False:
                    assert after.get("playerAuthorityAgentOwns") is False, "Source switch stole F9 authority"
                await self.r.request("close")
                native_after = await self.r.native()
                for key in ("acceptanceChestItems", "companionItems", "harnessFixtureCropCount"):
                    assert native_before.get(key) == native_after.get(key), "Source switch altered native world: " + key
                return {"before": before, "chooser": chooser, "after": after,
                        "nativeBefore": native_before, "nativeAfter": native_after, "ackAndPersistence": True}
            await asyncio.sleep(1)
        raise TimeoutError("Source acknowledgement/persistence did not complete")

    async def send(self, text, *, wait=True):
        offset = len(rows(self.r.mod / "chat_commands.jsonl"))
        await self.r.request("close")
        await self.r.request("open-conversation")
        await self.r.request("text", field="_input", text=text)
        await self.r.request("key", key="Enter")
        await self.r.request("close")
        if not wait:
            return {"submitted": True, "commandOffset": offset}
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            terminal = next((x for x in rows(self.r.mod / "chat_commands.jsonl")[offset:] if x.get("prompt") == text), None)
            if terminal:
                sample = await self.r.native()
                if native_finished(sample):
                    if terminal.get("status") != "completed":
                        raise RuntimeError("Model turn did not complete: " + str(terminal.get("status")))
                    return {"turn": terminal, "native": sample}
            await asyncio.sleep(2)
        raise TimeoutError("No terminal native-settled model turn")

    async def cancel(self):
        path = self.r.evidence / "free-mode-control-action.json"
        await self.r.wait(lambda: not path.exists(), "Cancel channel pending")
        path.write_text("cancel", encoding="utf-8")
        await self.r.wait(lambda: not path.exists(), "Cancel not consumed")
        for _ in range(30):
            value = await self.r.native()
            if native_finished(value):
                return value
            await asyncio.sleep(1)
        raise TimeoutError("Native actions did not settle after cancel")

    async def sleep(self):
        await self.r.request("close")
        before = await self.r.native()
        assert native_finished(before), "Refuse to sleep during an active action"
        await self.r.request("host-go-bed")
        await asyncio.sleep(2)
        await self.r.request("host-walk-bed")
        for _ in range(60):
            ui = await self.r.request("snapshot")
            if ui.get("playerIsInBed") and any(r.get("key") == "Yes" for r in ui.get("dialogueResponses") or []):
                await self.r.request("choice", key="Yes")
                break
            await asyncio.sleep(.5)
        else: raise TimeoutError("Native bed walk did not show its genuine sleep question")
        for _ in range(90):
            await asyncio.sleep(1)
            ui = await self.r.request("snapshot")
            changed = (ui.get("year"), ui.get("season"), ui.get("day")) != (before["year"], before["season"], before["day"])
            if changed and ui.get("worldReady") and not ui.get("nativeNewDay") and not ui.get("nativeFade") and ui.get("menuType") is None:
                await self.r.request("close")
                await self.r.request("leave-player-bed")
                return {"before": before, "after": await self.r.native(), "nativeSleep": True}
            if "ShippingMenu" in str(ui.get("menuType")):
                await self.r.request("key", key="Escape")
        raise TimeoutError("Native overnight did not advance the date")

    async def observer(self):
        fixtures = list(self.r.evidence.glob("harness-fixture-*.json"))
        fixture = read(max(fixtures, key=lambda p: p.stat().st_mtime))
        sample = await self.r.native()
        value = {"runId": self.run.name, "saveName": fixture["saveName"], "mode": fixture["mode"],
                 "targets": sample["harnessFixtureTargets"], "chest": fixture["chest"]}
        write(self.r.evidence / "continuity-observer.json", value)
        return value

    async def execute(self, action, value):
        if action == "identity":
            binding = read(self.r.mod / "data/companion-connection.json") or {}
            discovery = read(self.r.mod / "data/transport-discovery.json") or {}
            sessions = read(self.r.mod / "chat_sessions.json") or {}
            dll = self.r.mod / "StardewAI.Companion.Mod.dll"
            return {"runId": self.run.name, "modDllSha256": hashlib.sha256(dll.read_bytes()).hexdigest(),
                    "binding": {k: binding.get(k) for k in ("adapter", "providerSessionId", "bindingId", "enabled")},
                    "game": {k: discovery.get(k) for k in ("saveId", "gameSessionId", "gamePid", "pid")},
                    "builtinSessionIds": sorted(set(sessions.values())),
                    "ui": await self.r.request("snapshot")}
        if action == "source": return await self.source(value)
        if action == "send": return await self.send(value)
        if action == "start": return await self.send(value, wait=False)
        if action == "sleep": return await self.sleep()
        if action == "snapshot": return {"ui": await self.r.request("snapshot"), "native": await self.r.native()}
        if action == "cancel": return await self.cancel()
        if action == "verify-human-hold":
            samples = []
            for _ in range(10):
                ui = await self.r.request("snapshot")
                native = await self.r.native()
                assert ui.get("actorMode") == "player" and ui.get("playerAuthorityAgentOwns") is False
                assert not native.get("companionMachineExecuting"), "Old native action restarted after human takeover"
                samples.append({"ui": ui, "native": native})
                await asyncio.sleep(1)
            assert all(x["ui"]["playerTile"] == samples[0]["ui"]["playerTile"] for x in samples), "Player moved without restored AI authority"
            return {"humanAuthorityHeld": True, "oldActionStayedStopped": True, "samples": samples}
        if action in {"f9", "takeover-active"}:
            await self.r.request("close")
            before = await self.r.request("snapshot")
            assert before.get("actorMode") == "player", "F9 acceptance requires a player-mode restart"
            active = None
            if action == "takeover-active":
                assert before.get("playerAuthorityAgentOwns") is True, "AI must own control before active takeover"
                for _ in range(180):
                    active = await self.r.native()
                    if active.get("companionMachineExecuting") is True: break
                    await asyncio.sleep(.5)
                else: raise TimeoutError("No actual native action began")
            after = await self.r.request("takeover-key")
            assert before.get("playerAuthorityAgentOwns") is not after.get("playerAuthorityAgentOwns"), "F9 did not toggle authority"
            if active:
                await asyncio.sleep(2)
                stopped = await self.r.native()
                assert not stopped.get("companionMachineExecuting"), "Native action continued after takeover"
                return {"before": before, "active": active, "after": after, "stopped": stopped}
            return {"before": before, "after": after}
        if action in {"save-exit", "save-title"}:
            await self.cancel()
            await self.observer()
            return await self.lifecycle(action)
        if action == "reload": return await self.lifecycle("reload")
        if action in {"harvest", "logistics", "forestry"}:
            if value and re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value) is None:
                raise ValueError("Evidence label must use 1-48 letters, digits, underscores or hyphens")
            if action == "logistics": self.r.case = "harvest-logistics"
            if action == "forestry": self.r.case = "rest-forestry"
            await self.r.execute()
            archive = self.r.evidence / ("continuity-harvest-" + (value or uuid.uuid4().hex[:8]))
            archive.mkdir(exist_ok=False)
            for name in ("preset-report.json", "preset-report.md", "preset-trace.json"):
                if (self.r.evidence / name).exists(): shutil.copy2(self.r.evidence / name, archive / name)
            report = read(archive / "preset-report.json")
            await self.observer()
            if report.get("status") != "passed": raise RuntimeError("Harvest acceptance failed: " + str(archive))
            return {"report": str(archive), "status": report["status"]}
        raise ValueError(action)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    parser.add_argument("action", choices=["identity", "source", "send", "start", "sleep", "snapshot", "cancel", "f9", "takeover-active", "verify-human-hold", "save-exit", "save-title", "reload", "harvest", "logistics", "forestry"])
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    probe = Probe(args.run)
    result = await probe.execute(args.action, args.value)
    output = probe.r.evidence / ("continuity-" + args.action + "-" + uuid.uuid4().hex[:8] + ".json")
    write(output, result)
    print(json.dumps({"evidence": str(output), "action": args.action, "ok": True}))


if __name__ == "__main__": asyncio.run(main())
