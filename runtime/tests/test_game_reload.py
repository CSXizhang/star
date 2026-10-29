import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.game_reload import (
    RUNTIME_FILES,
    mark_game_load_applied,
    pending_game_load,
    read_reload_history,
    reload_context,
    restore_game_load,
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def request(tmp_path, committed=None):
    value = {"saveId": "farm", "gameSessionId": "new-session", "gameDate": "1:spring:2",
             "runtimePartitions": {n: json.dumps((committed or {}).get(n, {})) for n in RUNTIME_FILES}
             if committed is not None else None}
    write(tmp_path / "data/game-load.json", value)
    return value


@pytest.mark.parametrize("checkpoint", [False, True])
def test_reload_preserves_intent_and_paid_usage_but_invalidates_work(tmp_path, checkpoint):
    data = tmp_path / "data"
    work = {"goals": [{"id": "g", "text": "留树，养鸡", "status": "completed", "constraints": {"keep": "tree"}}],
            "tasks": [{"id": "old", "status": "running"}], "todos": [{"id": "t", "status": "done", "intent": "买种子"}],
            "notices": [{"id": "n", "message": "买几只？", "answer": "两只，省钱", "answeredAt": "now"}],
            "decision": {"token": "old-token"}, "executions": [{"id": "abandoned"}]}
    write(data / "work-state.json", {"farm": work, "other": {"tasks": ["untouched"]}})
    write(data / "companion-memory.json", {"farm": {"memoryRevision": 4, "instructionRevision": 2,
          "entries": [{"kind": "agreement", "text": "不砍树"}, {"kind": "event", "text": "已买两只鸡"}]}})
    write(data / "autonomy-state.json", {"farm": {"paused": True, "daily_tokens": {"today": {"total": 99}}, "dailySpend": 100}})
    journal = tmp_path / "chat_commands.jsonl"
    journal.write_text("paid-usage\n", encoding="utf-8")
    req = request(tmp_path, {"work-state.json": {"executions": [{"id": "saved"}]},
                             "autonomy-state.json": {"dailySpend": 12}} if checkpoint else None)
    restore_game_load(tmp_path, req)
    restored = json.loads((data / "work-state.json").read_text(encoding="utf-8"))
    assert restored["other"] == {"tasks": ["untouched"]}
    restored = restored["farm"]
    assert restored["goals"][0]["status"] == "active"
    assert restored["goals"][0]["constraints"] == {"keep": "tree"}
    assert restored["notices"] == work["notices"]
    assert restored["tasks"] == [] and restored["decision"] == {}
    assert restored["todos"][0]["status"] == "pending"
    assert restored["executions"] == ([{"id": "saved"}] if checkpoint else [])
    memory = json.loads((data / "companion-memory.json").read_text(encoding="utf-8"))["farm"]
    assert memory["entries"] == [{"kind": "agreement", "text": "不砍树"}]
    assert memory["instructionRevision"] == 2
    autonomy = json.loads((data / "autonomy-state.json").read_text(encoding="utf-8"))["farm"]
    assert autonomy["paused"] and autonomy["daily_tokens"]["today"]["total"] == 99
    assert autonomy["dailySpend"] == (12 if checkpoint else 0)
    assert journal.read_text(encoding="utf-8") == "paid-usage\n"
    before = (data / "work-state.json").read_bytes()
    restore_game_load(tmp_path, req)  # Interrupted restore can safely replay its before-image.
    assert (data / "work-state.json").read_bytes() == before
    mark_game_load_applied(tmp_path, req)
    assert pending_game_load(tmp_path, "farm", "new-session") is None
    assert pending_game_load(tmp_path, "other", "new-session") is None


def test_reload_history_is_complete_paginated_and_scoped(tmp_path):
    archive = tmp_path / "data/conversations/reloads/history.json"
    entries = [{"Text": str(i) + "很长的原话" * 150, "IsPlayer": i % 2 == 0} for i in range(231)]
    write(archive, {"SaveId": "farm", "Entries": entries})
    req = request(tmp_path)
    req["reloadSummary"] = {"fullConversationArchive": str(archive)}
    restore_game_load(tmp_path, req)
    result, offset = [], 0
    while offset is not None:
        page = read_reload_history(tmp_path, "farm", offset)
        result.extend(page["entries"])
        offset = page["nextOffset"]
    assert result == entries
    assert read_reload_history(tmp_path, "other")["entries"] == []
    write(archive, {"SaveId": "other", "Entries": entries})
    with pytest.raises(ValueError, match="another save"):
        read_reload_history(tmp_path, "farm")
    write(tmp_path / "data/work-state.json", {"farm": {"goals": [{"text": "新目标"}]}})
    assert reload_context(tmp_path, "farm")["goals"] == [{"text": "新目标"}]


def test_bridge_reload_and_restart_keep_original_session_with_fresh_world(tmp_path):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        bridge._life_chat.record_session_id("farm", "existing-dialogue")
        bridge._life_chat.record_fingerprint("farm", bridge._profile_revision("farm"), bridge._memory_instruction_revision("farm"))
        bridge._life_chat.record_discussion("farm", "chat", "不要砍树", "已记住，明天再核对材料。")
        bridge._sessions["life:fake:farm"] = "legacy-dialogue"
        bridge._sessions["fake:other"] = "other-save"
        request(tmp_path)
        await bridge._prepare_game_load("farm", "new-session")
        assert bridge.get_conversation_id("farm") == "existing-dialogue"
        assert bridge._life_chat.get_session_id("farm") == "existing-dialogue"
        assert bridge.get_conversation_id("other") == "other-save"
        summary = reload_context(tmp_path, "farm")["conversationHandoff"]
        assert summary["recentDialogue"][0]["text"] == "不要砍树"
        assert "旧回复" in summary["instruction"]
        bridge._work_store.add_goal("farm", "new goal after reload")
        await bridge._prepare_game_load("farm", "new-session")
        assert bridge._life_chat.get_session_id("farm") == "existing-dialogue"
        restarted = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        await restarted._prepare_game_load("farm", "new-session")
        assert restarted.get_conversation_id("farm") == "existing-dialogue"
        assert bridge._work_store.state("farm").goals[0].text == "new goal after reload"
        bridge._send_reply = AsyncMock()
    asyncio.run(scenario())


def test_reload_repairs_only_exact_linked_choices_without_replaying_work(tmp_path):
    import hashlib

    from stardew_ai_runtime.game_reload import recover_recorded_choices
    from stardew_ai_runtime.work_state import WorkStore
    store = WorkStore(tmp_path / "data/work-state.json")
    goal = store.add_goal("farm", "农场")
    first = store.request_player_decision("farm", goal.id, "存哪里？", choices=["左箱", "右箱"])
    second = store.request_player_decision("farm", goal.id, "种什么？", choices=["土豆", "草莓"])
    path = tmp_path / "data/conversations" / (hashlib.sha256(b"farm").hexdigest().upper() + ".json")
    write(path, {"SaveId": "farm", "Entries": [
        {"Id": "selected:player", "IsPlayer": True, "Text": "右箱", "ReplyToDecisionId": first["id"]},
        {"Id": "question:player", "IsPlayer": True, "Text": "哪个好？", "ReplyToDecisionId": second["id"]}]})
    assert recover_recorded_choices(tmp_path, "farm") == 1
    restored = WorkStore(tmp_path / "data/work-state.json")
    assert [n["id"] for n in restored.unresolved_player_notices("farm")] == [second["id"]]
    assert not restored.state("farm").tasks
    assert recover_recorded_choices(tmp_path, "farm") == 0
