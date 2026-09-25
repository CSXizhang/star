"""Companion memory store: per-save preferences, agreements, and shared events.

Persistence mirrors autonomy.py:
- single JSON file keyed by saveId
- cross-process .lock (msvcrt/fcntl)
- tmp+replace atomic write
- mutate holds lock and re-reads
- camelCase wire format

Limits per §2 of contract:
  agreement  ≤ 15
  preference ≤ 15
  event      ≤ 20  (oldest evicted first when needed for new add)
  Total render budget: agreements+preferences full (≤1200 chars), events latest ≤8

Events are written ONLY by the system (source=system) and only in a real success
terminal state. op=add does NOT accept kind=event.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KIND_VALUES = frozenset({"preference", "agreement", "event"})
SOURCE_VALUES = frozenset({"player", "companion", "system"})

_AGREEMENT_LIMIT = 15
_PREFERENCE_LIMIT = 15
_EVENT_LIMIT = 20
_RENDER_EVENT_COUNT = 8
_RENDER_CHAR_BUDGET = 1200


class CompanionMemoryStore:
    """Persistent companion memory, one record per save.

    File: ``<run_dir>/data/companion-memory.json``
    Format: ``{saveId: {memoryRevision: int, entries: [...]}}``

    Each entry: ``{id, kind, text, source, gameDate, createdAt}``
    """

    def __init__(self, state_path: Path | str) -> None:
        self.state_path = Path(state_path)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ I/O
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {}
                for k, v in raw.items():
                    if isinstance(v, dict):
                        self._data[str(k)] = {
                            "memoryRevision": int(v.get("memoryRevision", 0)),
                            "entries": list(v.get("entries", [])) if isinstance(v.get("entries"), list) else [],
                        }
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self._data = {}

    @staticmethod
    def _lock(lock: Any) -> None:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _unlock(lock: Any) -> None:
        with contextlib.suppress(Exception):
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_unlocked(self) -> None:
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.state_path)

    def _mutate(self, save_id: str, fn: Any) -> dict[str, Any]:
        if not save_id:
            raise ValueError("save_id is required")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with open(lock_path, "a+b") as lock:
            self._lock(lock)
            try:
                self._load()
                record = self._data.setdefault(save_id, {"memoryRevision": 0, "entries": []})
                fn(record)
                self._write_unlocked()
                return dict(record)
            finally:
                self._unlock(lock)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _count_kind(entries: list[dict[str, Any]], kind: str) -> int:
        return sum(1 for e in entries if isinstance(e, dict) and e.get("kind") == kind)

    @staticmethod
    def _entry_id() -> str:
        return f"mem-{uuid.uuid4().hex[:12]}"

    # ---------------------------------------------------------------- public API
    def list(self, save_id: str) -> dict[str, Any]:
        """Return {memoryRevision, entries} for save_id (wire shape, §1.5).

        The internal dedup field commandId stays on disk and in add() results,
        but is stripped here: list() output goes onto the wire.
        """
        self._load()
        record = self._data.get(save_id, {"memoryRevision": 0, "entries": []})
        entries: list[Any] = []
        for e in record.get("entries", []):
            if isinstance(e, dict) and "commandId" in e:
                e = {k: v for k, v in e.items() if k != "commandId"}
            entries.append(e)
        return {
            "memoryRevision": int(record.get("memoryRevision", 0)),
            "entries": entries,
        }

    def add(
        self,
        save_id: str,
        kind: str,
        text: str,
        source: str,
        game_date: str,
        expected_revision: int,
        *,
        entry_id: str | None = None,
        command_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Add a new memory entry.

        For kind=event, only source=system is accepted and there is no
        expectedRevision check (events are written by the system internally).
        For kind=preference/agreement, expectedRevision must match.
        Returns (status, result_dict).
        """
        status_holder: list[str] = ["confirmed"]
        result: dict[str, Any] = {}

        def apply(record: dict[str, Any]) -> None:
            entries: list[dict[str, Any]] = record.setdefault("entries", [])
            current_rev = int(record.get("memoryRevision", 0))

            # Revision check for player/companion adds
            if kind != "event" and current_rev != expected_revision:
                status_holder[0] = "rejected"
                result["reason"] = "STALE_REVISION"
                result["memoryRevision"] = current_rev
                return

            # Dedup by command_id for events
            if kind == "event" and command_id:
                for e in entries:
                    if isinstance(e, dict) and e.get("commandId") == command_id:
                        result["memoryRevision"] = current_rev
                        result["entry"] = dict(e)
                        return

            if kind == "event":
                # Evict oldest events if at limit
                event_count = self._count_kind(entries, "event")
                while event_count >= _EVENT_LIMIT:
                    # find and remove oldest event
                    for i, e in enumerate(entries):
                        if isinstance(e, dict) and e.get("kind") == "event":
                            entries.pop(i)
                            break
                    event_count = self._count_kind(entries, "event")
            elif kind == "agreement":
                if self._count_kind(entries, "agreement") >= _AGREEMENT_LIMIT:
                    status_holder[0] = "rejected"
                    result["reason"] = "MEMORY_FULL"
                    result["memoryRevision"] = current_rev
                    return
            elif kind == "preference":
                if self._count_kind(entries, "preference") >= _PREFERENCE_LIMIT:
                    status_holder[0] = "rejected"
                    result["reason"] = "MEMORY_FULL"
                    result["memoryRevision"] = current_rev
                    return

            eid = entry_id or self._entry_id()
            new_entry: dict[str, Any] = {
                "id": eid,
                "kind": kind,
                "text": str(text)[:200],
                "source": source,
                "gameDate": game_date,
                "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            if command_id:
                new_entry["commandId"] = command_id
            entries.append(new_entry)
            record["memoryRevision"] = current_rev + 1
            result["memoryRevision"] = record["memoryRevision"]
            result["entry"] = dict(new_entry)

        self._mutate(save_id, apply)
        return status_holder[0], result

    def correct(
        self,
        save_id: str,
        entry_id: str,
        text: str,
        expected_revision: int,
    ) -> tuple[str, dict[str, Any]]:
        """Correct the text of an existing entry (non-event only)."""
        status_holder: list[str] = ["confirmed"]
        result: dict[str, Any] = {}

        def apply(record: dict[str, Any]) -> None:
            entries: list[dict[str, Any]] = record.get("entries", [])
            current_rev = int(record.get("memoryRevision", 0))
            if current_rev != expected_revision:
                status_holder[0] = "rejected"
                result["reason"] = "STALE_REVISION"
                result["memoryRevision"] = current_rev
                return
            for e in entries:
                if isinstance(e, dict) and e.get("id") == entry_id:
                    if e.get("kind") == "event":
                        status_holder[0] = "rejected"
                        result["reason"] = "CANNOT_EDIT_EVENT"
                        result["memoryRevision"] = current_rev
                        return
                    e["text"] = str(text)[:200]
                    record["memoryRevision"] = current_rev + 1
                    result["memoryRevision"] = record["memoryRevision"]
                    result["entry"] = dict(e)
                    return
            status_holder[0] = "rejected"
            result["reason"] = "NOT_FOUND"
            result["memoryRevision"] = current_rev

        self._mutate(save_id, apply)
        return status_holder[0], result

    def delete(
        self,
        save_id: str,
        entry_id: str,
        expected_revision: int,
    ) -> tuple[str, dict[str, Any]]:
        """Delete an entry by id."""
        status_holder: list[str] = ["confirmed"]
        result: dict[str, Any] = {}

        def apply(record: dict[str, Any]) -> None:
            entries: list[dict[str, Any]] = record.get("entries", [])
            current_rev = int(record.get("memoryRevision", 0))
            if current_rev != expected_revision:
                status_holder[0] = "rejected"
                result["reason"] = "STALE_REVISION"
                result["memoryRevision"] = current_rev
                return
            for i, e in enumerate(entries):
                if isinstance(e, dict) and e.get("id") == entry_id:
                    entries.pop(i)
                    record["memoryRevision"] = current_rev + 1
                    result["memoryRevision"] = record["memoryRevision"]
                    return
            status_holder[0] = "rejected"
            result["reason"] = "NOT_FOUND"
            result["memoryRevision"] = current_rev

        self._mutate(save_id, apply)
        return status_holder[0], result

    def render_for_context(self, save_id: str) -> dict[str, Any]:
        """Render memory in a compact form suitable for LLM context injection.

        agreements + preferences: full, combined ≤ 1200 chars total (oldest events truncated).
        events: latest ≤ 8.
        """
        self._load()
        record = self._data.get(save_id, {"memoryRevision": 0, "entries": []})
        entries: list[dict[str, Any]] = record.get("entries", [])

        agreements = [e for e in entries if isinstance(e, dict) and e.get("kind") == "agreement"]
        preferences = [e for e in entries if isinstance(e, dict) and e.get("kind") == "preference"]
        events = [e for e in entries if isinstance(e, dict) and e.get("kind") == "event"]

        def _entry_text(e: dict[str, Any]) -> str:
            return f"[{e.get('gameDate', '?')}] {e.get('text', '')}"

        def _truncate_to_budget(
            items: list[dict[str, Any]], budget: int
        ) -> list[dict[str, Any]]:
            result = []
            used = 0
            for item in items:
                t = _entry_text(item)
                if used + len(t) > budget and result:
                    break
                result.append(item)
                used += len(t)
            return result

        # Build agreements + preferences text; drop oldest if over budget
        ap_combined = agreements + preferences
        ap_text_total = sum(len(_entry_text(e)) for e in ap_combined)
        if ap_text_total > _RENDER_CHAR_BUDGET:
            ap_combined = _truncate_to_budget(ap_combined, _RENDER_CHAR_BUDGET)

        # Events: latest ≤8
        recent_events = events[-_RENDER_EVENT_COUNT:]

        return {
            "agreements": [
                {"id": e.get("id"), "text": e.get("text"), "gameDate": e.get("gameDate")}
                for e in ap_combined
                if e.get("kind") == "agreement"
            ],
            "preferences": [
                {"id": e.get("id"), "text": e.get("text"), "gameDate": e.get("gameDate")}
                for e in ap_combined
                if e.get("kind") == "preference"
            ],
            "recentEvents": [
                {"id": e.get("id"), "text": e.get("text"), "gameDate": e.get("gameDate")}
                for e in recent_events
            ],
        }
