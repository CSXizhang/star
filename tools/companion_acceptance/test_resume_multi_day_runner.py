import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import multi_day_runner as base
import resume_multi_day_runner as resume


def exercise_controls(tmp_path, monkeypatch, work_paused, autonomy_paused):
    instance = object.__new__(resume.ResumeMultiDayRunner)
    instance.report = {}
    instance.report_dir = tmp_path
    instance.dispatched = False
    state = {"work-state": work_paused, "autonomy-state": autonomy_paused}
    ui = {"playerAuthorityAgentOwns": False}
    actions = []
    menu = "StardewAI.Companion.Mod.Menus.CompanionConversationMenu"

    async def request(action, **params):
        actions.append((action, params))
        if action == "snapshot":
            return {**ui, "menuType": menu}
        if action == "key" and params["key"] == "Enter":
            assert ui["playerAuthorityAgentOwns"] is False
            state.update({"work-state": False, "autonomy-state": False})
        if action == "takeover-key":
            ui["playerAuthorityAgentOwns"] = True
            state["autonomy-state"] = False
        return {}

    instance.r = SimpleNamespace(request=request)
    monkeypatch.setattr(instance, "pause_flags", lambda save: dict(state))
    asyncio.run(instance.restore_controls(dict(ui), "saved"))
    return instance, actions


def test_unpaused_work_handoff_never_sends_an_extra_chat(tmp_path, monkeypatch):
    instance, actions = exercise_controls(tmp_path, monkeypatch, False, True)
    assert [name for name, _ in actions] == ["snapshot", "takeover-key", "snapshot"]
    assert instance.dispatched
    assert not any(instance.report["pauseStateAfterControls"].values())


def test_persisted_work_pause_is_cleared_before_f9_can_start_a_round(tmp_path, monkeypatch):
    instance, actions = exercise_controls(tmp_path, monkeypatch, True, True)
    names = [name for name, _ in actions]
    enter = next(i for i, (name, params) in enumerate(actions) if name == "key" and params["key"] == "Enter")
    assert enter < names.index("takeover-key")
    assert names.count("takeover-key") == 1
    assert [params["text"] for name, params in actions if name == "text"] == ["继续任务。"]
    assert not any(instance.report["pauseStateAfterControls"].values())


def saved_state():
    fixture = {"chest": {"x": 59, "y": 22},
               "protectedTrees": [{"identity": "tree-7"}, {"identity": "tree-8"}]}
    sample = {"year": 1, "season": "spring", "day": 6, "timeOfDay": 600,
              "multiDay": {"money": 751, "actorLocation": "FarmHouse", "crops": [],
                           "shippedItems": [], "harnessTimeFrozen": False,
                           "chests": [{"tile": fixture["chest"], "items": [
                               {"id": "(O)388", "stack": 85}, {"id": "(O)24", "stack": 4}]}],
                           "trees": [{"identity": "tree-7", "stage": 3, "stump": False},
                                     {"identity": "tree-8", "stage": 2, "stump": False}]}}
    previous = {"after": copy.deepcopy(sample), "providerSessionTimeline": [{"sessions": ["original-cid"]}]}
    return sample, fixture, previous


def test_loaded_progress_allows_naturally_grown_protected_trees():
    sample, fixture, previous = saved_state()
    sample["multiDay"]["trees"][0]["stage"] = 5
    sample["day"] = previous["after"]["day"] = 11
    sample["multiDay"]["money"] = previous["after"]["multiDay"]["money"] = 1124
    sample["timeOfDay"] = 610
    assert resume.resume_preflight(sample, fixture, {"agy:save": "original-cid"}, previous)["passed"]


def test_replaced_session_or_missing_resources_rejects_resume():
    sample, fixture, previous = saved_state()
    assert not resume.resume_preflight(sample, fixture, {"save": "replacement-cid"}, previous)["passed"]
    sample["multiDay"]["chests"][0]["items"][0]["stack"] = 84
    assert not resume.resume_preflight(sample, fixture, {"save": "original-cid"}, previous)["passed"]


def test_sampling_failure_still_uses_shared_finally_and_preserves_old_report(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "ROOT", tmp_path)
    run = tmp_path / ".test-runs" / "saved-run"
    segment = run / "evidence" / "resume-segment"
    segment.mkdir(parents=True)
    spec = tmp_path / "spec.json"
    base.write(spec, {"instruction": "original frozen task"})
    sample, _, previous = saved_state()
    base.write(segment / "previous-multi-day-report.json", previous)
    base.write(run / "evidence/multi-day-report.json", {"old": "untouched"})
    instance = resume.ResumeMultiDayRunner(run, spec, 60, segment)
    monkeypatch.setattr(base, "provider_trace", lambda mod: [])

    async def prepare():
        instance.dispatched = True
        instance.report["before"] = sample
        instance.report["samples"].append(sample)
        instance.report["usageOffset"] = 0
        return 0

    async def failed_sample():
        base.write(instance.ev / "reviewer-stop.json", {"runId": run.name,
                   "reviewerRequestedStop": True, "reason": "root-confirmed-stop"})
        raise TimeoutError("Native saving unavailable")

    monkeypatch.setattr(instance, "prepare", prepare)
    monkeypatch.setattr(instance, "native", failed_sample)
    assert asyncio.run(instance.execute()) == 2
    report = base.read(segment / "multi-day-report.json")
    assert report["after"] == sample
    assert report["stopReason"] == "root-confirmed-stop"
    assert report["dispatched"] is True
    assert base.read(run / "evidence/multi-day-report.json") == {"old": "untouched"}
    assert base.read(instance.ev / "free-mode-control-done.json")["success"] is False
