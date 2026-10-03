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
    journal.write_text("paid-usage\n")
    req = request(tmp_path, {"work-state.json": {"executions": [{"id": "saved"}]},
                             "autonomy-state.json": {"dailySpend": 12}} if checkpoint else None)
    restore_game_load(tmp_path, req)
    restored = json.loads((data / "work-state.json").read_text())
    assert restored["other"] == {"tasks": ["untouched"]}
    restored = restored["farm"]
    assert restored["goals"][0]["status"] == "active"
    assert restored["goals"][0]["constraints"] == {"keep": "tree"}
    assert restored["notices"] == work["notices"]
    assert restored["tasks"] == [] and restored["decision"] == {}
    assert restored["todos"][0]["status"] == "pending"
    assert restored["executions"] == ([{"id": "saved"}] if checkpoint else [])
    memory = json.loads((data / "companion-memory.json").read_text())["farm"]
    assert memory["entries"] == [{"kind": "agreement", "text": "不砍树"}]
    assert memory["instructionRevision"] == 2
    autonomy = json.loads((data / "autonomy-state.json").read_text())["farm"]
    assert autonomy["paused"] and autonomy["daily_tokens"]["today"]["total"] == 99
    assert autonomy["dailySpend"] == (12 if checkpoint else 0)
    assert journal.read_text() == "paid-usage\n"
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


def test_bridge_reload_retains_provider_session_and_does_not_repeat_on_reconnect(tmp_path):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
        bridge._life_chat.record_session_id("farm", "existing-dialogue")
        bridge._life_chat.record_fingerprint("farm", bridge._profile_revision("farm"), bridge._memory_instruction_revision("farm"))
        request(tmp_path)
        await bridge._prepare_game_load("farm", "new-session")
        assert bridge._life_chat.rotate_if_needed("farm", bridge._profile_revision("farm"), bridge._memory_instruction_revision("farm")) == "existing-dialogue"
        bridge._work_store.add_goal("farm", "new goal after reload")
        await bridge._prepare_game_load("farm", "new-session")
        assert bridge._work_store.state("farm").goals[0].text == "new goal after reload"
        bridge._send_reply = AsyncMock()
    asyncio.run(scenario())
