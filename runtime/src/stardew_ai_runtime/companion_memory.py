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
CATEGORY_VALUES = frozenset({"fact", "speculation", "constraint"})

# Default layering by entry kind: player agreements/preferences are user
# constraints; system-recorded shared events are facts.
_KIND_DEFAULT_CATEGORY = {"event": "fact", "agreement": "constraint", "preference": "constraint"}

_AGREEMENT_LIMIT = 15
_PREFERENCE_LIMIT = 15
_EVENT_LIMIT = 20
_RENDER_EVENT_COUNT = 8
_RENDER_CHAR_BUDGET = 1200

_CATEGORY_LABELS = {"fact": "事实", "speculation": "推测", "constraint": "用户约束"}


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
                            "instructionRevision": int(v.get("instructionRevision", v.get("memoryRevision", 0))),
                            "entries": [self._migrate_entry(dict(e)) for e in v.get("entries", []) if isinstance(e, dict)] if isinstance(v.get("entries"), list) else [],
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
                record = self._data.setdefault(save_id, {"memoryRevision": 0, "instructionRevision": 0, "entries": []})
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

    @staticmethod
    def _migrate_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """Fill layering fields on legacy entries (load-time only)."""
        if not isinstance(entry.get("category"), str) or entry["category"] not in CATEGORY_VALUES:
            entry["category"] = _KIND_DEFAULT_CATEGORY.get(str(entry.get("kind")), "fact")
        return entry

    @staticmethod
    def _is_current(entry: dict[str, Any]) -> bool:
        """Revoked or superseded entries never render into context."""
        return not entry.get("revoked") and not entry.get("superseded_by")

    # ---------------------------------------------------------------- public API
    def instruction_revision(self, save_id: str) -> int:
        """Revision of agreements/preferences only, for provider session rotation.

        Routine completion events still change the wire memoryRevision, but do
        not invalidate the work session. Legacy records start from their known
        memory revision so subsequent user corrections remain distinguishable.
        """
        self._load()
        return int(self._data.get(save_id, {}).get("instructionRevision", 0))

    def list(self, save_id: str) -> dict[str, Any]:
        """Return {memoryRevision, entries} for save_id (wire shape, §1.5).

        The internal dedup field commandId and the layering fields (category,
        validity, revoke/supersede markers) stay on disk and in
        render_for_context(), but are stripped here: list() output goes onto
        the wire whose §1.5 contract pins the entry field set.
        """
        self._load()
        record = self._data.get(save_id, {"memoryRevision": 0, "entries": []})
        internal = {"commandId", "category", "validFrom", "validTo",
                    "revoked", "superseded_by", "revisionReason"}
        entries: list[Any] = []
        for e in record.get("entries", []):
            if isinstance(e, dict) and internal.intersection(e):
                e = {k: v for k, v in e.items() if k not in internal}
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
        category: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Add a new memory entry.

        For kind=event, only source=system is accepted and there is no
        expectedRevision check (events are written by the system internally).
        For kind=preference/agreement, expectedRevision must match.
        ``category`` defaults from the entry kind (event→fact,
        agreement/preference→constraint); a scope-limited observation may be
        recorded as ``speculation`` so a later wider observation can supersede
        it. Returns (status, result_dict).
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
            resolved_category = category or _KIND_DEFAULT_CATEGORY.get(kind, "fact")
            if resolved_category not in CATEGORY_VALUES:
                status_holder[0] = "rejected"
                result["reason"] = "INVALID_CATEGORY"
                result["memoryRevision"] = current_rev
                return
            new_entry: dict[str, Any] = {
                "id": eid,
                "kind": kind,
                "text": str(text)[:200],
                "source": source,
                "gameDate": game_date,
                "category": resolved_category,
                "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            if valid_from:
                new_entry["validFrom"] = str(valid_from)
            if valid_to:
                new_entry["validTo"] = str(valid_to)
            if command_id:
                new_entry["commandId"] = command_id
            entries.append(new_entry)
            record["memoryRevision"] = current_rev + 1
            if kind != "event":
                record["instructionRevision"] = int(record.get("instructionRevision", current_rev)) + 1
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
                    record["instructionRevision"] = int(record.get("instructionRevision", current_rev)) + 1
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
                    if e.get("kind") != "event":
                        record["instructionRevision"] = int(record.get("instructionRevision", current_rev)) + 1
                    result["memoryRevision"] = record["memoryRevision"]
                    return
            status_holder[0] = "rejected"
            result["reason"] = "NOT_FOUND"
            result["memoryRevision"] = current_rev

        self._mutate(save_id, apply)
        return status_holder[0], result

    def revoke(
        self,
        save_id: str,
        entry_id: str,
        reason: str,
        expected_revision: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Retract an entry: it stops rendering, with the reason preserved."""
        status_holder: list[str] = ["confirmed"]
        result: dict[str, Any] = {}

        def apply(record: dict[str, Any]) -> None:
            entries: list[dict[str, Any]] = record.get("entries", [])
            current_rev = int(record.get("memoryRevision", 0))
            if expected_revision is not None and current_rev != expected_revision:
                status_holder[0] = "rejected"
                result["reason"] = "STALE_REVISION"
                result["memoryRevision"] = current_rev
                return
            for e in entries:
                if isinstance(e, dict) and e.get("id") == entry_id:
                    e["revoked"] = True
                    e["revisionReason"] = str(reason or "")[:200]
                    record["memoryRevision"] = current_rev + 1
                    if e.get("kind") != "event":
                        record["instructionRevision"] = int(record.get("instructionRevision", current_rev)) + 1
                    result["memoryRevision"] = record["memoryRevision"]
                    result["entry"] = dict(e)
                    return
            status_holder[0] = "rejected"
            result["reason"] = "NOT_FOUND"
            result["memoryRevision"] = current_rev

        self._mutate(save_id, apply)
        return status_holder[0], result

    def supersede(
        self,
        save_id: str,
        old_id: str,
        text: str,
        *,
        reason: str = "",
        source: str = "companion",
        game_date: str | None = None,
        expected_revision: int | None = None,
        category: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Replace an entry with a corrected positive one.

        The old entry is marked ``superseded_by`` the new id (it stops
        rendering); the new entry carries the current belief. Used to turn a
        scope-limited speculation (e.g. "no water source inside the farm
        scan") into an updated fact without history loss.
        """
        status_holder: list[str] = ["confirmed"]
        result: dict[str, Any] = {}

        def apply(record: dict[str, Any]) -> None:
            entries: list[dict[str, Any]] = record.get("entries", [])
            current_rev = int(record.get("memoryRevision", 0))
            if expected_revision is not None and current_rev != expected_revision:
                status_holder[0] = "rejected"
                result["reason"] = "STALE_REVISION"
                result["memoryRevision"] = current_rev
                return
            old = next((e for e in entries if isinstance(e, dict) and e.get("id") == old_id), None)
            if old is None:
                status_holder[0] = "rejected"
                result["reason"] = "NOT_FOUND"
                result["memoryRevision"] = current_rev
                return
            new_id = self._entry_id()
            new_entry: dict[str, Any] = {
                "id": new_id,
                "kind": old.get("kind") if old.get("kind") != "event" else "event",
                "text": str(text or "")[:200],
                "source": source,
                "gameDate": game_date or old.get("gameDate") or "unknown",
                "category": category or old.get("category") or "fact",
                "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            if valid_from:
                new_entry["validFrom"] = str(valid_from)
            if valid_to:
                new_entry["validTo"] = str(valid_to)
            old["superseded_by"] = new_id
            old["revisionReason"] = str(reason or "")[:200]
            entries.append(new_entry)
            record["memoryRevision"] = current_rev + 1
            if old.get("kind") != "event":
                record["instructionRevision"] = int(record.get("instructionRevision", current_rev)) + 1
            result["memoryRevision"] = record["memoryRevision"]
            result["entry"] = dict(new_entry)

        self._mutate(save_id, apply)
        return status_holder[0], result

    def render_for_context(self, save_id: str) -> dict[str, Any]:
        """Render memory in a compact form suitable for LLM context injection.

        agreements + preferences: full, combined ≤ 1200 chars total (oldest events truncated).
        events: latest ≤ 8.
        Layered projection (``facts``/``speculations``/``constraints``): grouped
        with validity labels; revoked/superseded entries never render.
        """
        self._load()
        record = self._data.get(save_id, {"memoryRevision": 0, "entries": []})
        entries: list[dict[str, Any]] = record.get("entries", [])
        # Do not rewrite player agreements or historical events. Only retired
        # engine-generated allowance rules are excluded from current context.
        entries = [entry for entry in entries if not (
            isinstance(entry, dict) and entry.get("source") == "system"
            and entry.get("kind") in {"preference", "agreement"}
            and any(marker in str(entry.get("text", "")) for marker in
                    ("每日购买额度", "每日采购额度", "purchaseBudget", "dailyPurchaseBudget")))]
        entries = [self._migrate_entry(dict(entry)) for entry in entries]
        # Retracted or replaced knowledge never reaches the model context.
        current = [e for e in entries if self._is_current(e)]

        agreements = [e for e in current if isinstance(e, dict) and e.get("kind") == "agreement"]
        preferences = [e for e in current if isinstance(e, dict) and e.get("kind") == "preference"]
        events = [e for e in current if isinstance(e, dict) and e.get("kind") == "event"]

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

        def _layered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rendered: list[dict[str, Any]] = []
            for e in rows:
                label = _CATEGORY_LABELS.get(str(e.get("category")), "事实")
                row: dict[str, Any] = {
                    "id": e.get("id"),
                    "label": label,
                    "text": e.get("text"),
                    "gameDate": e.get("gameDate"),
                }
                if e.get("validFrom") or e.get("validTo"):
                    row["validity"] = {"from": e.get("validFrom"), "to": e.get("validTo")}
                rendered.append(row)
            return rendered

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
            "facts": _layered([e for e in current if e.get("category") == "fact"]),
            "speculations": _layered([e for e in current if e.get("category") == "speculation"]),
            "constraints": _layered([e for e in current if e.get("category") == "constraint"]),
        }
