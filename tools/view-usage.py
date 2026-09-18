#!/usr/bin/env python3
"""Antigravity Stardew AI Companion Token Usage Viewer.

Parses per-turn token usage (input, output, cached, thinking) and MCP tool call history
from SQLite conversation databases (~/.gemini/antigravity-cli/conversations/<cid>.db)
and transcript logs.

Supports both formatted terminal output and self-contained interactive HTML dashboards.
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import pathlib
import sqlite3
import sys
from typing import Any


def fmt_token(value: Any) -> str:
    """Render a token counter; unknown stays '未知' instead of a fake zero."""
    if value is None:
        return "未知"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    res = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, offset
        shift += 7
    return res, offset


def parse_protobuf(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    offset = 0
    fields: dict[int, list[tuple[int, Any]]] = {}
    while offset < len(data):
        try:
            tag_val, offset = decode_varint(data, offset)
        except Exception:
            break
        tag = tag_val >> 3
        wire = tag_val & 7
        if wire == 0:  # Varint
            val, offset = decode_varint(data, offset)
        elif wire == 2:  # Length-delimited (string / bytes / embedded message)
            length, offset = decode_varint(data, offset)
            val = data[offset : offset + length]
            offset += length
        elif wire == 1:  # 64-bit fixed
            val = data[offset : offset + 8]
            offset += 8
        elif wire == 5:  # 32-bit fixed
            val = data[offset : offset + 4]
            offset += 4
        else:
            break
        fields.setdefault(tag, []).append((wire, val))
    return fields


def parse_usage_from_gen_metadata(data: bytes) -> dict[str, int]:
    """Decodes GenerationMetadata -> UsageMetadata from protobuf blob.

    Protobuf schema in gen_metadata.data:
      Tag 1: GenerationMetadata
        Tag 4: UsageMetadata
          Tag 1: prompt_tokens (varint)
          Tag 2: input_tokens / candidates_token_count (varint)
          Tag 3: output_tokens (varint)
          Tag 5: cache_read_tokens (varint)
          Tag 9: thinking_tokens (varint)
    """
    usage = {
        "prompt_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "thinking_tokens": 0,
        "total_tokens": 0,
    }
    if not data:
        return usage

    f = parse_protobuf(data)
    if 1 not in f:
        return usage

    wire, val = f[1][0]
    if wire != 2 or not isinstance(val, bytes):
        return usage

    gen_meta = parse_protobuf(val)
    if 4 not in gen_meta:
        return usage

    wire4, val4 = gen_meta[4][0]
    if wire4 != 2 or not isinstance(val4, bytes):
        return usage

    u = parse_protobuf(val4)
    usage["prompt_tokens"] = u.get(1, [(0, 0)])[0][1]
    usage["input_tokens"] = u.get(2, [(0, 0)])[0][1]
    usage["output_tokens"] = u.get(3, [(0, 0)])[0][1]
    usage["cache_read_tokens"] = u.get(5, [(0, 0)])[0][1]
    usage["thinking_tokens"] = u.get(9, [(0, 0)])[0][1]
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def read_conversation_db(db_path: pathlib.Path) -> dict[str, Any] | None:
    if not db_path.is_file():
        return None

    cid = db_path.stem
    con = None
    try:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        # 1. Turns usage
        turns: list[dict[str, Any]] = []
        try:
            for idx, data, size in con.execute(
                "SELECT idx, data, size FROM gen_metadata ORDER BY idx"
            ):
                u = parse_usage_from_gen_metadata(data)
                u["turn"] = idx
                turns.append(u)
        except Exception:
            pass

        # 2. Steps for user prompt & tool calls
        user_prompt = ""
        user_prompt_full = ""
        stardew_tools: list[str] = []
        all_tool_calls: list[dict[str, Any]] = []

        try:
            for idx, stype, payload in con.execute(
                "SELECT idx, step_type, step_payload FROM steps ORDER BY idx"
            ):
                text = (
                    payload.decode("utf-8", errors="replace")
                    if isinstance(payload, bytes)
                    else (payload or "")
                )
                if not user_prompt and "<USER_REQUEST>" in text:
                    start = text.find("<USER_REQUEST>") + len("<USER_REQUEST>")
                    end = text.find("</USER_REQUEST>", start)
                    if end > start:
                        clean_body = text[start:end].strip()
                        user_prompt_full = clean_body
                        first_line = clean_body.split("\n")[0].strip()
                        user_prompt = first_line[:100]

                # Check for MCP tool calls
                if "stardew-" in text or "call_mcp_tool" in text or "ToolName" in text:
                    # Find tool name
                    pos = text.find('"ToolName"')
                    if pos >= 0:
                        colon = text.find(":", pos)
                        if colon >= 0:
                            rem = text[colon + 1 : colon + 60].strip()
                            tname = ""
                            if rem.startswith('"'):
                                q2 = rem.find('"', 1)
                                if q2 > 0:
                                    tname = rem[1:q2]
                            elif rem.startswith('\\"'):
                                q2 = rem.find('\\"', 2)
                                if q2 > 0:
                                    tname = rem[2:q2]
                            if tname:
                                stardew_tools.append(tname)
                                all_tool_calls.append({"step": idx, "tool": tname})
        except Exception:
            pass

        # Also inspect transcript if available for cleaner text
        transcript_path = (
            pathlib.Path.home()
            / f".gemini/antigravity-cli/brain/{cid}/.system_generated/logs/transcript.jsonl"
        )
        created_time = datetime.datetime.fromtimestamp(
            db_path.stat().st_mtime, tz=datetime.timezone.utc
        )
        if transcript_path.is_file():
            try:
                with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        if (
                            obj.get("type") == "USER_INPUT"
                            and not user_prompt
                            and obj.get("content")
                        ):
                            content_str = obj["content"]
                            if "<USER_REQUEST>" in content_str:
                                s = content_str.find("<USER_REQUEST>") + len("<USER_REQUEST>")
                                e = content_str.find("</USER_REQUEST>", s)
                                if e > s:
                                    user_prompt_full = content_str[s:e].strip()
                                    user_prompt = user_prompt_full.split("\n")[0].strip()[:100]
                            else:
                                user_prompt_full = content_str.strip()
                                user_prompt = user_prompt_full.split("\n")[0].strip()[:100]
                        if obj.get("created_at"):
                            try:
                                created_time = datetime.datetime.fromisoformat(
                                    obj["created_at"].replace("Z", "+00:00")
                                )
                            except Exception:
                                pass
                        if obj.get("type") == "PLANNER_RESPONSE" and obj.get("tool_calls"):
                            for tc in obj["tool_calls"]:
                                if tc.get("name") == "call_mcp_tool":
                                    args = tc.get("args") or {}
                                    tn = args.get("ToolName", "")
                                    if tn and tn not in stardew_tools:
                                        stardew_tools.append(tn)
            except Exception:
                pass

        total_input = sum(t["input_tokens"] for t in turns)
        total_output = sum(t["output_tokens"] for t in turns)
        total_cache = sum(t["cache_read_tokens"] for t in turns)
        total_think = sum(t["thinking_tokens"] for t in turns)
        grand_total = total_input + total_output

        # Tool summary count
        tool_counts: dict[str, int] = {}
        for t in stardew_tools:
            tool_counts[t] = tool_counts.get(t, 0) + 1

        return {
            "cid": cid,
            "mtime": db_path.stat().st_mtime,
            "created_time": created_time.isoformat(),
            "display_time": created_time.strftime("%Y-%m-%d %H:%M:%S"),
            "prompt_short": user_prompt or "(No user prompt detected)",
            "prompt_full": user_prompt_full,
            "turn_count": len(turns),
            "turns": turns,
            "total_input": total_input,
            "total_output": total_output,
            "total_cache": total_cache,
            "total_think": total_think,
            "grand_total": grand_total,
            "stardew_tools": stardew_tools,
            "tool_counts": tool_counts,
            "is_stardew": len(stardew_tools) > 0 or "星露谷" in user_prompt or "种" in user_prompt,
        }
    except Exception as ex:
        sys.stderr.write(f"Error reading {db_path}: {ex}\n")
        return None
    finally:
        if con:
            con.close()


def collect_all_sessions(
    conversations_dir: pathlib.Path, include_all: bool = False
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    if not conversations_dir.is_dir():
        return sessions

    for db_file in conversations_dir.glob("*.db"):
        info = read_conversation_db(db_file)
        if not info:
            continue
        if include_all or info["is_stardew"] or info["turn_count"] > 0:
            sessions.append(info)

    # Sort descending by last modified time
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def classify_command_record(record: dict[str, Any], source_path: pathlib.Path | None = None) -> str:
    """Classifies record as 'production' (实机) or 'test' (测试/开发)."""
    src = str(record.get("source", "")).lower()
    if src in ("test", "testing", "dev", "mock"):
        return "test"
    if src == "production":
        return "production"

    if source_path:
        for part in source_path.parts:
            if part.startswith(".test-runs") or part == "test-runs":
                return "test"

    req_id = str(record.get("requestId", ""))
    if req_id in ("req-to-cancel", "req-resumed", "req-new-1", "req-cancel-persist", "req-cancel-1"):
        return "test"
    if req_id.startswith(("test-", "mock-", "req-test-", "r_pre", "r_post")):
        return "test"

    save_id = str(record.get("saveId", ""))
    if save_id in ("Save1", "SaveNew", "test-save"):
        return "test"

    cid = str(record.get("conversationId", ""))
    if cid.startswith(("test-", "mock-", "conv-existing-", "fake-")):
        return "test"

    return "production"


def load_chat_commands(run_dir: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """Loads command execution records from chat_commands.jsonl."""
    files_to_check: list[pathlib.Path] = []
    if run_dir:
        files_to_check.append(run_dir / "chat_commands.jsonl")

    else:
        repo_root = pathlib.Path(__file__).resolve().parent.parent
        files_to_check.append(repo_root / "chat_commands.jsonl")
        files_to_check.extend((repo_root / ".test-runs").glob("*/chat_commands.jsonl"))
        files_to_check.append(pathlib.Path.home() / ".gemini" / "antigravity-cli" / "chat_commands.jsonl")

    commands: list[dict[str, Any]] = []
    seen_reqs: set[str] = set()

    for path in files_to_check:
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        req_id = record.get("requestId")
                        if req_id and req_id not in seen_reqs:
                            seen_reqs.add(req_id)
                            category = classify_command_record(record, path)
                            record["category"] = category
                            record["isTest"] = (category == "test")
                            commands.append(record)
                    except Exception:
                        pass
        except Exception:
            pass

    commands.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return commands


def format_cli_table(
    sessions: list[dict[str, Any]],
    commands: list[dict[str, Any]] | None = None,
    specific_cid: str | None = None,
    include_tests: bool = False,
) -> str:
    lines: list[str] = []

    # 1. Per-Command Table
    if commands:
        all_relevant = [
            c for c in commands
            if not specific_cid or (c.get("conversationId") and c["conversationId"].startswith(specific_cid))
        ]
        test_count = sum(1 for c in all_relevant if c.get("isTest"))
        relevant_cmds = all_relevant if include_tests else [c for c in all_relevant if not c.get("isTest")]

        if relevant_cmds:
            lines.append("=" * 125)
            lines.append("【用户自然语言指令使用明细 (Per-Command Usage)】")
            lines.append("-" * 125)
            lines.append(
                f"{'类别':<6} | {'指令 ID':<10} | {'执行时间':<19} | {'状态':<9} | {'对应会话':<10} | {'耗时':<6} | {'输入 Tokens':<11} | {'输出 Tokens':<11} | {'思考 Tokens':<11} | {'缓存读取':<11} | {'指令内容'}"
            )
            lines.append("-" * 125)
            for c in relevant_cmds:
                cat_tag = "[测试]" if c.get("isTest") else "[实机]"
                req = c.get("requestId", "")[:8]
                ts = c.get("timestamp", "")[:19].replace("T", " ")
                st = c.get("status", "unknown")
                cid = (c.get("conversationId") or "")[:8] or "-"
                dur = f"{c.get('durationSeconds', 0.0):.1f}s"
                u = c.get("usage") or {}
                prompt_text = (c.get("prompt") or "").replace("\n", " ")
                p = prompt_text[:28] + ("..." if len(prompt_text) > 28 else "")
                if u:
                    inp_s = fmt_token(u.get("input_tokens"))
                    out_s = fmt_token(u.get("output_tokens"))
                    th_s = fmt_token(u.get("thinking_tokens"))
                    ca_s = fmt_token(u.get("cache_read_tokens"))
                    gens_cnt = u.get("generations_count", 0)
                    gen_range = f"代数: {gens_cnt} (gen {c.get('startGenIdx', -1)+1}..{c.get('endGenIdx', -1)}) | 来源: {u.get('source', '-')}"
                else:
                    missing = c.get("missingReason") or "未提供"
                    inp_s = "-"
                    out_s = "-"
                    th_s = "-"
                    ca_s = "-"
                    gen_range = f"[{missing}]"


                lines.append(
                    f"{cat_tag:<6} | {req:<10} | {ts:<19} | {st:<9} | {cid:<10} | {dur:<6} | {inp_s:<11} | {out_s:<11} | {th_s:<11} | {ca_s:<11} | {p}"
                )
                lines.append(f"  └─ 边界与来源: {gen_range} | 存档: {c.get('saveId', '-')}")
            lines.append("-" * 125)
            if test_count > 0 and not include_tests:
                lines.append(f"  * 已自动隐藏 {test_count} 条开发/测试指令记录。使用 --include-tests 查看全部记录。")


    # 2. Session Table
    lines.append("=" * 115)
    lines.append("【会话底层汇总明细 (Session Database View)】")
    lines.append("-" * 115)
    lines.append(
        f"{'会话 ID':<10} | {'执行时间':<19} | {'轮数':<4} | {'输入 Tokens':<11} | {'输出 Tokens':<11} | {'思考 Tokens':<11} | {'缓存读取':<11} | {'星露谷伙伴 MCP 调用'}"
    )
    lines.append("-" * 115)

    grand_inp = 0
    grand_out = 0
    grand_think = 0
    grand_cache = 0

    cid_to_cmds: dict[str, list[dict[str, Any]]] = {}
    if commands:
        for c in commands:
            c_cid = c.get("conversationId")
            if c_cid:
                cid_to_cmds.setdefault(c_cid, []).append(c)

    for s in sessions:
        if specific_cid and not s["cid"].startswith(specific_cid):
            continue
        cid_full = s["cid"]
        cid_short = cid_full[:8]
        time_str = s["display_time"]
        turns = s["turn_count"]
        inp = f"{s['total_input']:,}"
        out = f"{s['total_output']:,}"
        th = f"{s['total_think']:,}"
        cache = f"{s['total_cache']:,}"

        tool_desc = ", ".join(f"{k}:{v}" for k, v in sorted(s["tool_counts"].items()))
        if not tool_desc:
            tool_desc = "-"
        if len(tool_desc) > 35:
            tool_desc = tool_desc[:32] + "..."

        lines.append(
            f"{cid_short:<10} | {time_str:<19} | {turns:<4} | {inp:<11} | {out:<11} | {th:<11} | {cache:<11} | {tool_desc}"
        )

        matched_cmds = cid_to_cmds.get(cid_full, [])
        if matched_cmds:
            lines.append(f"  └─ 关联指令: 已绑定 {len(matched_cmds)} 条用户指令 (最新: {matched_cmds[0].get('prompt', '')[:40]})")
        else:
            lines.append(f"  └─ 任务提示: [历史未绑定指令 / 全会话视图] {s['prompt_short']}")

        if len(sessions) == 1 or specific_cid:
            lines.append("  ┌─ 逐轮明细 (Per-Turn Breakdown):")
            lines.append("  │  轮次   | 输入 Tokens | 输出 Tokens | 思考 Tokens | 缓存命中    | 该轮合计")
            lines.append("  │  " + "-" * 65)
            for t in s["turns"]:
                lines.append(
                    f"  │  Turn {t['turn']:<2} | {t['input_tokens']:<11,} | {t['output_tokens']:<11,} | {t['thinking_tokens']:<11,} | {t['cache_read_tokens']:<11,} | {t['total_tokens']:<11,}"
                )
            lines.append("  └─ " + "-" * 65)

        grand_inp += s["total_input"]
        grand_out += s["total_output"]
        grand_think += s["total_think"]
        grand_cache += s["total_cache"]

    lines.append("=" * 115)
    total_tokens = grand_inp + grand_out
    lines.append(
        f"【汇总统计】会话数: {len(sessions)} | 总输入: {grand_inp:,} | 总输出: {grand_out:,} | 总思考: {grand_think:,} | 缓存节约: {grand_cache:,} | 实际产生: {total_tokens:,}"
    )
    lines.append("=" * 115)
    return "\n".join(lines)


def request_usage_html(usage: dict[str, Any]) -> str:
    requests = usage.get("requests") or []
    if not requests:
        return "<details><summary>每次模型请求</summary>未知：没有逐请求用量记录。</details>"
    rows = []
    keys = ("index", "inputOther", "inputCacheRead", "inputCacheCreation", "inputContext", "output")
    for request in requests:
        cells = [html.escape(str(request[k])) if isinstance(request.get(k), int) else "未知" for k in keys]
        rows.append("<tr>" + "".join("<td>" + cell + "</td>" for cell in cells) + "</tr>")
    return ('<details><summary>每次模型请求（' + str(len(requests)) + '）</summary>'
            '<table><thead><tr><th>序号</th><th>非缓存输入</th><th>缓存读取</th>'
            '<th>缓存写入</th><th>输入上下文</th><th>输出</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></details>')


def installed_usage_directory(repo_root: pathlib.Path) -> pathlib.Path | None:
    metadata = repo_root / "config" / "installed-candidate.json"
    if not metadata.exists():
        return None
    installed = json.loads(metadata.read_text(encoding="utf-8-sig"))
    directory = pathlib.Path(installed["modDirectory"]).resolve()
    expected = (pathlib.Path(installed["gameDirectory"]) / "Mods" / "StardewAI.Companion.Mod").resolve()
    if directory != expected or ".test-runs" in [p.lower() for p in directory.parts]:
        raise ValueError("Candidate usage directory must be its normal game instance")
    return directory


def generate_html_report(
    sessions: list[dict[str, Any]],
    output_file: pathlib.Path,
    commands: list[dict[str, Any]] | None = None,
    include_tests: bool = False,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    total_sessions = len(sessions)
    total_turns = sum(s["turn_count"] for s in sessions)
    total_input = sum(s["total_input"] for s in sessions)
    total_output = sum(s["total_output"] for s in sessions)
    total_cache = sum(s["total_cache"] for s in sessions)
    total_think = sum(s["total_think"] for s in sessions)
    grand_total = total_input + total_output
    total_mcp = sum(len(s["stardew_tools"]) for s in sessions)

    # Per-Command rows
    cmd_rows: list[str] = []
    if commands:
        all_cmds = commands if include_tests else [c for c in commands if not c.get("isTest")]
        for c in all_cmds:
            req = c.get("requestId", "")[:8]
            ts = c.get("timestamp", "")[:19].replace("T", " ")
            st = c.get("status", "unknown")
            cid = (c.get("conversationId") or "")[:8] or "-"
            dur = f"{c.get('durationSeconds', 0.0):.1f}s"
            u = c.get("usage") or {}
            prompt_text = (c.get("prompt") or "").replace("\n", " ")
            p_esc = html.escape(prompt_text[:40] + ("..." if len(prompt_text) > 40 else ""))
            badge_class = (
                "badge-status-success"
                if st == "success"
                else ("badge-status-cancelled" if st == "cancelled" else "badge-status-error")
            )
            cat_badge = (
                '<span class="badge badge-status-success">实机</span>'
                if not c.get("isTest")
                else '<span class="badge badge-tool">测试/开发</span>'
            )
            if u:
                inp_s = html.escape(fmt_token(u.get("input_tokens")))
                out_s = html.escape(fmt_token(u.get("output_tokens")))
                th_s = html.escape(fmt_token(u.get("thinking_tokens")))
                ca_s = html.escape(fmt_token(u.get("cache_read_tokens")))
                src_s = html.escape(f"{u.get('source', '-')}")
            else:
                missing = html.escape(c.get("missingReason") or "未提供")
                inp_s = out_s = th_s = ca_s = "-"
                src_s = f"<span class='text-muted'>[{missing}]</span>"

            cmd_rows.append(f"""
            <tr>
                <td>{cat_badge}</td>
                <td><code>{html.escape(req)}</code></td>
                <td>{html.escape(ts)}</td>
                <td><span class="badge {badge_class}">{html.escape(st)}</span></td>
                <td><code>{html.escape(cid)}</code></td>
                <td>{dur}</td>
                <td>{inp_s}</td>
                <td>{out_s}</td>
                <td>{th_s}</td>
                <td class="text-cache">{ca_s}</td>
                <td>{src_s}</td>
                <td>{p_esc}{request_usage_html(u)}</td>
            </tr>
            """)

    commands_section_html = ""
    if cmd_rows:
        commands_section_html = f"""
    <div class="section-title">
        <h2>用户自然语言指令使用明细 (Per-Command Usage)</h2>
        <div class="meta">数据源: chat_commands.jsonl (每条游戏内指令起止代数边界与精确用量汇总)</div>
    </div>
    <div class="table-container" style="margin-bottom: 32px;">
        <table>
            <thead>
                <tr>
                    <th>类别</th>
                    <th>指令 ID</th>
                    <th>执行时间</th>
                    <th>状态</th>
                    <th>对应会话</th>
                    <th>耗时</th>
                    <th>输入 Tokens</th>
                    <th>输出 Tokens</th>
                    <th>思考 Tokens</th>
                    <th>缓存节约</th>
                    <th>代数 / 来源</th>
                    <th>指令内容</th>
                </tr>
            </thead>
            <tbody>
                {''.join(cmd_rows)}
            </tbody>
        </table>
    </div>
        """


    rows_html: list[str] = []
    for idx, s in enumerate(sessions):
        cid = s["cid"]
        cid_short = cid[:8]
        display_time = s["display_time"]
        turns = s["turn_count"]
        prompt_esc = html.escape(s["prompt_short"])
        prompt_full_esc = html.escape(s["prompt_full"])

        tools_html = ""
        if s["tool_counts"]:
            badges = [
                f'<span class="badge badge-tool">{html.escape(k)} <span class="count">{v}</span></span>'
                for k, v in sorted(s["tool_counts"].items(), key=lambda x: -x[1])
            ]
            tools_html = "".join(badges)
        else:
            tools_html = '<span class="text-muted">无 MCP 交互</span>'

        # Per-turn table rows
        turn_rows = []
        for t in s["turns"]:
            t_num = t["turn"]
            t_inp = f"{t['input_tokens']:,}"
            t_out = f"{t['output_tokens']:,}"
            t_th = f"{t['thinking_tokens']:,}"
            t_cache = f"{t['cache_read_tokens']:,}"
            t_tot = f"{t['total_tokens']:,}"
            turn_rows.append(
                f"<tr><td>Turn {t_num}</td><td>{t_inp}</td><td>{t_out}</td><td>{t_th}</td><td>{t_cache}</td><td>{t_tot}</td></tr>"
            )
        turn_table_html = (
            f"""
            <table class="detail-table">
                <thead><tr><th>轮次</th><th>输入 Tokens</th><th>输出 Tokens</th><th>思考 Tokens</th><th>缓存命中</th><th>该轮合计</th></tr></thead>
                <tbody>{''.join(turn_rows)}</tbody>
            </table>
        """
            if turn_rows
            else "<p class='text-muted'>无单轮详细明细</p>"
        )

        row = f"""
        <tr class="main-row" onclick="toggleDetails('{cid}')">
            <td><code>{cid_short}</code></td>
            <td>{display_time}</td>
            <td><strong>{prompt_esc}</strong></td>
            <td><span class="badge badge-turns">{turns} 轮</span></td>
            <td>{s['total_input']:,}</td>
            <td>{s['total_output']:,}</td>
            <td>{s['total_think']:,}</td>
            <td class="text-cache">{s['total_cache']:,}</td>
            <td class="text-total"><strong>{s['grand_total']:,}</strong></td>
            <td>{tools_html}</td>
        </tr>
        <tr id="details-{cid}" class="details-row" style="display:none;">
            <td colspan="10">
                <div class="details-card">
                    <div class="details-header">
                        <h4>会话完整信息 (ID: <code>{cid}</code>)</h4>
                    </div>
                    <div class="prompt-box">
                        <strong>完整任务指令：</strong>
                        <pre>{prompt_full_esc}</pre>
                    </div>
                    <h5>按轮次 Token 消耗明细</h5>
                    {turn_table_html}
                </div>
            </td>
        </tr>
        """
        rows_html.append(row)

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>星露谷伙伴 Token 使用量实时看板</title>
    <style>
        :root {{
            --bg: #0f172a;
            --card-bg: #1e293b;
            --card-border: #334155;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --accent-hover: #0ea5e9;
            --success: #4ade80;
            --warning: #facc15;
            --danger: #f87171;
            --purple: #c084fc;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            padding: 24px;
            line-height: 1.5;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            border-bottom: 1px solid var(--card-border);
            padding-bottom: 16px;
        }}
        .header h1 {{
            font-size: 24px;
            font-weight: 700;
            color: var(--accent);
        }}
        .header .meta, .section-title .meta {{
            color: var(--text-muted);
            font-size: 14px;
        }}
        .section-title {{
            margin: 28px 0 14px 0;
        }}
        .section-title h2 {{
            font-size: 18px;
            font-weight: 700;
            color: var(--accent);
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 28px;
        }}
        .stat-card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 10px;
            padding: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .stat-card .label {{
            font-size: 13px;
            color: var(--text-muted);
            margin-bottom: 6px;
        }}
        .stat-card .value {{
            font-size: 24px;
            font-weight: 700;
            color: var(--text);
        }}
        .stat-card.accent .value {{ color: var(--accent); }}
        .stat-card.success .value {{ color: var(--success); }}
        .stat-card.purple .value {{ color: var(--purple); }}
        .stat-card.warning .value {{ color: var(--warning); }}

        .table-container {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 10px;
            overflow-x: auto;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 14px;
        }}
        th, td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--card-border);
        }}
        th {{
            background: #111827;
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 12px;
            letter-spacing: 0.05em;
        }}
        tr.main-row:hover {{
            background: #283548;
            cursor: pointer;
        }}
        code {{
            background: #0f172a;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            color: var(--accent);
        }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 9999px;
            font-size: 12px;
            font-weight: 500;
            margin-right: 4px;
            margin-bottom: 2px;
        }}
        .badge-turns {{ background: #0369a1; color: #e0f2fe; }}
        .badge-tool {{
            background: #334155;
            color: #cbd5e1;
            border: 1px solid #475569;
        }}
        .badge-tool .count {{
            color: var(--accent);
            font-weight: 700;
        }}
        .badge-status-success {{ background: #065f46; color: #a7f3d0; }}
        .badge-status-cancelled {{ background: #854d0e; color: #fef08a; }}
        .badge-status-error {{ background: #991b1b; color: #fecaca; }}
        .text-muted {{ color: var(--text-muted); }}
        .text-cache {{ color: var(--success); font-weight: 600; }}
        .text-total {{ color: var(--accent); font-weight: 700; }}

        .details-row td {{
            background: #151f30;
            padding: 18px;
        }}
        .details-card {{
            border-left: 3px solid var(--accent);
            padding-left: 14px;
        }}
        .prompt-box {{
            background: #0b1120;
            border: 1px solid var(--card-border);
            border-radius: 6px;
            padding: 12px;
            margin: 12px 0 16px 0;
        }}
        .prompt-box pre {{
            white-space: pre-wrap;
            word-break: break-all;
            font-size: 13px;
            color: #e2e8f0;
            margin-top: 6px;
        }}
        .detail-table {{
            margin-top: 10px;
            font-size: 13px;
        }}
        .detail-table th {{
            background: #0b1120;
        }}
        .detail-table td {{
            padding: 8px 12px;
        }}
    </style>
    <script>
        function toggleDetails(cid) {{
            var el = document.getElementById('details-' + cid);
            if (el) {{
                el.style.display = (el.style.display === 'none' || el.style.display === '') ? 'table-row' : 'none';
            }}
        }}
    </script>
</head>
<body>
    <div class="header">
        <div>
            <h1>星露谷伙伴 Token 使用量实时看板</h1>
            <div class="meta">数据源: 当前实例 chat_commands.jsonl；旧会话数据库仅在未安装候选时显示 | 报告生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        </div>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="label">追踪会话数</div>
            <div class="value">{total_sessions}</div>
        </div>
        <div class="stat-card accent">
            <div class="label">模型总轮次</div>
            <div class="value">{total_turns}</div>
        </div>
        <div class="stat-card">
            <div class="label">总输入 Tokens</div>
            <div class="value">{total_input:,}</div>
        </div>
        <div class="stat-card warning">
            <div class="label">总输出 Tokens</div>
            <div class="value">{total_output:,}</div>
        </div>
        <div class="stat-card purple">
            <div class="label">总思考 Tokens</div>
            <div class="value">{total_think:,}</div>
        </div>
        <div class="stat-card success">
            <div class="label">缓存命中节约</div>
            <div class="value">{total_cache:,}</div>
        </div>
        <div class="stat-card accent">
            <div class="label">总 MCP 工具调用</div>
            <div class="value">{total_mcp:,}</div>
        </div>
        <div class="stat-card">
            <div class="label">实际产生 Tokens 合计</div>
            <div class="value">{grand_total:,}</div>
        </div>
    </div>

    {commands_section_html}

    <div class="section-title">
        <h2>会话底层汇总明细 (Session Database View)</h2>
        <div class="meta">按会话完整聚合记录</div>
    </div>
    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th>会话 ID</th>
                    <th>执行时间</th>
                    <th>任务目标</th>
                    <th>轮数</th>
                    <th>输入 Tokens</th>
                    <th>输出 Tokens</th>
                    <th>思考 Tokens</th>
                    <th>缓存节约 Tokens</th>
                    <th>实际总 Tokens</th>
                    <th>星露谷伙伴 MCP 调用</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows_html)}
            </tbody>
        </table>
    </div>
</body>
</html>
"""
    if not sessions:
        start = html_content.index('    <div class="stats-grid">')
        end = html_content.index('    <div class="section-title">', start) if commands_section_html else html_content.index('</body>', start)
        html_content = html_content[:start] + html_content[end:]
        start = html_content.find('    <div class="section-title">\n        <h2>会话底层')
        if start >= 0:
            html_content = html_content[:start] + '</body></html>'
    output_file.write_text(html_content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="View Antigravity Token Usage")
    parser.add_argument(
        "--cid",
        type=str,
        default=None,
        help="View token usage for a specific conversation ID",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include all conversations (not just Stardew Companion)",
    )
    parser.add_argument(
        "--html",
        type=pathlib.Path,
        default=None,
        help="Path to output HTML report dashboard",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data",
    )
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="Include automated testing / development command records",
    )
    args = parser.parse_args()

    conv_dir = pathlib.Path.home() / ".gemini/antigravity-cli/conversations"
    run_dir = installed_usage_directory(pathlib.Path(__file__).resolve().parent.parent)
    sessions = [] if run_dir else collect_all_sessions(conv_dir, include_all=args.all)
    commands = load_chat_commands(run_dir)

    if args.cid:
        sessions = [s for s in sessions if s["cid"] == args.cid or s["cid"].startswith(args.cid)]
        if not sessions:
            print(f"No conversation found matching ID: {args.cid}")
            sys.exit(1)

    if args.json:
        out_cmds = commands if args.include_tests else [c for c in commands if not c.get("isTest")]
        print(json.dumps({"sessions": sessions, "commands": out_cmds}, ensure_ascii=False, indent=2))
        return

    if args.html:
        generate_html_report(sessions, args.html, commands=commands, include_tests=args.include_tests)
        print(f"HTML dashboard generated at: {args.html.resolve()}")

    # Print CLI table
    table = format_cli_table(sessions, commands=commands, specific_cid=args.cid, include_tests=args.include_tests)
    print(table)



if __name__ == "__main__":
    main()
