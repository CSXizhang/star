"""Acceptance must reject false positives even if a model says it completed."""
import asyncio
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("preset_runner", Path(__file__).with_name("runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_agy_binding_rejects_missing_or_unregistrable_tools():
    tools = [{"name": name, "inputSchema": {"type": "object"}} for name in runner.EXPECTED_GAME_TOOLS]
    with pytest.raises(ValueError, match="eight game tools"):
        runner.verify_agy_tool_schemas("star-short", tools[:-1])
    with pytest.raises(ValueError, match="cannot register"):
        runner.verify_agy_tool_schemas("stardew-accept-multi-day-1105-113312-0ba2", tools)
    tools[0]["inputSchema"]["required"] = 4
    with pytest.raises(ValueError, match="Invalid game tool schema"):
        runner.verify_agy_tool_schemas("star-short", tools)


def test_agy_binding_reads_all_schemas_before_observing_save(tmp_path, monkeypatch):
    import mcp
    import mcp.client.stdio
    from mcp.types import Tool

    run = tmp_path / "run"
    mod = run / "mods/mod"
    runner.write(mod / "config/chat-backend.json", {"backend": "agy"})
    runner.write(run / "evidence/agy-game-binding.json", {
        "serverName": "star-short", "command": "python", "args": [], "env": {}})
    runner.write(run / "evidence/harness-fixture-startup.json", {"saveName": "saved-world"})
    actions = []

    @asynccontextmanager
    async def fake_stdio(params):
        yield None, None

    class Session:
        def __init__(self, reader, writer):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            actions.append("initialize")

        async def list_tools(self):
            actions.append("list_tools")
            return SimpleNamespace(tools=[Tool(name=name, inputSchema={"type": "object"})
                                          for name in runner.EXPECTED_GAME_TOOLS])

        async def call_tool(self, name, arguments):
            actions.append(name)
            assert arguments == {"detail": True}
            return SimpleNamespace(structuredContent={"saveId": "saved-world"}, content=[], isError=False)

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", fake_stdio)
    proof = asyncio.run(runner.verify_agy_binding(run, mod, {}))
    assert actions == ["initialize", "list_tools", "get_status"]
    assert len(proof["tools"]) == 8
    assert proof["saveId"] == "saved-world"
    assert runner.read(run / "evidence/agy-binding-verified.json")["schemaSha256"] == proof["schemaSha256"]


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


def test_agy_native_transcript_pairs_receipt_and_keeps_missing_result_unknown():
    call = {"name": "call_mcp_tool", "args": {"ServerName": '"isolated"',
            "ToolName": '"submit_work"', "Arguments": '{"action":{"rest":{"target_stamina":80}}}'}}
    planner = {"_provider": "agy", "type": "PLANNER_RESPONSE", "step_index": 5, "tool_calls": [call]}
    receipt = {"_provider": "agy", "type": "GENERIC", "step_index": 6, "status": "DONE",
               "content": 'Created At: stamp\nCompleted At: stamp\n{"status":"completed","details":{"finalLocation":"FarmHouse","finalTile":{"x":9,"y":9}}}'}
    summary = runner.summarize_trace([planner, receipt])
    got = summary["toolCalls"][0]
    assert got["name"] == "mcp__isolated__submit_work"
    assert got["input"]["action"]["rest"]["target_stamina"] == 80
    assert got["result"]["details"]["finalTile"] == {"x": 9, "y": 9}
    assert not got["nonterminal"]
    # Parallel generic receipts have no stable tool ID: never guess their pairing.
    ambiguous = runner.summarize_trace([{**planner, "tool_calls": [call, call]}, receipt])
    assert all(c["result"] is None and c["nonterminal"] for c in ambiguous["toolCalls"])
    failed = runner.summarize_trace([planner, {**receipt, "status": "ERROR", "content": "Error executing tool: STALE_DECISION"}])
    assert failed["errorCount"] == 1 and failed["toolCalls"][0]["result"] is None
    assert not failed["toolCalls"][0]["nonterminal"]


def test_agy_large_receipt_is_archived_only_from_its_own_step(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.Path, "home", lambda: tmp_path)
    cid = "a" * 32
    output = tmp_path / ".gemini/antigravity-cli/brain" / cid / ".system_generated/steps/7/output.txt"
    output.parent.mkdir(parents=True)
    output.write_text('{"status":"executed","terminalState":"succeeded"}')
    row = {"conversationId": cid, "step_index": 7,
           "content": "The output was large and was saved to: " + output.as_uri()}
    archived = runner.attach_agy_output(row)
    assert runner.decoded(archived["_externalOutput"]["text"])["terminalState"] == "succeeded"
    assert archived["_externalOutput"]["sha256"]
    assert "_externalOutput" not in runner.attach_agy_output({**row, "step_index": 8})


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


def test_agy_inline_truncation_reads_only_complete_same_session_step(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.Path, "home", lambda: tmp_path)
    cid = "b" * 32
    output = tmp_path / ".gemini/antigravity-cli/brain" / cid / ".system_generated/steps/21/output.txt"
    output.parent.mkdir(parents=True)
    output.write_text('{"status":"executed","terminalState":"succeeded","completedCount":5}')
    row = {"conversationId": cid, "step_index": 21,
           "content": '{"status":"executed",\n<truncated 299 bytes>\n}'}
    archived = runner.attach_agy_output(row)
    assert archived["content"] == row["content"]
    assert runner.decoded(archived["_externalOutput"]["text"])["completedCount"] == 5
    assert archived["_externalOutput"]["source"] == output.as_uri()
    assert "_externalOutput" not in runner.attach_agy_output({**row, "conversationId": "c" * 32})
    assert "_externalOutput" not in runner.attach_agy_output({**row, "step_index": 22})
    output.write_text('{"terminalState":')
    assert "_externalOutput" not in runner.attach_agy_output(row)
