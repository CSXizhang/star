"""Tests for Kimi wire usage metering (provider-specific usage.record)."""

from __future__ import annotations

import json
from pathlib import Path

from stardew_ai_runtime.kimi_wire_usage import (
    aggregate_usage_records,
    read_usage_since,
    resolve_wire_path,
    wire_offset,
)

SESSION = "session_00000000-0000-4000-8000-000000000001"


def _usage_line(input_other: int, output: int, cache_read: int, cache_creation: int = 0) -> str:
    return json.dumps({
        "type": "usage.record",
        "agentId": "main",
        "model": "kimi-code/k3",
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": cache_read,
            "inputCacheCreation": cache_creation,
        },
        "usageScope": "turn",
        "time": 1789488102273,
    })


def _write_wire(home: Path, cwd: Path, workspace_id: str, session_id: str, lines: list[str]) -> Path:
    _ = cwd
    wire = home / ".kimi-code" / "sessions" / workspace_id / session_id / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return wire


def _write_workspaces(home: Path, workspace_id: str, root: Path) -> None:
    base = home / ".kimi-code"
    base.mkdir(parents=True, exist_ok=True)
    (base / "workspaces.json").write_text(
        json.dumps({"version": 1, "workspaces": {workspace_id: {"root": str(root)}}}),
        encoding="utf-8",
    )


def test_resolve_wire_path_uses_workspace_mapping(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_workspaces(tmp_path, "wd_star_abc", cwd)
    wire = _write_wire(tmp_path, cwd, "wd_star_abc", SESSION, [_usage_line(1, 2, 3)])

    assert resolve_wire_path(SESSION, cwd=cwd, home=tmp_path) == wire


def test_resolve_wire_path_uses_session_index_without_workspace(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / SESSION
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text(_usage_line(1, 2, 3) + "\n", encoding="utf-8")
    base = tmp_path / ".kimi-code"
    base.mkdir(parents=True, exist_ok=True)
    (base / "session_index.jsonl").write_text(
        json.dumps({"sessionId": SESSION, "sessionDir": str(session_dir)}) + "\n",
        encoding="utf-8",
    )

    assert resolve_wire_path(SESSION, cwd=tmp_path / "nope", home=tmp_path) == wire


def test_aggregate_sums_records_and_computes_total() -> None:
    text = "\n".join([
        '{"type":"turn.prompt"}',
        _usage_line(590, 506, 24064, 0),
        _usage_line(781, 93, 24576, 100),
    ])
    aggregate = aggregate_usage_records(text)

    assert aggregate["generations_count"] == 2
    assert aggregate["input_tokens"] == 1371
    assert aggregate["output_tokens"] == 599
    assert aggregate["cache_read_tokens"] == 48640
    assert aggregate["cache_creation_tokens"] == 100
    assert aggregate["total_tokens"] == 1371 + 599 + 48640 + 100
    assert aggregate["unknown"] is False
    assert aggregate["source"] == "kimi_wire_usage_record"
    assert aggregate["thinking_tokens"] is None
    assert aggregate["prompt_tokens"] is None
    assert aggregate["turn_prompt_count"] == 1


def test_aggregate_missing_field_is_unknown_not_zero() -> None:
    # A future/renamed schema that drops inputCacheRead must not report 0.
    text = json.dumps({
        "type": "usage.record",
        "model": "kimi-code/k3",
        "usage": {"inputOther": 10, "output": 5},
    })
    aggregate = aggregate_usage_records(text)

    assert aggregate["input_tokens"] == 10
    assert aggregate["output_tokens"] == 5
    assert aggregate["cache_read_tokens"] is None
    assert aggregate["total_tokens"] is None
    assert aggregate["unknown"] is True
    assert "inputCacheRead" in aggregate["unknown_fields"]


def test_aggregate_malformed_usage_is_unknown() -> None:
    text = json.dumps({"type": "usage.record", "model": "kimi-code/k3"})
    aggregate = aggregate_usage_records(text)

    assert aggregate["generations_count"] == 0
    assert aggregate["unknown_records"] == 1
    assert aggregate["unknown"] is True
    assert aggregate["total_tokens"] is None


def test_read_usage_since_honours_offset(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_workspaces(tmp_path, "wd_star_abc", cwd)
    first = _usage_line(100, 10, 1000)
    second = _usage_line(200, 20, 2000)
    wire = _write_wire(tmp_path, cwd, "wd_star_abc", SESSION, [first, second])

    # Offset captured before a resumed turn points just after the first record.
    offset = len((first + "\n").encode("utf-8"))
    assert wire_offset(SESSION, cwd=cwd, home=tmp_path) == wire.stat().st_size

    usage = read_usage_since(SESSION, offset, cwd=cwd, home=tmp_path)

    assert usage is not None
    assert usage["generations_count"] == 1
    assert usage["input_tokens"] == 200
    assert usage["output_tokens"] == 20
    assert usage["cache_read_tokens"] == 2000
    assert usage["wireOffsetStart"] == offset


def test_read_usage_since_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_usage_since(SESSION, 0, cwd=tmp_path, home=tmp_path) is None


def test_read_usage_since_no_records_returns_none(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_workspaces(tmp_path, "wd_star_abc", cwd)
    _write_wire(tmp_path, cwd, "wd_star_abc", SESSION, ['{"type":"turn.prompt"}'])

    assert read_usage_since(SESSION, 0, cwd=cwd, home=tmp_path) is None


def test_wire_offset_is_zero_without_session(tmp_path: Path) -> None:
    assert wire_offset(None, cwd=tmp_path, home=tmp_path) == 0
