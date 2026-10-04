import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from stardew_ai_runtime.usage_display import UsageDisplay
from stardew_ai_runtime.usage_meter import (
    AgyTurnReceipts,
    normalize_usage,
    read_agy_generations,
    recover_agy_receipts,
)

CID = "00000000-0000-4000-8000-000000000123"


def _varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _scalar(tag, value):
    return _varint(tag << 3) + _varint(value)


def _blob(tag, data):
    return _varint(tag << 3 | 2) + _varint(len(data)) + data


def _database(folder: Path):
    folder.mkdir()
    with sqlite3.connect(folder / f"{CID}.db") as db:
        db.execute("CREATE TABLE gen_metadata(idx INTEGER PRIMARY KEY,data BLOB)")
        db.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY,step_type INTEGER,metadata BLOB)")


def _generation(folder: Path, idx: int, response_id: str):
    usage = _scalar(1, 1320) + _scalar(2, 10) + _scalar(3, 3) + _scalar(5, 20) + _blob(11, response_id.encode())
    stamp = _scalar(1, int(datetime(2026, 10, 1, 16, 1, idx, tzinfo=UTC).timestamp()))
    with sqlite3.connect(folder / f"{CID}.db") as db:
        db.execute("INSERT INTO gen_metadata VALUES (?,?)", (idx, _blob(1, _blob(4, usage))))
        db.execute("INSERT INTO steps VALUES (?,15,?)", (idx, _blob(9, usage) + _blob(7, stamp)))


