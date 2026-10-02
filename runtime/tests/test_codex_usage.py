import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from stardew_ai_runtime.agent_backends import CodexBackend
from stardew_ai_runtime.codex_usage import CodexUsageMeter, normalize_usage

SESSION = "01a0f177-c802-7622-8e37-82dc15d95cc8"


def rollout(home: Path, usage: dict | None = None) -> Path:
    path = home / "sessions/2026/09/30" / f"rollout-2026-09-30T16-39-14-{SESSION}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SESSION, "timestamp": "2026-09-30T08:39:14.355Z"}}) + "\n", encoding="utf-8")
    if usage is not None:
        append_usage(path, usage)
    return path


def append_usage(path: Path, usage: dict, last_usage: dict | None = None) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": usage,
            "last_token_usage": last_usage}}}) + "\n")


def test_resume_delta_uses_total_once_and_normalizes_cached_subset(tmp_path):
    before = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
              "reasoning_output_tokens": 5, "total_tokens": 110}
    path = rollout(tmp_path, before)
    meter = CodexUsageMeter(SESSION, home=tmp_path)
    after = {"input_tokens": 300, "cached_input_tokens": 250, "output_tokens": 30,
             "reasoning_output_tokens": 12, "total_tokens": 330}
    append_usage(path, after, {"input_tokens": 99999, "cached_input_tokens": 80000})
    append_usage(path, after, {"input_tokens": 99999, "cached_input_tokens": 80000})
    result = meter.finish(SESSION, {"input_tokens": 300})
    assert result["usage"] == {"input_tokens": 200, "cache_read_input_tokens": 170,
                                "output_tokens": 20, "reasoning_output_tokens": 7, "total_tokens": 220}
    assert result["usage_request_count"] == 1
    assert result["usage_source"] == "codex_rollout_cumulative_delta"
    assert result["usage_latest_request_input_context"] == 99999
    assert "latest_request_input_context" not in result["usage"]


@pytest.mark.parametrize("last_usage", [None, {}, {"input_tokens": True},
                                         {"input_tokens": -1}, {"input_tokens": "100000"}])
def test_context_requires_explicit_valid_latest_request_input(tmp_path, last_usage):
    path = rollout(tmp_path, {"input_tokens": 100})
    meter = CodexUsageMeter(SESSION, home=tmp_path)
    append_usage(path, {"input_tokens": 200000}, {"input_tokens": 100000})
    append_usage(path, {"input_tokens": 300000}, last_usage)
    result = meter.finish(SESSION)
    assert result["usage"]["input_tokens"] == 299900
    assert "usage_latest_request_input_context" not in result


def test_context_measurement_is_independent_of_missing_billing_baseline(tmp_path):
    meter = CodexUsageMeter(SESSION, home=tmp_path)
    path = rollout(tmp_path)
    append_usage(path, {"input_tokens": 700000}, {"input_tokens": 100000})
    result = meter.finish(SESSION)
    assert result["usage"] is None
    assert result["usage_status"] == "unknown"
    assert result["usage_latest_request_input_context"] == 100000
    assert "usage_latest_request_input_context" not in meter.finish("foreign-thread")


def test_cli_cumulative_usage_never_invents_request_context(tmp_path):
    meter = CodexUsageMeter(None, home=tmp_path)
    result = meter.finish(SESSION, {"input_tokens": 100000})
    assert result["usage"]["input_tokens"] == 100000
    assert "usage_latest_request_input_context" not in result


@pytest.mark.parametrize("request_input", [0, 100000])
def test_fresh_owned_context_measurement_and_old_thread_exclusion(tmp_path, request_input):
    meter = CodexUsageMeter(None, home=tmp_path)
    meter.started = datetime(2026, 9, 30, 8, 39, 14, tzinfo=UTC)
    path = rollout(tmp_path)
    append_usage(path, {"input_tokens": 700000}, {"input_tokens": request_input})
    result = meter.finish(SESSION)
    assert result["usage_latest_request_input_context"] == request_input
    assert result["usage"]["input_tokens"] == 700000
    old = CodexUsageMeter(None, home=tmp_path)
    old.started = meter.started + timedelta(days=1)
    assert "usage_latest_request_input_context" not in old.finish(SESSION)


def test_missing_resume_baseline_never_counts_entire_history(tmp_path):
    meter = CodexUsageMeter(SESSION, home=tmp_path)
    rollout(tmp_path, {"input_tokens": 500, "output_tokens": 2, "total_tokens": 502})
    assert meter.finish(SESSION, {"input_tokens": 500})["usage"] is None
    assert meter.finish(SESSION)["usage_status"] == "unknown"


def test_fresh_rollout_records_interrupted_turn_without_cli_completion(tmp_path):
    meter = CodexUsageMeter(None, home=tmp_path)
    meter.started = datetime(2026, 9, 30, 8, 39, 14, tzinfo=UTC)
    path = rollout(tmp_path, {"input_tokens": 720038, "cached_input_tokens": 709888,
                              "output_tokens": 2295, "reasoning_output_tokens": 759,
                              "total_tokens": 722333})
    with path.open("a") as stream:
        stream.write('{"type":"event_msg",')
    result = meter.finish(SESSION)
    assert result["usage"]["total_tokens"] == 722333
    assert result["usage"]["cache_read_input_tokens"] == 709888


def test_foreign_thread_and_old_new_thread_are_not_counted(tmp_path):
    rollout(tmp_path, {"input_tokens": 100})
    meter = CodexUsageMeter(None, home=tmp_path)
    meter.started = datetime.now(UTC) + timedelta(days=1)
    assert meter.finish(SESSION)["usage"] is None
    existing = CodexUsageMeter(SESSION, home=tmp_path)
    assert existing.finish("some-other-thread", {"input_tokens": 50})["usage"] is None


def test_new_cli_cache_alias_preserves_unknown_fields():
    assert normalize_usage({"input_tokens": 10, "cached_input_tokens": 8}) == {
        "input_tokens": 10, "cache_read_input_tokens": 8}
    assert normalize_usage({"input_tokens": True, "output_tokens": -1}) is None


def test_backend_cancel_and_timeout_results_keep_recorded_usage(tmp_path):
    for error in ("TIMEOUT", "CANCELLED"):
        home = tmp_path / error
        path = rollout(home, {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})

        def stopped(*args, path=path, error=error):
            append_usage(path, {"input_tokens": 130, "output_tokens": 13, "total_tokens": 143})
            return {"success": False, "conversation_id": SESSION, "usage": None, "error": error}

        with patch.dict("os.environ", {"CODEX_HOME": str(home)}), patch.object(CodexBackend, "_run", side_effect=stopped):
            result = CodexBackend(tmp_path).run(None, SESSION, "next")
        assert result["success"] is False
        assert result["error"] == error
        assert result["usage"]["total_tokens"] == 33
