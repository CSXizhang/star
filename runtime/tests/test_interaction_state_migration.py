"""Regression checks for preserving player intent through interaction migration."""
import json

from stardew_ai_runtime.companion_memory import CompanionMemoryStore
from stardew_ai_runtime.companion_milestones import CompanionMilestoneStore
from stardew_ai_runtime.companion_profile import CompanionProfileStore
from stardew_ai_runtime.work_state import WorkStore


def test_old_allowance_fields_do_not_override_player_terms(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("farm", "种地并养鸡", constraints={
        "purchaseBudget": 0, "dailyPurchaseBudget": 0, "reservedFunds": 1000,
        "termsNote": "玩家明确要求今天不买鸡，先建好鸡舍", "milestoneSpec": {"purchaseBudget": 0},
    })
    loaded = store.state("farm").goals[0]
    assert loaded.id == goal.id
    assert loaded.constraints == {"reservedFunds": 1000,
        "termsNote": "玩家明确要求今天不买鸡，先建好鸡舍", "milestoneSpec": {}}


def test_player_allowance_words_remain_but_retired_system_rules_do_not(tmp_path):
    path = tmp_path / "memory.json"
    entries = [{"id": "p", "kind": "agreement", "source": "player", "text": "每日购买额度500，我亲口定的"},
               {"id": "s", "kind": "agreement", "source": "system", "text": "每日购买额度为0"}]
    path.write_text(json.dumps({"farm": {"memoryRevision": 1, "entries": entries}}), encoding="utf-8")
    store = CompanionMemoryStore(path)
    rendered = store.render_for_context("farm")
    assert [item["id"] for item in rendered["agreements"]] == ["p"]
    assert json.loads(path.read_text(encoding="utf-8"))["farm"]["entries"] == entries


def test_duplicate_discussion_does_not_make_duplicate_goals_and_chores_survive_night(tmp_path):
    milestones = CompanionMilestoneStore(tmp_path / "milestones.json")
    work = WorkStore(tmp_path / "work.json")
    fields = dict(title="浇完菜地", target_date="1:spring:2", preparation=["water"])
    first = milestones.propose("farm", **fields)
    repeated = milestones.propose("farm", **fields)
    assert first["id"] == repeated["id"]
    adopted = milestones.adopt("farm", first["id"], work_store=work)
    assert len(work.state("farm").goals) == 1
    assert adopted["goalId"] == work.state("farm").goals[0].id
    assert work.state("farm").todos[0].expiry is None


def test_invalid_profile_patch_cannot_partially_change_bedtime(tmp_path):
    profiles = CompanionProfileStore(tmp_path / "profile.json")
    status, before = profiles.set("farm", {"bedtime": 2300}, 0)
    assert status == "confirmed"
    status, _ = profiles.set("farm", {"bedtime": 2200, "personality": "invalid"}, 1)
    assert status == "rejected"
    assert profiles.get("farm")["profile"]["bedtime"] == 2300
    assert profiles.get("farm")["profileRevision"] == before["profileRevision"]
