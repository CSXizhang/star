"""Usage UI from installation-owned receipts, without provider/model calls."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from stardew_ai_runtime.usage_display import UsageDisplay

NOW = datetime.fromisoformat("2026-10-02T00:10:00+08:00")


def _receipt(request_id="request-1", *, provider="codex", save_id="Save1",
             timestamp="2026-10-01T16:00:00Z", usage=None, model="game-test", **extra):
    return {"timestamp": timestamp, "provider": provider, "requestId": request_id,
            "saveId": save_id, "model": model, "usage": usage, **extra}


def _codex_usage(input_tokens=100, output_tokens=10, cache=60):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_tokens": cache}


def _append(journal: Path, *receipts):
    with journal.open("a", encoding="utf-8") as stream:
        for receipt in receipts:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")


def _assert_counts(text: str, *, total, input_tokens, cache, output):
    assert f"{total} token（输入 {input_tokens}，其中缓存 {cache}；输出 {output}）" in text


def test_usage_follows_real_local_date_not_game_date(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal,
            _receipt("prior", timestamp="2026-10-01T15:59:59Z", usage=_codex_usage(999, 999), gameDate="1:spring:1"),
            _receipt("today", usage=_codex_usage(), gameDate="20:winter:28"),
            _receipt("next", timestamp="2026-10-02T16:00:00Z", usage=_codex_usage(999, 999), gameDate="1:spring:1"))
    display = UsageDisplay(journal)
    text = display.today_text(NOW)
    assert "今日用量（2026-10-02）" in text
    _assert_counts(text, total="110", input_tokens="100", cache="60", output="10")
    tomorrow = display.today_text(datetime.fromisoformat("2026-10-03T00:10:00+08:00"))
    _assert_counts(tomorrow, total="1,998", input_tokens="999", cache="60", output="999")


def test_same_installation_aggregates_calls_across_save_switches(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    display = UsageDisplay(journal)
    _append(journal, _receipt("save-a", save_id="SaveA", usage=_codex_usage()))
    _assert_counts(display.today_text(NOW), total="110", input_tokens="100", cache="60", output="10")
    _append(journal, _receipt("save-b", save_id="SaveB", usage=_codex_usage(200, 20, 100)))
    _assert_counts(display.today_text(NOW), total="330", input_tokens="300", cache="160", output="30")


def test_duplicate_request_is_counted_once_incrementally_and_after_restart(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    receipt = _receipt(usage=_codex_usage())
    _append(journal, _receipt(usage=None), receipt)
    display = UsageDisplay(journal)
    before = display.today_text(NOW)
    _append(journal, receipt)
    assert display.today_text(NOW) == before
    assert UsageDisplay(journal).today_text(NOW) == before
    _assert_counts(before, total="110", input_tokens="100", cache="60", output="10")


def test_same_request_id_from_different_providers_is_not_a_duplicate(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt(usage=_codex_usage()), _receipt(provider="other", usage=_codex_usage()))
    _assert_counts(UsageDisplay(journal).today_text(NOW), total="220", input_tokens="200", cache="120", output="20")


def test_codex_cache_is_part_of_input_and_reasoning_is_part_of_output(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    usage = {**_codex_usage(), "reasoning_output_tokens": 8, "thinking_tokens": 8}
    _append(journal, _receipt(usage=usage))
    _assert_counts(UsageDisplay(journal).today_text(NOW), total="110", input_tokens="100", cache="60", output="10")


def test_kimi_normalized_input_other_plus_cache_read_and_creation(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    # Our receipt stores wire inputOther as input_tokens, with cache read and
    # creation in separate fields. All three make up Kimi's complete input.
    usage = {"input_tokens": 100, "cache_read_tokens": 60, "cache_creation_tokens": 20,
             "output_tokens": 10}
    _append(journal, _receipt(provider="kimi", usage=usage))
    _assert_counts(UsageDisplay(journal).today_text(NOW), total="190", input_tokens="180", cache="60", output="10")


def test_missing_counts_are_unknown_and_partial_aggregate_is_marked(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt("known", usage=_codex_usage()), _receipt("unknown", usage={"input_tokens": 50}))
    _assert_counts(UsageDisplay(journal).today_text(NOW), total="110+未记录", input_tokens="150", cache="60+未记录", output="10+未记录")


def test_missing_kimi_cache_fields_do_not_become_zero(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt(provider="kimi", usage={"input_tokens": 10, "output_tokens": 5}))
    _assert_counts(UsageDisplay(journal).today_text(NOW), total="未记录", input_tokens="未记录", cache="未记录", output="5")


@pytest.mark.parametrize("invalid", [None, True, -1, "10", 1.5])
def test_invalid_or_missing_usage_values_are_unknown(tmp_path, invalid):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt(usage={"input_tokens": invalid, "output_tokens": invalid,
                                     "cache_read_tokens": invalid, "total_tokens": invalid}))
    _assert_counts(UsageDisplay(journal).today_text(NOW), total="未记录", input_tokens="未记录", cache="未记录", output="未记录")


def test_explicit_zero_usage_remains_known_zero(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt(usage=_codex_usage(0, 0, 0)))
    _assert_counts(UsageDisplay(journal).today_text(NOW), total="0", input_tokens="0", cache="0", output="0")


def test_optional_provided_cost_and_api_equivalent_estimate_have_distinct_labels(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal,
            _receipt("provided", usage={**_codex_usage(), "cost_usd": 0.25, "estimated_api_cost_usd": 99}),
            _receipt("estimated", usage={**_codex_usage(), "estimated_api_cost_usd": 0.5}))
    text = UsageDisplay(journal).today_text(NOW)
    assert "已提供费用 $0.2500（部分调用）" in text
    assert "API 等价估算 $0.5000（部分调用）" in text
    assert "$99" not in text


def test_user_prices_produce_api_equivalent_estimate_without_claiming_actual_charge(tmp_path):
    journal, prices = tmp_path / "chat_commands.jsonl", tmp_path / "usage-prices.json"
    _append(journal, _receipt(usage=_codex_usage(1_000_000, 100_000, 400_000)))
    prices.write_text(json.dumps({"codex": {"game-test": {"uncachedInput": 2, "cachedInput": 0.5, "output": 8}}}), encoding="utf-8")
    text = UsageDisplay(journal, prices).today_text(NOW)
    assert "API 等价估算 $2.2000" in text
    assert "已提供费用" not in text


def test_kimi_price_estimate_uses_separate_cache_creation_rate(tmp_path):
    journal, prices = tmp_path / "chat_commands.jsonl", tmp_path / "usage-prices.json"
    usage = {"input_tokens": 600_000, "cache_read_tokens": 300_000,
             "cache_creation_tokens": 100_000, "output_tokens": 100_000}
    _append(journal, _receipt(provider="kimi", usage=usage))
    prices.write_text(json.dumps({"kimi": {"game-test": {
        "uncachedInput": 2, "cachedInput": 0.5, "cacheWrite": 2.5, "output": 8}}}), encoding="utf-8")
    assert "API 等价估算 $2.4000" in UsageDisplay(journal, prices).today_text(NOW)


def test_missing_price_or_cache_creation_rate_does_not_invent_cost(tmp_path):
    journal, prices = tmp_path / "chat_commands.jsonl", tmp_path / "usage-prices.json"
    _append(journal, _receipt(provider="kimi", usage={"input_tokens": 100, "cache_read_tokens": 60,
                                                     "cache_creation_tokens": 20, "output_tokens": 10}))
    assert "费用暂不可用" in UsageDisplay(journal).today_text(NOW)
    prices.write_text(json.dumps({"kimi": {"game-test": {"uncachedInput": 2, "cachedInput": 0.5, "output": 8}}}), encoding="utf-8")
    assert "费用暂不可用" in UsageDisplay(journal, prices).today_text(NOW)


def test_partial_trailing_receipt_is_ignored_then_read_once_after_completion(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt("first", usage=_codex_usage()))
    tail = json.dumps(_receipt("second", usage=_codex_usage(200, 20, 100))) + "\n"
    split = len(tail) // 2
    with journal.open("a", encoding="utf-8") as stream:
        stream.write(tail[:split])
    display = UsageDisplay(journal)
    _assert_counts(display.today_text(NOW), total="110", input_tokens="100", cache="60", output="10")
    with journal.open("a", encoding="utf-8") as stream:
        stream.write(tail[split:])
    complete = display.today_text(NOW)
    _assert_counts(complete, total="330", input_tokens="300", cache="160", output="30")
    assert display.today_text(NOW) == complete
    assert UsageDisplay(journal).today_text(NOW) == complete


def test_inaccessible_journal_reports_unknown_even_after_previous_successful_read(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt(usage=_codex_usage()))
    display = UsageDisplay(journal)
    display.today_text(NOW)
    with patch.object(Path, "open", side_effect=PermissionError("journal denied")):
        text = display.today_text(NOW)
    assert "暂不可读取" in text
    assert "110 token" not in text and "输入 0" not in text


def test_later_complete_receipt_enriches_unknown_fields_without_duplicate_counting(tmp_path):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt(usage={"input_tokens": 100, "output_tokens": None}))
    display = UsageDisplay(journal)
    assert "输出 未记录" in display.today_text(NOW)
    _append(journal, _receipt(usage=_codex_usage()))
    _assert_counts(display.today_text(NOW), total="110", input_tokens="100", cache="60", output="10")
    assert UsageDisplay(journal).today_text(NOW) == display.today_text(NOW)


@pytest.mark.parametrize("timestamp", [123, None, {}, "bad-date"])
def test_invalid_completed_receipt_does_not_break_other_valid_usage(tmp_path, timestamp):
    journal = tmp_path / "chat_commands.jsonl"
    _append(journal, _receipt("bad", timestamp=timestamp, usage=_codex_usage()), _receipt("good", usage=_codex_usage()))
    _assert_counts(UsageDisplay(journal).today_text(NOW), total="110", input_tokens="100", cache="60", output="10")


@pytest.mark.parametrize("provider_prices", [None, [], "bad-price-data", 10])
def test_malformed_price_group_keeps_usage_visible_and_cost_unknown(tmp_path, provider_prices):
    journal, prices = tmp_path / "chat_commands.jsonl", tmp_path / "usage-prices.json"
    _append(journal, _receipt(usage=_codex_usage()))
    prices.write_text(json.dumps({"codex": provider_prices}), encoding="utf-8")
    text = UsageDisplay(journal, prices).today_text(NOW)
    _assert_counts(text, total="110", input_tokens="100", cache="60", output="10")
    assert "费用暂不可用" in text


@pytest.mark.parametrize("terminal", [False, True])
def test_response_dates_survive_midnight_terminal_and_restart(tmp_path, terminal):
    journal, responses = tmp_path / "commands.jsonl", tmp_path / "responses.jsonl"
    usage = {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10,
             "cache_read_tokens": 60, "input_includes_cache": True}
    before = _receipt(provider="dsh", timestamp="2026-10-01T15:59:59Z", usage=usage, responseId="a")
    after = _receipt(provider="dsh", timestamp="2026-10-01T16:00:01Z", usage=usage, responseId="b")
    _append(responses, before, after, after)
    if terminal:
        _append(journal, _receipt(provider="dsh", usage={**usage, "total_tokens": 200,
            "input_tokens": 180, "output_tokens": 20, "cache_read_tokens": 120, "cost_usd": 0.25}))
    for display in (UsageDisplay(journal, responses=responses), UsageDisplay(journal, responses=responses)):
        suffix = "" if terminal else "+未记录"
        for now in (NOW, datetime.fromisoformat("2026-10-01T23:59:59+08:00")):
            text = display.today_text(now, save_id="Save1")
            _assert_counts(text, total="100" + suffix, input_tokens="90" + suffix,
                           cache="60" + suffix, output="10" + suffix)
            assert "已提供费用" not in text  # Whole-turn charge cannot be assigned to a single date.
        cumulative = display.summary_text(NOW, save_id="Save1", today=False)
        assert "200" + suffix + " token" in cumulative
        if terminal:
            assert "已提供费用 $0.2500" in cumulative


def test_delayed_terminal_does_not_move_yesterday_response_to_today(tmp_path):
    journal, responses = tmp_path / "commands.jsonl", tmp_path / "responses.jsonl"
    usage = {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10,
             "cache_read_tokens": 60, "input_includes_cache": True}
    _append(responses, _receipt(provider="dsh", timestamp="2026-10-01T15:59:59Z", usage=usage, responseId="a"))
    _append(journal, _receipt(provider="dsh", usage=usage))
    assert "暂无已记录" in UsageDisplay(journal, responses=responses).today_text(NOW)


def test_cross_midnight_terminal_remainder_is_not_double_counted(tmp_path):
    journal, responses = tmp_path / "commands.jsonl", tmp_path / "responses.jsonl"
    usage = {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10,
             "cache_read_tokens": 60, "input_includes_cache": True}
    _append(responses, _receipt(provider="dsh", timestamp="2026-10-01T15:59:59Z", usage=usage, responseId="a"))
    _append(journal, _receipt(provider="dsh", usage={**usage, "total_tokens": 200,
        "input_tokens": 180, "output_tokens": 20, "cache_read_tokens": 120}))
    display = UsageDisplay(journal, responses=responses)
    assert "暂无已记录" in display.today_text(NOW)
    assert "另有 100 token 日期无法确定，已计入累计" in display.today_text(NOW)
    assert "200 token" in display.summary_text(NOW, today=False)


def test_legacy_agy_separate_cache_is_included_without_rewriting_raw_journal(tmp_path):
    journal = tmp_path / "commands.jsonl"
    raw = {"input_tokens": 100, "cache_read_tokens": 300, "output_tokens": 10,
           "total_tokens": 110, "source": "db_gen_metadata_delta"}
    _append(journal, _receipt(provider="agy", usage=raw))
    original = journal.read_bytes()
    display = UsageDisplay(journal)
    _assert_counts(display.today_text(NOW), total="410", input_tokens="400", cache="300", output="10")
    assert "费用暂不可用" in display.today_text(NOW)
    assert journal.read_bytes() == original
    assert UsageDisplay(journal).today_text(NOW) == display.today_text(NOW)


def test_inclusive_agy_receipt_and_legacy_terminal_do_not_double_add_cache(tmp_path):
    journal, responses = tmp_path / "commands.jsonl", tmp_path / "responses.jsonl"
    _append(journal, _receipt(provider="agy", usage={"input_tokens": 100, "cache_read_tokens": 300,
            "output_tokens": 10, "total_tokens": 110}))
    _append(responses, _receipt(provider="agy", responseId="measured", usage={
        "input_tokens": 400, "cache_read_tokens": 300, "output_tokens": 10,
        "total_tokens": 410, "input_includes_cache": True}))
    display = UsageDisplay(journal, responses=responses)
    _assert_counts(display.today_text(NOW), total="410", input_tokens="400", cache="300", output="10")
    _append(responses, _receipt(provider="agy", responseId="auxiliary", usage=None))
    _assert_counts(display.today_text(NOW), total="410+未记录", input_tokens="400+未记录",
                   cache="300+未记录", output="10+未记录")
    assert "费用暂不可用" in display.today_text(NOW)


def test_partial_agy_cache_stays_unknown_not_silent_zero(tmp_path):
    journal = tmp_path / "commands.jsonl"
    _append(journal, _receipt(provider="agy", usage={"input_tokens": 100, "output_tokens": 10,
            "total_tokens": 110}))
    text = UsageDisplay(journal).today_text(NOW)
    assert "110+未记录 token" in text
    assert "输入 100+未记录" in text and "缓存 未记录" in text


def test_agy_midnight_receipts_are_normalized_before_terminal_comparison(tmp_path):
    journal, responses = tmp_path / "commands.jsonl", tmp_path / "responses.jsonl"
    raw = {"input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 3, "total_tokens": 13}
    _append(responses, _receipt(provider="agy", responseId="a", timestamp="2026-10-01T15:59:59Z", usage=raw),
            _receipt(provider="agy", responseId="b", timestamp="2026-10-01T16:00:01Z", usage=raw))
    _append(journal, _receipt(provider="agy", usage={"input_tokens": 20, "cache_read_tokens": 40,
            "output_tokens": 6, "total_tokens": 26}))
    display = UsageDisplay(journal, responses=responses)
    _assert_counts(display.today_text(NOW), total="33", input_tokens="30", cache="20", output="3")
    assert "66 token" in display.summary_text(NOW, today=False)


def test_mixed_agy_response_dates_do_not_hide_measured_date_when_another_is_unknown(tmp_path):
    journal = tmp_path / "commands.jsonl"
    responses = tmp_path / "responses.jsonl"
    usage = {"input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 3, "total_tokens": 13}
    _append(responses, _receipt(provider="agy", responseId="unallocated", usage=usage, dateUnallocated=True),
            _receipt(provider="agy", responseId="today", usage=usage))
    display = UsageDisplay(journal, responses=responses)
    text = display.today_text(NOW)
    assert "33+未记录 token" in text and "另有 33 token 日期无法确定" in text
    assert "66+未记录 token" in display.summary_text(today=False)
