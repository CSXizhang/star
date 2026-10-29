"""Persistence, ownership and retrieval boundaries for free-text learning."""
import json

from stardew_ai_runtime.companion_memory import CompanionMemoryStore, resolve_companion_id


def test_notes_survive_legacy_edits_without_changing_wire(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text(json.dumps({"farm": {"memoryRevision": 1, "entries": [{
        "id": "old", "kind": "preference", "text": "喜欢花", "source": "player",
        "gameDate": "1:spring:1", "createdAt": "yesterday", "category": "constraint"}]}}))
    store = CompanionMemoryStore(path)
    prose = "遇到池塘边绕路，先观察可达的边缘。" * 30
    status, saved = store.save_note("farm", prose, companion_id="alice", kind="method",
                                    title="农田浇水", scope="有水源的农场")
    assert status == "confirmed"
    store.correct("farm", "old", "更喜欢蓝花", saved["memoryRevision"])
    reopened = CompanionMemoryStore(path)
    assert reopened.recall("farm", "浇水", companion_id="alice")["notes"][0]["text"] == prose
    wire = reopened.list("farm")
    assert set(wire) == {"memoryRevision", "entries"}
    assert set(wire["entries"][0]) == {"id", "kind", "text", "source", "gameDate", "createdAt"}
    assert reopened.render_for_context("farm")["constraints"] == []


def test_companion_and_save_isolation_and_atomic_consolidation(tmp_path):
    store = CompanionMemoryStore(tmp_path / "memory.json")
    _, first = store.save_note("farm", "池塘在东侧", companion_id="alice", scope="农场观察")
    _, second = store.save_note("farm", "从南边接近水源", companion_id="alice")
    ids = [first["note"]["id"], second["note"]["id"]]
    assert store.recall("farm", companion_id="bob")["notes"] == []
    assert store.recall("other", companion_id="alice")["notes"] == []
    assert store.save_note("farm", "偷改", companion_id="bob", note_id=ids[0])[0] == "rejected"
    assert store.save_note("farm", "合并", companion_id="alice", supersedes=[ids[0], "missing"])[0] == "rejected"
    assert len(store.recall("farm", companion_id="alice")["notes"]) == 2
    status, result = store.save_note("farm", "东侧池塘从南边接近", companion_id="alice", supersedes=ids)
    assert status == "confirmed"
    assert result["note"]["derivedFrom"] == ids
    assert len(store.recall("farm", companion_id="alice")["notes"]) == 1
    historical = store.recall("farm", companion_id="alice", note_id=ids[0])["notes"][0]
    assert historical["status"] == "superseded"
    assert historical["superseded_by"] == result["note"]["id"]
    assert store.forget_note("farm", result["note"]["id"], companion_id="bob")[0] == "rejected"
    store.forget_note("farm", result["note"]["id"], companion_id="alice", reason="读档后布局不同")
    assert store.recall("farm", companion_id="alice")["notes"] == []
    assert store.recall("farm", companion_id="alice", note_id=result["note"]["id"])["notes"][0]["status"] == "revoked"


def test_temporary_notes_do_not_become_long_term_truth(tmp_path):
    store = CompanionMemoryStore(tmp_path / "memory.json")
    _, saved = store.save_note("farm", "今天不砍树", companion_id="alice", source="player",
                               scope="只在今天", valid_on="1:spring:5", game_date="1:spring:5")
    assert store.recall("farm", companion_id="alice", game_date="1:spring:6")["notes"] == []
    historical = store.recall("farm", companion_id="alice", game_date="1:spring:4",
                              note_id=saved["note"]["id"])["notes"][0]
    assert historical["applicability"] == "historical"
    assert historical["scope"] == "只在今天"
    assert historical["source"] == "player"
    assert store.render_for_context("farm", companion_id="alice", game_date="1:spring:5")["relevantNotes"]


def test_recall_finds_older_relevant_methods_without_executing_them(tmp_path):
    store = CompanionMemoryStore(tmp_path / "memory.json")
    store.save_note("farm", "先观察水源和绕行通路，失败时重新考虑。", companion_id="alice",
                    kind="method", title="浇水经验")
    store.save_note("farm", "玩家喜欢蓝莓", companion_id="alice")
    found = store.recall("farm", "今天浇水", companion_id="alice", kind="method")["notes"]
    assert len(found) == 1 and found[0]["title"] == "浇水经验"
    assert store.instruction_revision("farm") == 0
    assert store.recall("farm", "采矿", companion_id="alice")["notes"] == []


def test_identity_uses_selected_original_session_without_fallback(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "chat_sessions.json").write_text(json.dumps({"codex:farm": "session-a"}))
    (tmp_path / "data/companion-connection.json").write_text(json.dumps({"providerSessionId": "session-b"}))
    builtin = resolve_companion_id(tmp_path, "farm", provider="codex")
    assert builtin == resolve_companion_id(tmp_path, "farm", provider="codex", session_id="session-a")
    assert builtin != resolve_companion_id(tmp_path, "farm", provider="codex", connection_source="external")
    assert not resolve_companion_id(tmp_path, "farm", provider="mcode")
    assert not resolve_companion_id(tmp_path, "farm")
    assert CompanionMemoryStore(tmp_path / "memory.json").save_note("farm", "无主笔记", companion_id="")[0] == "rejected"


def test_large_library_uses_context_pointers_and_lossless_paged_recall(tmp_path):
    store = CompanionMemoryStore(tmp_path / "memory.json")
    prose = "观察绕行路线再试，不要把旧经验当现况。" * 500
    _, saved = store.save_note("farm", prose, companion_id="alice", kind="method", title="绕行")
    store.save_note("farm", "喜欢种花", companion_id="alice")
    context = store.render_for_context("farm", companion_id="alice", context_budget=900)
    assert len(json.dumps(context, ensure_ascii=False)) < 2000
    pointer = next(n for n in context["relevantNotes"] if n["id"] == saved["note"]["id"])
    assert pointer["recallRequired"] is True
    assert store.recall("farm", companion_id="alice", note_id=pointer["id"])["notes"][0]["text"] == prose
    first = store.recall("farm", companion_id="alice", limit=1)
    second = store.recall("farm", companion_id="alice", offset=first["nextOffset"], limit=1)
    assert first["notes"][0]["id"] != second["notes"][0]["id"]
    assert second["nextOffset"] is None


def test_legacy_shared_memory_can_be_corrected_and_withdrawn(tmp_path):
    store = CompanionMemoryStore(tmp_path / "memory.json")
    _, old = store.add("farm", "agreement", "每天去海边", "player", "1:spring:5", 0)
    status, updated = store.save_note("farm", "有空再去海边", companion_id="alice",
                                      note_id=old["entry"]["id"])
    assert status == "confirmed"
    new_id = updated["note"]["id"]
    shared = store.recall("farm", companion_id="bob")["entries"]
    assert [e["id"] for e in shared] == [new_id]
    assert shared[0]["text"] == "有空再去海边"
    assert store.recall("farm", companion_id="alice", note_id=old["entry"]["id"])["entries"][0]["superseded_by"] == new_id
    assert all(set(e) == {"id", "kind", "text", "source", "gameDate", "createdAt"}
               for e in store.list("farm")["entries"])
    status, _ = store.forget_note("farm", new_id, companion_id="bob", reason="不再约定")
    assert status == "confirmed"
    assert store.render_for_context("farm")["agreements"] == []


def test_edit_preserves_temporary_scope_until_explicitly_cleared(tmp_path):
    store = CompanionMemoryStore(tmp_path / "memory.json")
    _, saved = store.save_note("farm", "今天别砍树", companion_id="alice",
                               scope="今天休息", valid_on="1:spring:5")
    _, edited = store.save_note("farm", "今天先别砍树", companion_id="alice", note_id=saved["note"]["id"])
    assert edited["note"]["scope"] == "今天休息"
    assert edited["note"]["validOn"] == "1:spring:5"
    assert not store.recall("farm", companion_id="alice", game_date="1:spring:6")["notes"]
    _, lasting = store.save_note("farm", "幼树平时保留", companion_id="alice", note_id=edited["note"]["id"],
                                 scope="长期", valid_on="")
    assert "validOn" not in lasting["note"]
    assert store.recall("farm", companion_id="alice", game_date="1:spring:6")["notes"][0]["text"] == "幼树平时保留"


def test_default_recall_paginates_newest_first(tmp_path):
    store = CompanionMemoryStore(tmp_path / "memory.json")
    store.save_note("farm", "旧的浇水经验", companion_id="alice")
    _, latest = store.save_note("farm", "新的浇水经验", companion_id="alice")
    assert store.recall("farm", companion_id="alice", limit=1)["notes"][0]["id"] == latest["note"]["id"]
    assert store.recall("farm", "浇水", companion_id="alice", limit=1)["notes"][0]["id"] == latest["note"]["id"]


def test_continue_keeps_recent_context_without_crowding_out_agreements(tmp_path):
    store = CompanionMemoryStore(tmp_path / "memory.json")
    store.add("farm", "agreement", "保留幼树", "player", "1:spring:5", 0)
    store.add("farm", "event", "完成了浇水", "system", "1:spring:5", 1)
    store.save_note("farm", "刚决定把工具放回箱子", companion_id="alice")
    store.save_note("farm", "整理经验" * 1000, companion_id="alice", kind="method")
    context = store.render_for_context("farm", companion_id="alice", query="继续", context_budget=1500)
    assert context["agreements"][0]["text"] == "保留幼树"
    assert context["recentEvents"][0]["text"] == "完成了浇水"
    assert any(n["text"] == "刚决定把工具放回箱子" for n in context["relevantNotes"])