def test_agy_raw_and_normalized_counters_are_explicit_and_idempotent():
    raw = {"input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 3, "total_tokens": 13}
    normalized = normalize_usage("agy", raw)
    assert raw["total_tokens"] == 13 and normalized["raw_total_tokens"] == 13
    assert normalized["input_tokens"] == 30 and normalized["total_tokens"] == 33
    assert normalized["uncached_input_tokens"] == 10
    assert normalize_usage("agy", normalized) == normalized


def test_db_reader_measures_one_request_context_and_keeps_raw_counters(tmp_path):
    folder = tmp_path / "conversations"
    _database(folder)
    _generation(folder, 0, "a")
    _generation(folder, 1, "b")
    records = read_agy_generations(CID, 0, conversations_dir=folder)
    assert len(records) == 1 and records[0]["idx"] == 1
    assert records[0]["raw_usage"]["total_tokens"] == 13
    assert records[0]["usage"]["total_tokens"] == 33
    assert records[0]["usage"]["latestRequestInputContext"] == 30
    assert records[0]["timestamp"] == "2026-10-01T16:01:01+00:00"


def test_response_receipts_keep_unmetered_response_unknown_and_do_not_double_count(tmp_path):
    folder = tmp_path / "conversations"
    _database(folder)
    _generation(folder, 0, "known")
    turn = AgyTurnReceipts(tmp_path, request_id="autonomy-test", save_id="Save1", model="gemini-test",
                           conversations_dir=folder)
    turn.begin()
    turn.bind(CID)
    turn.log_file.write_text("URL: https://example/v1internal:streamGenerateContent?alt=sse ResponseID: auxiliary\n"
                             "URL: https://example/v1internal:streamGenerateContent?alt=sse ResponseID: known\n", encoding="utf-8")
    turn.finish("completed")
    turn.collect()
    responses = tmp_path / "data" / "model-usage.jsonl"
    rows = [json.loads(line) for line in responses.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    unknown = next(row for row in rows if row["responseId"] == "auxiliary")
    assert unknown["usage"] is None and unknown["model"] == ""
    journal = tmp_path / "commands.jsonl"
    journal.write_text(json.dumps({"timestamp":"2026-10-01T16:02:00Z","provider":"agy","model":"gemini-test",
        "requestId":"autonomy-test","saveId":"Save1","usage":{"input_tokens":10,"output_tokens":3,
        "cache_read_tokens":20,"total_tokens":13}}) + "\n", encoding="utf-8")
    text = UsageDisplay(journal, responses=responses).summary_text(today=False)
    assert "33+未记录 token" in text and "输入 30+未记录" in text
    assert "费用暂不可用" in text
    assert recover_agy_receipts(tmp_path, conversations_dir=folder) == 0


def test_restart_recovers_bound_turn_and_stops_at_next_same_cid_baseline(tmp_path):
    folder = tmp_path / "conversations"
    _database(folder)
    first = AgyTurnReceipts(tmp_path, request_id="old", save_id="Save1", model="gemini-test", conversations_dir=folder)
    first.begin()
    first.bind(CID)
    _generation(folder, 0, "old-response")
    second = AgyTurnReceipts(tmp_path, request_id="new", save_id="Save1", model="gemini-test",
                             conversation_id=CID, start_idx=0, conversations_dir=folder)
    second.begin()
    _generation(folder, 1, "new-response")
    assert recover_agy_receipts(tmp_path, conversations_dir=folder) == 2
    assert recover_agy_receipts(tmp_path, conversations_dir=folder) == 0
    rows = [json.loads(line) for line in (tmp_path / "data" / "model-usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(r["requestId"], r["responseId"]) for r in rows} == {("old","old-response"),("new","new-response")}


def test_unbound_turn_and_unsafe_log_path_never_search_other_sessions(tmp_path, monkeypatch):
    turn = AgyTurnReceipts(tmp_path, request_id="unbound", save_id="Save1", model="gemini-test")
    turn.begin()
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not search DB")))
    assert recover_agy_receipts(tmp_path) == 0
    turn.bind(CID)
    turn.state["logFile"] = "../unrelated.log"
    turn._persist()
    assert recover_agy_receipts(tmp_path) == 0


def test_truncated_metadata_is_unknown_but_explicit_zero_usage_is_known(tmp_path):
    folder = tmp_path / "conversations"
    _database(folder)
    with sqlite3.connect(folder / f"{CID}.db") as db:
        db.execute("INSERT INTO gen_metadata VALUES (0,?)", (b"\x0a\xff",))
        usage = _scalar(2, 0) + _scalar(3, 0) + _scalar(5, 0) + _blob(11, b"zero")
        db.execute("INSERT INTO gen_metadata VALUES (1,?)", (_blob(1, _blob(4, usage)),))
    rows = read_agy_generations(CID, conversations_dir=folder)
    assert len(rows) == 1 and rows[0]["idx"] == 1
    assert rows[0]["usage"]["total_tokens"] == 0 and rows[0]["timestamp"] is None


def test_inflight_unknown_response_can_be_completed_without_double_count(tmp_path):
    folder = tmp_path / "conversations"
    _database(folder)
    turn = AgyTurnReceipts(tmp_path, request_id="request", save_id="Save1", model="gemini-test",
                           conversation_id=CID, conversations_dir=folder)
    turn.begin()
    turn.log_file.write_text("URL: https://example/v1internal:streamGenerateContent?alt=sse ResponseID: response\n", encoding="utf-8")
    assert turn.collect() is None
    _generation(folder, 0, "response")
    turn.finish("completed")
    responses = tmp_path / "data" / "model-usage.jsonl"
    display = UsageDisplay(tmp_path / "commands.jsonl", responses=responses)
    usage = display.turn_response_usage("request", "Save1", "agy")
    assert usage["generations_count"] == 1 and usage["model_response_count"] == 1
    assert usage["unknown_response_count"] == 0 and usage["total_tokens"] == 33
    assert usage["latestRequestInputContext"] == 30


def test_malformed_receipt_rows_and_association_timestamps_are_ignored(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "model-usage.jsonl").write_text("[]\nnull\n{broken\n", encoding="utf-8")
    (tmp_path / "data" / "agy-turns.jsonl").write_text(json.dumps({"turnKey":"invalid","startedAt":0}) + "\n", encoding="utf-8")
    turn = AgyTurnReceipts(tmp_path, request_id="request", save_id="Save1", model="gemini-test")
    turn.begin()
    assert recover_agy_receipts(tmp_path) == 0


def test_partial_output_does_not_discard_measured_input_context(tmp_path):
    folder = tmp_path / "conversations"
    _database(folder)
    with sqlite3.connect(folder / f"{CID}.db") as db:
        usage = _scalar(2, 10) + _scalar(5, 20) + _blob(11, b"partial")
        db.execute("INSERT INTO gen_metadata VALUES (0,?)", (_blob(1, _blob(4, usage)),))
    records = read_agy_generations(CID, conversations_dir=folder)
    assert records[0]["usage"]["input_tokens"] == 30
    assert records[0]["usage"]["output_tokens"] is None and records[0]["usage"]["partial"]
    assert records[0]["usage"]["latestRequestInputContext"] == 30
    turn = AgyTurnReceipts(tmp_path, request_id="partial", save_id="Save1", model="gemini-test",
                           conversation_id=CID, conversations_dir=folder)
    turn.begin()
    turn.finish("cancelled")
    measured = turn.collect()
    assert measured["total_tokens"] == 30 and "output_tokens" not in measured
    assert measured["latestRequestInputContext"] == 30 and measured["input_context_measured"]


def test_closed_cancelled_turn_retries_transient_db_lock_on_restart(tmp_path, monkeypatch):
    folder = tmp_path / "conversations"
    _database(folder)
    _generation(folder, 0, "late-metering")
    turn = AgyTurnReceipts(tmp_path, request_id="cancelled", save_id="Save1", model="gemini-test",
                           conversation_id=CID, conversations_dir=folder)
    turn.begin()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", locked)
        turn.finish("cancelled")
    assert turn.state["status"] == "cancelled" and turn.state["meteringPending"]
    original_finished_at = turn.state["finishedAt"]
    assert recover_agy_receipts(tmp_path, conversations_dir=folder) == 1
    assert recover_agy_receipts(tmp_path, conversations_dir=folder) == 0
    latest = json.loads((tmp_path / "data" / "agy-turns.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert latest["status"] == "cancelled" and not latest["meteringPending"]
    assert latest["finishedAt"] == original_finished_at and latest["endGenIdx"] == 0


def test_partial_response_upgrade_updates_usage_completion_date_and_ui(tmp_path):
    folder = tmp_path / "conversations"
    _database(folder)
    with sqlite3.connect(folder / f"{CID}.db") as db:
        partial = _scalar(2, 10) + _scalar(5, 20) + _blob(11, b"upgrade")
        db.execute("INSERT INTO gen_metadata VALUES (0,?)", (_blob(1, _blob(4, partial)),))
    turn = AgyTurnReceipts(tmp_path, request_id="request", save_id="Save1", model="gemini-test",
                           conversation_id=CID, conversations_dir=folder)
    turn.begin()
    turn.finish("completed")
    assert turn.state["meteringPending"]
    journal = tmp_path / "commands.jsonl"
    journal.write_text(json.dumps({"timestamp":"2026-10-01T16:02:00Z","provider":"agy","requestId":"request",
        "saveId":"Save1","usage":{"input_tokens":10,"output_tokens":3,"cache_read_tokens":20,"total_tokens":13}}) + "\n", encoding="utf-8")
    display = UsageDisplay(journal, responses=tmp_path / "data" / "model-usage.jsonl")
    assert "未记录" in display.summary_text(today=False)
    with sqlite3.connect(folder / f"{CID}.db") as db:
        db.execute("DELETE FROM gen_metadata WHERE idx=0")
    _generation(folder, 0, "upgrade")
    assert recover_agy_receipts(tmp_path, conversations_dir=folder) == 0  # updated one known ID
    turn_after = json.loads((tmp_path / "data" / "agy-turns.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert not turn_after["meteringPending"] and turn_after["status"] == "completed"
    now = datetime.fromisoformat("2026-10-02T00:10:00+08:00")
    text = display.today_text(now)
    assert "33 token（输入 30，其中缓存 20；输出 3）" in text
    assert "未记录" not in text and "日期无法确定" not in text
    stored = next(iter(display._responses._records.values()))
    assert stored["usage"]["partial"] is False and stored["dateUnallocated"] is False
    assert stored["timestamp"] == "2026-10-01T16:01:00+00:00"
