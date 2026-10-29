"""Companion memory store: per-save preferences, agreements, and shared events.

Persistence mirrors autonomy.py:
- single JSON file keyed by saveId
- cross-process .lock (msvcrt/fcntl)
- tmp+replace atomic write
- mutate holds lock and re-reads
- camelCase wire format

Free-text notes and methods retain source, scope and revision history.

Events are written ONLY by the system (source=system) and only in a real success
terminal state. op=add does NOT accept kind=event.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KIND_VALUES = frozenset({"preference", "agreement", "event"})
SOURCE_VALUES = frozenset({"player", "companion", "system"})
CATEGORY_VALUES = frozenset({"fact", "speculation", "constraint", "preference"})

# Agreements express constraints; preferences remain preferences.
# System-recorded shared events are historical facts.
_KIND_DEFAULT_CATEGORY = {"event": "fact", "agreement": "constraint", "preference": "preference"}

_CATEGORY_LABELS = {"fact": "事实", "speculation": "推测", "constraint": "用户约束", "preference": "偏好（非硬性约束）"}


def resolve_companion_id(run_dir: Path | str, save_id: str, *, provider: str = "",
                         connection_source: str = "builtin", session_id: str | None = None) -> str:
    """Bind notes to the actual provider session, never to a display name.

    Callers supply the selected source/provider; an unavailable selected session
    returns no identity instead of falling back to another partner.
    """
    def read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    root = Path(run_dir)
    if connection_source == "external":
        binding = read(root / "data" / "companion-connection.json")
        if not binding.get("enabled", True):
            return ""
        provider = provider or str(binding.get("provider") or binding.get("adapter") or "external")
        session_id = session_id or binding.get("providerSessionId")
    elif not session_id:
        if not provider:
            return ""
        sessions = read(root / "chat_sessions.json")
        session_id = sessions.get(f"{provider}:{save_id}") or sessions.get(f"life:{provider}:{save_id}")
        if not session_id and provider == "agy":
            session_id = sessions.get(save_id)
    if not provider or not isinstance(session_id, str) or not session_id.strip():
        return ""
    return "partner-" + hashlib.sha256(
        json.dumps([connection_source, provider, session_id]).encode("utf-8")
    ).hexdigest()[:24]


def _recall_terms(text: str) -> set[str]:
    """Small local lexical recall, including Chinese without a tokenizer service."""
    terms = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(segment[i:i + 2] for i in range(max(1, len(segment) - 1)))
    return terms


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
                            **v,
                            "memoryRevision": int(v.get("memoryRevision", 0)),
                            "instructionRevision": int(v.get("instructionRevision", v.get("memoryRevision", 0))),
                            "entries": [self._migrate_entry(dict(e)) for e in v.get("entries", []) if isinstance(e, dict)] if isinstance(v.get("entries"), list) else [],
                            "notes": [dict(n) for n in v.get("notes", []) if isinstance(n, dict)] if isinstance(v.get("notes"), list) else [],
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
        # Earlier versions assigned every preference to the constraint layer.
        # The saved kind is the player's explicit distinction from an agreement.
        if entry.get("kind") == "preference" and entry.get("category") == "constraint":
            entry["category"] = "preference"
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
                    "revoked", "superseded_by", "revisionReason", "title", "scope",
                    "validOn", "derivedFrom", "companionId"}
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
        agreement→constraint, preference→preference); a scope-limited observation may be
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
                "text": str(text),
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
                    e["text"] = str(text)
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
                    e["revisionReason"] = str(reason or "")
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
                "text": str(text or ""),
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
            old["revisionReason"] = str(reason or "")
            entries.append(new_entry)
            record["memoryRevision"] = current_rev + 1
            if old.get("kind") != "event":
                record["instructionRevision"] = int(record.get("instructionRevision", current_rev)) + 1
            result["memoryRevision"] = record["memoryRevision"]
            result["entry"] = dict(new_entry)

        self._mutate(save_id, apply)
        return status_holder[0], result

    def save_note(self, save_id: str, text: str, *, companion_id: str,
                  game_date: str = "unknown", source: str = "companion", scope: str | None = None,
                  kind: str = "memory", title: str = "", note_id: str | None = None,
                  supersedes: list[str] | str | None = None, valid_on: str | None = None,
                  ) -> tuple[str, dict[str, Any]]:
        """Save a model-authored note or reusable method as unexecuted prose.

        Updating or consolidating explicitly named notes is atomic. Historical
        versions remain on disk; arbitrary scope prose is presented to the model,
        not interpreted as an action plan. valid_on optionally pins a game day.
        """
        if not companion_id:
            return "rejected", {"reason": "COMPANION_IDENTITY_UNAVAILABLE"}
        if kind not in {"memory", "method"} or source not in SOURCE_VALUES:
            return "rejected", {"reason": "INVALID_NOTE_KIND_OR_SOURCE"}
        if not isinstance(text, str) or not text.strip() or (scope is not None and (not isinstance(scope, str) or not scope.strip())):
            return "rejected", {"reason": "EMPTY_NOTE_OR_SCOPE"}
        targets = [supersedes] if isinstance(supersedes, str) else list(supersedes or [])
        if note_id:
            targets.append(note_id)
        result: dict[str, Any] = {}
        status = "confirmed"

        def apply(record: dict[str, Any]) -> None:
            nonlocal status
            notes = record.setdefault("notes", [])
            entries = record.setdefault("entries", [])
            previous = []
            for target in dict.fromkeys(targets):
                old = next((n for n in notes if n.get("id") == target
                            and n.get("companionId") == companion_id and self._is_current(n)), None)
                if old is None:
                    old = next((e for e in entries if e.get("id") == target and self._is_current(e)), None)
                if old is None:
                    status = "rejected"
                    result["reason"] = "NOTE_NOT_FOUND"
                    return
                previous.append(old)
            if any(e in entries for e in previous) and any(n in notes for n in previous):
                status = "rejected"
                result["reason"] = "MIXED_SHARED_AND_PRIVATE_NOTES"
                return
            original = next((n for n in previous if n.get("id") == note_id), None)
            resolved_scope = scope if scope is not None else (original or {}).get("scope", "长期")
            resolved_day = valid_on if valid_on is not None else (original or {}).get("validOn")
            note = {"id": self._entry_id(), "kind": kind, "title": title,
                    "text": text.strip(), "source": source, "scope": resolved_scope.strip(),
                    "gameDate": game_date, "companionId": companion_id,
                    "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
            if resolved_day:
                note["validOn"] = resolved_day
            if previous:
                note["derivedFrom"] = [n["id"] for n in previous]
            for old in previous:
                old["superseded_by"] = note["id"]
            shared_origin = next((e for e in previous if e in entries), None)
            if shared_origin is not None:
                # A legacy shared agreement stays shared after a correction.
                # Do not convert player-wide information into one partner's note.
                note["kind"] = shared_origin.get("kind", "agreement")
                note["category"] = shared_origin.get("category", "fact")
                note.pop("companionId", None)
                entries.append(note)
                if note["kind"] != "event":
                    record["instructionRevision"] = int(record.get("instructionRevision", 0)) + 1
            else:
                notes.append(note)
            record["memoryRevision"] = int(record.get("memoryRevision", 0)) + 1
            result.update(note=dict(note), memoryRevision=record["memoryRevision"])

        self._mutate(save_id, apply)
        return status, result

    def forget_note(self, save_id: str, note_id: str, *, companion_id: str,
                    reason: str = "") -> tuple[str, dict[str, Any]]:
        """Withdraw an owned note while retaining provenance on disk."""
        result: dict[str, Any] = {}
        status = "rejected"

        def apply(record: dict[str, Any]) -> None:
            nonlocal status
            for note in record.get("notes", []):
                if companion_id and note.get("id") == note_id and note.get("companionId") == companion_id:
                    note.update(revoked=True, revisionReason=reason)
                    record["memoryRevision"] = int(record.get("memoryRevision", 0)) + 1
                    result.update(memoryRevision=record["memoryRevision"])
                    status = "confirmed"
                    return
            if companion_id:
                for entry in record.get("entries", []):
                    if entry.get("id") == note_id:
                        entry.update(revoked=True, revisionReason=reason)
                        record["memoryRevision"] = int(record.get("memoryRevision", 0)) + 1
                        if entry.get("kind") != "event":
                            record["instructionRevision"] = int(record.get("instructionRevision", 0)) + 1
                        result.update(memoryRevision=record["memoryRevision"])
                        status = "confirmed"
                        return
            result["reason"] = "NOTE_NOT_FOUND"

        self._mutate(save_id, apply)
        return status, result

    def recall(self, save_id: str, query: str = "", *, companion_id: str,
               game_date: str = "", kind: str | None = None,
               note_id: str | None = None, offset: int = 0,
               limit: int | None = None) -> dict[str, Any]:
        """Recall relevant prose with its source and scope; never execute methods.

        Empty queries list current owned notes. Explicit IDs can inspect a dated
        note after its applicable day, labelled historical. Lexical ranking is a
        local retrieval aid, not semantic understanding or a truth judgement.
        """
        self._load()
        record = self._data.get(save_id, {})
        terms = _recall_terms(query)

        def rank(rows: list[dict]) -> list[dict]:
            scored = []
            for row in reversed(rows):
                score = len(terms & _recall_terms(" ".join(str(row.get(k) or "") for k in ("text", "title", "scope"))))
                if not terms or score or note_id:
                    scored.append((score, row))
            return [dict(row) for _, row in sorted(scored, key=lambda pair: pair[0], reverse=True)]

        notes = []
        for note in record.get("notes", []):
            if not companion_id or note.get("companionId") != companion_id:
                continue
            if not note_id and not self._is_current(note):
                continue
            if kind and note.get("kind") != kind or note_id and note.get("id") != note_id:
                continue
            dated = note.get("validOn")
            if dated and dated != game_date and not note_id:
                continue
            notes.append({**note, "status": "revoked" if note.get("revoked") else "superseded" if note.get("superseded_by") else "current",
                          "applicability": "historical" if dated and dated != game_date else "check_scope",
                          "worldEvidence": "历史记录；涉及当前世界的结论须重新观察。"})
        entries = [e for e in record.get("entries", [])
                   if (e.get("id") == note_id if note_id else self._is_current(e)
                       and (not e.get("validOn") or e["validOn"] == game_date))] if kind != "method" else []
        ranked_notes, ranked_entries = rank(notes), rank(entries)
        # Explicit pagination is for browsing, never a storage eviction policy.
        combined = [("notes", row) for row in ranked_notes] + [("entries", row) for row in ranked_entries]
        start = max(0, offset)
        page = combined[start:] if limit is None else combined[start:start + max(1, limit)]
        return {"memoryRevision": int(record.get("memoryRevision", 0)),
                "notes": [row for bucket, row in page if bucket == "notes"],
                "entries": [row for bucket, row in page if bucket == "entries"],
                "total": len(combined), "nextOffset": start + len(page) if start + len(page) < len(combined) else None}

    def render_for_context(self, save_id: str, *, companion_id: str = "", query: str = "",
                           game_date: str = "", context_budget: int = 6000) -> dict[str, Any]:
        """Render memory in a compact form suitable for LLM context injection.

        Prose is preserved whole; relevant recall is available for focused queries.
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
        current = [e for e in entries if self._is_current(e)
                   and (not e.get("validOn") or e["validOn"] == game_date)]
        recent = self.recall(save_id, companion_id=companion_id, game_date=game_date)
        related = self.recall(save_id, query, companion_id=companion_id, game_date=game_date) if query else {"entries": [], "notes": []}
        recent_events = list(reversed([e for e in current if e.get("kind") == "event"]))[:3]
        recent_ids = {e.get("id") for e in recent_events}
        related_ids = {e.get("id") for e in related["entries"]}
        extra_events = [e for e in reversed(current) if e.get("kind") == "event"
                        and e.get("id") in related_ids and e.get("id") not in recent_ids]
        notes = recent["notes"][:3]
        recent_note_ids = {n.get("id") for n in notes}
        notes += [n for n in related["notes"] if n.get("id") not in recent_note_ids]
        remaining = max(0, context_budget)

        def compact(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal remaining
            selected = []
            for row in rows:
                cost = len(json.dumps(row, ensure_ascii=False))
                if cost <= remaining:
                    selected.append(row)
                    remaining -= cost
                elif remaining >= 200:
                    # Keep an addressable pointer, never a chopped sentence.
                    pointer = {"id": row.get("id"), "kind": row.get("kind"),
                               "title": row.get("title", ""), "source": row.get("source"),
                               "gameDate": row.get("gameDate"), "category": row.get("category"),
                               "scope": row.get("scope"), "validOn": row.get("validOn"),
                               "recallRequired": True,
                               "text": "完整笔记请使用 consult recall_memory 按 note_id 读取。"}
                    pointer_cost = len(json.dumps(pointer, ensure_ascii=False))
                    if pointer_cost <= remaining:
                        selected.append(pointer)
                        remaining -= pointer_cost
            return selected

        # Shared agreements and immediate continuity must not be crowded out by
        # an arbitrarily large learned-method library. Everything stays within
        # the same prompt budget; full prose remains explicitly retrievable.
        current = compact([e for e in current if e.get("kind") != "event"] + recent_events)
        relevant_notes = compact(notes)
        current += compact(extra_events)

        agreements = [e for e in current if isinstance(e, dict) and e.get("kind") == "agreement"]
        preferences = [e for e in current if isinstance(e, dict) and e.get("kind") == "preference"]
        events = [e for e in current if isinstance(e, dict) and e.get("kind") == "event"]

        ap_combined = agreements + preferences
        recent_events = list(reversed(events))

        def _layered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rendered: list[dict[str, Any]] = []
            for e in rows:
                label = _CATEGORY_LABELS.get(str(e.get("category")), "事实")
                row: dict[str, Any] = {
                    "id": e.get("id"),
                    "label": label,
                    "text": e.get("text"),
                    "gameDate": e.get("gameDate"),
                    "source": e.get("source"),
                }
                if e.get("validFrom") or e.get("validTo"):
                    row["validity"] = {"from": e.get("validFrom"), "to": e.get("validTo")}
                rendered.append(row)
            return rendered

        return {
            "relevantNotes": relevant_notes,
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
