"""Acceptance must reject false positives even if a model says it completed."""
import importlib.util
import asyncio
from pathlib import Path

spec = importlib.util.spec_from_file_location("preset_runner", Path(__file__).with_name("runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_world_evidence_rejects_missing_remaining_and_day_rollover():
    before = {"year": 1, "season": "spring", "day": 1, "harnessFixtureTargetCount": 2,
              "harnessFixtureTargets": [{"x": 1, "y": 2}, {"x": 2, "y": 2}], "companionWater": 1}
    after = dict(before, harnessFixtureDryCropCount=0, harnessFixtureCropCount=2,
                 harnessFixtureWateredCount=2, companionRestState="awake", companionWater=20,
                 companionMachineExecuting=None, companionActiveTaskId=None)
    assert all(runner.evaluate("water-refill", before, after, [after]).values())
    for invalid in ({}, dict(after, harnessFixtureDryCropCount=1), dict(after, day=2),
                    dict(after, companionMachineExecuting=True), dict(after, companionActiveTaskId="running")):
        assert not all(runner.evaluate("water-refill", before, invalid, [invalid]).values())


def test_harvest_disappearance_does_not_prove_pickup_or_storage():
    before = {"year": 1, "season": "spring", "day": 1, "harnessFixtureTargetCount": 1,
              "harnessFixtureTargets": [{"x": 1, "y": 2}]}
    after = dict(before, harnessFixtureCropCount=0, companionRestState="awake",
                 companionMachineExecuting=None, companionActiveTaskId=None,
                 acceptanceGroundDropRemaining=False, companionItems=[],
                 acceptanceChestItems=[{"id": "(O)24", "stack": 1}, {"id": "(O)388", "stack": 3}])
    assert all(runner.evaluate("harvest-store", before, after, []).values())
    assert not all(runner.evaluate("harvest-store", before, dict(after, acceptanceChestItems=[]), []).values())
    assert not all(runner.evaluate("harvest-store", before, dict(after, companionItems=[{"id": "(O)24", "stack": 1}]), []).values())


def test_trace_retains_tool_failures_and_nonterminal_results():
    trace = [{"event": {"item": {"type": "tool_call", "toolCall": {
        "name": "mcp__stardew-companion__submit_work", "input": {"water_auto": {}},
        "output": {"content": [{"type": "text", "text": 'Error executing tool: IN_PROGRESS'}]}}}}}]
    trace.append({"event": {"item": {"type": "tool_call", "toolCall": {
        "name": "mcp__stardew-companion__submit_work", "status": 3,
        "output": {"content": [{"type": "text", "text": "Validation failed for tool"}]}}}}})
    summary = runner.summarize_trace(trace)
    assert summary["errorCount"] == 2
    assert summary["toolCalls"][0]["nonterminal"]


def test_suite_keeps_game_open_and_preserves_failed_scene(tmp_path, monkeypatch):
    calls = []
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    class FakeRunner:
        def __init__(self, run, case, timeout, cases_file, **options):
            calls.append((case, options))
            self.report = {"case": case, "status": "passed" if case == "care" else "failed", "elapsedSeconds": 1}

        async def execute(self):
            for filename in ("preset-report.json", "preset-report.md", "preset-trace.json"):
                (evidence / filename).write_text(self.report["case"])
            assert not (evidence / "free-mode-control-done.json").exists()
            return self.report["status"] != "passed"

    monkeypatch.setattr(runner, "Runner", FakeRunner)
    assert asyncio.run(runner.run_suite(tmp_path, ["care", "harvest", "rest"], 60, None)) == 1
    assert calls == [("care", {"keep_open": True, "reset_fixture": False}),
                     ("harvest", {"keep_open": True, "reset_fixture": True})]
    assert (evidence / "care/preset-report.md").read_text() == "care"
    assert runner.read(evidence / "free-mode-control-done.json") == {"success": False}
