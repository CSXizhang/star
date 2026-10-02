"""Real-date, read-only usage display from this installation's turn receipts."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _money(value: Any) -> float | None:
    return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


class UsageDisplay:
    """Incrementally read our own journal; never consult unrelated model sessions."""

    def __init__(self, journal: Path, prices: Path | None = None, responses: Path | None = None):
        self.journal = journal
        self.prices = prices
        self._offset = 0
        self._identity: tuple[int, int] | None = None
        self._records: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._responses = UsageDisplay(responses) if responses else None

    def _read(self) -> bool:
        try:
            stat = self.journal.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity != self._identity or stat.st_size < self._offset:
                self._records.clear()
                self._offset = 0
                self._identity = identity
            with self.journal.open("rb") as stream:
                stream.seek(self._offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break  # A concurrently written receipt is read next time.
                    self._offset = stream.tell()
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or not row.get("requestId"):
                            continue
                        datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    key = (str(row.get("provider", "")), str(row.get("saveId", "")),
                           str(row.get("responseId") or row["requestId"]))
                    previous = self._records.get(key)
                    if previous is None or not previous.get("usage"):
                        self._records[key] = row
                    elif isinstance(previous.get("usage"), dict) and isinstance(row.get("usage"), dict):
                        # A later terminal can complete an interrupted receipt.
                        # Merge fields into one turn, never sum duplicate turns.
                        previous["usage"] = {**previous["usage"], **row["usage"]}
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _merged_rows(self, now: datetime | None = None) -> list[dict[str, Any]]:
        rows = {(r.get("provider"), r.get("saveId", ""), r["requestId"]): dict(r) for r in self._records.values()}
        if self._responses is None:
            return list(rows.values())
        self._responses._read()
        turns: dict[tuple, dict[str, Any]] = {}
        responses: dict[tuple, list[dict[str, Any]]] = {}
        for response in self._responses._records.values():
            key = (response.get("provider"), response.get("saveId", ""), response["requestId"])
            responses.setdefault(key, []).append(response)
            turn = turns.setdefault(key, {**response, "usage": {"input_includes_cache": True}, "responseCount": 0})
            turn["responseCount"] += 1
            for field in ("total_tokens", "input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens"):
                value = _number((response.get("usage") or {}).get(field))
                if value is not None:
                    turn["usage"][field] = turn["usage"].get(field, 0) + value
                elif field != "thinking_tokens":
                    turn["partialUsage"] = True
        dated_rows: list[dict[str, Any]] = []
        for key, turn in turns.items():
            previous = rows.get(key)
            previous_total = _number((previous.get("usage") or {}).get("total_tokens")) if previous else None
            if now is not None:
                dated = responses[key]
                dates = {datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).astimezone(now.tzinfo).date()
                         for r in [*dated, *([previous] if previous else [])]}
                if len(dates) > 1 or (previous_total is not None and previous_total > turn["usage"].get("total_tokens", 0)):
                    # A turn may straddle midnight. Keep measured responses on
                    # their own dates, even when its terminal arrives later.
                    terminal_usage = (previous.get("usage") or {}) if previous else {}
                    incomplete = (previous_total is None or terminal_usage.get("partial")
                                  or previous_total != turn["usage"].get("total_tokens"))
                    dated_rows.extend({**r, "partialUsage": bool(incomplete)} for r in dated)
                    rows.pop(key, None)
                    # Preserve terminal-only measurements without counting the
                    # same responses twice. Their exact response dates are lost,
                    # so keep the difference unallocated in the daily view.
                    remainder = {}
                    for field in ("total_tokens", "input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens"):
                        value = _number(terminal_usage.get(field))
                        if value is not None:
                            remainder[field] = max(0, value - turn["usage"].get(field, 0))
                    if previous and any(remainder.values()):
                        dated_rows.append({**previous, "usage": {**remainder, "input_includes_cache": True},
                                           "partialUsage": True, "dateUnallocated": True})
                    # Do not assign a whole-turn monetary charge to one of its
                    # days. Response costs or configured token prices still work;
                    # the lifetime view retains the provided whole-turn charge.
                    continue
            if previous_total is None or turn["usage"].get("total_tokens", 0) > previous_total:
                # A response arrives before a turn terminal, or a handoff stops
                # that terminal. Retain measured consumption as a lower bound.
                turn["partialUsage"] = True
                rows[key] = turn
        return [*rows.values(), *dated_rows]

    def today_text(self, now: datetime | None = None, save_id: str | None = None) -> str:
        return self.summary_text(now, save_id=save_id)

    def turn_response_usage(self, request_id: str, save_id: str, provider: str = "dsh") -> dict[str, Any] | None:
        if self._responses is None or not self._responses._read():
            return None
        rows = [r for r in self._responses._records.values()
                if r.get("requestId") == request_id and r.get("saveId") == save_id and r.get("provider") == provider]
        if not rows:
            return None
        usage: dict[str, Any] = {"input_includes_cache": True, "source": "durable_response_receipts",
                                 "generations_count": len(rows), "partial": True}
        for field in ("total_tokens", "input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens"):
            values = [_number((r.get("usage") or {}).get(field)) for r in rows]
            if any(v is not None for v in values):
                usage[field] = sum(v for v in values if v is not None)
        latest = max(rows, key=lambda r: r["timestamp"])
        usage["latestRequestInputContext"] = (latest.get("usage") or {}).get("latestRequestInputContext")
        return usage

    def summary_text(self, now: datetime | None = None, *, save_id: str | None = None, today: bool = True) -> str:
        now = now or datetime.now().astimezone()
        date = now.date()
        scope = " · 当前存档" if save_id is not None else ""
        prefix = f"今日用量（{date.isoformat()}{scope}）" if today else "累计用量（" + ("当前存档" if save_id is not None else "全部存档") + "）"
        if not self._read():
            return prefix + "：暂不可读取。"
        scoped = [r for r in self._merged_rows(now if today else None) if save_id is None or r.get("saveId") == save_id]
        unallocated = sum(_number((r.get("usage") or {}).get("total_tokens")) or 0 for r in scoped if r.get("dateUnallocated"))
        note = f" 另有 {unallocated:,} token 日期无法确定，已计入累计用量。" if today and unallocated else ""
        rows = [r for r in scoped if not today or (not r.get("dateUnallocated") and
                datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).astimezone(now.tzinfo).date() == date)]
        if not rows:
            return prefix + "：暂无已记录的模型调用；费用暂不可用。" + note
        try:
            prices = json.loads(self.prices.read_text(encoding="utf-8")) if self.prices else {}
        except (OSError, ValueError):
            prices = {}
        totals = {"total": 0, "input": 0, "cache": 0, "output": 0}
        known = dict.fromkeys(totals, 0)
        costs: dict[str, list[float]] = {"actual": [], "estimate": []}
        for row in rows:
            usage = row.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            if usage.get("partial"):
                row["partialUsage"] = True
            inp = _number(usage.get("input_tokens"))
            out = _number(usage.get("output_tokens"))
            cache = _number(usage.get("cache_read_tokens", usage.get("cache_read_input_tokens", usage.get("cached_input_tokens"))))
            write = _number(usage.get("cache_creation_tokens", usage.get("cache_write_input_tokens")))
            if row.get("provider") == "kimi":
                inp = inp + cache + write if None not in (inp, cache, write) else None
            elif row.get("provider") == "dsh" and not usage.get("input_includes_cache"):
                inp = inp + cache if None not in (inp, cache) else None
            total = _number(usage.get("total_tokens"))
            if total is None and inp is not None and out is not None:
                total = inp + out
            for key, value in (("total", total), ("input", inp), ("cache", cache), ("output", out)):
                if value is not None:
                    totals[key] += value
                    known[key] += 1
            actual = _money(usage.get("cost_usd"))
            estimate = _money(usage.get("estimated_api_cost_usd"))
            provider_prices = prices.get(str(row.get("provider")), {}) if isinstance(prices, dict) else {}
            rates = provider_prices.get(str(row.get("model")), {}) if isinstance(provider_prices, dict) else {}
            if estimate is None and isinstance(rates, dict) and None not in (inp, out, cache):
                rates_values = [_money(rates.get(k)) for k in ("uncachedInput", "cachedInput", "output")]
                # Cache creation has a separate rate only where explicitly measured.
                creation = write or 0
                write_rate = _money(rates.get("cacheWrite")) if creation else 0.0
                if None not in rates_values and write_rate is not None and inp >= cache + creation:
                    estimate = ((inp - cache - creation) * rates_values[0] + cache * rates_values[1]
                                + out * rates_values[2] + creation * write_rate) / 1_000_000
            if actual is not None:
                costs["actual"].append(actual)
            elif estimate is not None:
                costs["estimate"].append(estimate)
        def count(key: str) -> str:
            return f"{totals[key]:,}" + ("+未记录" if known[key] < len(rows) or any(r.get("partialUsage") for r in rows) else "") if known[key] else "未记录"
        text = f"{prefix}：{count('total')} token（输入 {count('input')}，其中缓存 {count('cache')}；输出 {count('output')}）。"
        if costs["actual"]:
            text += f" 已提供费用 ${sum(costs['actual']):.4f}"
            if len(costs["actual"]) < len(rows):
                text += "（部分调用）"
            text += "。"
        if costs["estimate"]:
            text += f" API 等价估算 ${sum(costs['estimate']):.4f}"
            if len(costs["estimate"]) < len(rows):
                text += "（部分调用）"
            text += "。"
        if not any(costs.values()):
            text += " 费用暂不可用。"
        return text + note
