import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import multi_day_runner as monitor


def make_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    run = tmp_path / ".test-runs" / "review-stop"
    evidence = run / "evidence"
    evidence.mkdir(parents=True)
    spec = tmp_path / "spec.json"
    monitor.write(spec, {"instruction": "original task", "initial": {"money": 500}})
    instance = monitor.MultiDayRunner(run, spec, 7200)
    return instance


def stop_request(instance, **overrides):
    request = {"runId": instance.run.name, "reviewerRequestedStop": True,
               "reason": "reviewer_confirmed_blocker"}
    request.update(overrides)
    monitor.write(instance.ev / "reviewer-stop.json", request)


def test_mismatched_or_unconfirmed_stop_is_ignored(tmp_path, monkeypatch):
    instance = make_runner(tmp_path, monkeypatch)
    stop_request(instance, runId="another-run")
    assert instance.reviewer_stop() is None
    stop_request(instance, reviewerRequestedStop=False)
    assert instance.reviewer_stop() is None
    assert "reviewerRequestedStop" not in instance.report


def test_explicit_stop_retains_original_reason(tmp_path, monkeypatch):
    instance = make_runner(tmp_path, monkeypatch)
    stop_request(instance, reason="root reviewed the blocked turn")
    assert instance.reviewer_stop() == "root reviewed the blocked turn"
    assert instance.report["reviewerStopRequest"]["runId"] == instance.run.name


def test_sample_failure_stop_exits_through_finally_with_last_state(tmp_path, monkeypatch):
    instance = make_runner(tmp_path, monkeypatch)
    run = instance.run
    saves = run / "saves"
    saves.mkdir()
    (saves / "save").write_text("original")
    monitor.write(run / "config/test-session.json", {"runId": run.name,
                  "scenario": "free-mode-control", "isolatedSavesPath": str(saves)})
    monitor.write(instance.ev / "harness-coordination.json", {"state": "READY_FOR_ACT"})
    for path in (instance.r.mod / "StardewAI.Companion.Mod.dll",
                 run / "mods/StardewAI.Companion.TestDriver/StardewAI.Companion.TestDriver.dll"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    # Start with empty append-only records, as a fresh real run does.
    (instance.r.mod / "chat_commands.jsonl").write_text("")
    monkeypatch.setattr(monitor, "native_preflight", lambda *args: {"passed": True})
    monkeypatch.setattr(monitor, "provider_trace", lambda mod: [])

    async def binding(*args):
        return None

    monkeypatch.setattr(monitor, "verify_agy_binding", binding)
    handed_off = False

    async def request(action, **params):
        nonlocal handed_off
        if action == "takeover-key":
            handed_off = True
        return {"actorMode": "player", "playerAuthorityAgentOwns": handed_off,
                "productionChatConnected": True, "chatStatus": "Idle",
                "menuType": "StardewAI.Companion.Mod.Menus.CompanionConversationMenu"}

    instance.r.request = request
    before = {"year": 1, "season": "spring", "day": 1,
              "multiDay": {"money": 500, "inventory": []}}
    latest = {"year": 1, "season": "spring", "day": 2, "nativeProof": "last successful sample"}
    attempts = 0

    async def native():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return before
        instance.report["samples"].append(latest)
        usage_path = instance.r.mod / "data/context-usage.jsonl"
        usage_path.parent.mkdir(exist_ok=True)
        usage_path.write_text(json.dumps({"usage": {"output_tokens": 17}}) + "\n")
        stop_request(instance)
        raise TimeoutError("overnight snapshot unavailable")

    instance.native = native
    assert asyncio.run(instance.execute()) == 2
    result = monitor.read(instance.ev / "multi-day-report.json")
    done = monitor.read(instance.ev / "free-mode-control-done.json")
    assert attempts == 2
    assert result["status"] == "reviewer-stopped"
    assert result["stopReason"] == "reviewer_confirmed_blocker"
    assert result["after"] == latest
    assert result["contextUsage"][0]["usage"]["output_tokens"] == 17
    assert result["samplingErrors"][0]["error"] == "overnight snapshot unavailable"
    assert done["reason"] == result["stopReason"]
    assert done["success"] is False and done["taskJudgmentPending"] is True
