"""Companion profile store: per-save personality and play-style settings.

Persistence follows the same pattern as autonomy.py:
- single JSON file keyed by saveId
- cross-process .lock (msvcrt/fcntl)
- tmp+replace atomic write
- every mutate holds the lock and re-reads before writing
- camelCase wire format
- missing file / missing save key => default values (profile=None)
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

PLAY_STYLES = frozenset({"earn", "workhorse", "community", "decor"})
PERSONALITIES = frozenset({"gentle", "lively", "calm", "tsundere"})
CARE_FREQUENCIES = frozenset({"quiet", "moderate", "chatty"})

DEFAULT_COMPANION_NAME = "阿星"

_WIRE_TO_FIELD: dict[str, str] = {
    "onboarded": "onboarded",
    "skipped": "skipped",
    "playStyle": "play_style",
    "personality": "personality",
    "careFrequency": "care_frequency",
    "companionName": "companion_name",
    "profileRevision": "profile_revision",
}

_FIELD_TO_WIRE: dict[str, str] = {v: k for k, v in _WIRE_TO_FIELD.items()}


class CompanionProfileStore:
    """Persistent companion profile, one record per save.

    File: ``<run_dir>/data/companion-profile.json``
    Format: ``{saveId: {onboarded, skipped, playStyle, personality,
               careFrequency, companionName, profileRevision}}``

    A missing file or missing saveId key is treated as not-onboarded
    (profile=None). Every successful ``set()`` increments ``profileRevision``.
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
                for save_id, wire in raw.items():
                    if not isinstance(wire, dict):
                        continue
                    record: dict[str, Any] = {}
                    for wire_key, field_name in _WIRE_TO_FIELD.items():
                        if wire_key in wire:
                            record[field_name] = wire[wire_key]
                    self._data[str(save_id)] = record
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
        # Serialize back to camelCase wire format
        output: dict[str, Any] = {}
        for save_id, record in self._data.items():
            wire: dict[str, Any] = {}
            for field, wire_key in _FIELD_TO_WIRE.items():
                if field in record:
                    wire[wire_key] = record[field]
            output[save_id] = wire
        temp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    def _mutate(self, save_id: str, fn: Any) -> dict[str, Any]:
        """Acquire lock, reload, apply fn, write atomically."""
        if not save_id:
            raise ValueError("save_id is required")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with open(lock_path, "a+b") as lock:
            self._lock(lock)
            try:
                self._load()
                record = self._data.setdefault(save_id, self._default_record())
                fn(record)
                self._write_unlocked()
                return dict(record)
            finally:
                self._unlock(lock)

    # ---------------------------------------------------------------- defaults
    @staticmethod
    def _default_record() -> dict[str, Any]:
        return {
            "onboarded": False,
            "skipped": False,
            "play_style": "earn",
            "personality": "gentle",
            "care_frequency": "moderate",
            "companion_name": DEFAULT_COMPANION_NAME,
            "profile_revision": 0,
        }

    # ---------------------------------------------------------------- public API
    def get(self, save_id: str) -> dict[str, Any]:
        """Return profile dict for save_id.

        Returns ``{"profile": None, "profileRevision": 0}`` when the save
        has not been onboarded yet (missing or onboarded=False and skipped=False).
        Returns ``{"profile": {...}, "profileRevision": N}`` when onboarded or skipped.
        """
        self._load()
        record = self._data.get(save_id)
        revision = 0
        if record is None:
            return {"profile": None, "profileRevision": 0}
        revision = int(record.get("profile_revision", 0))
        if not record.get("onboarded") and not record.get("skipped"):
            return {"profile": None, "profileRevision": revision}
        profile = {
            "onboarded": bool(record.get("onboarded", False)),
            "skipped": bool(record.get("skipped", False)),
            "playStyle": record.get("play_style", "earn"),
            "personality": record.get("personality", "gentle"),
            "careFrequency": record.get("care_frequency", "moderate"),
            "companionName": record.get("companion_name", DEFAULT_COMPANION_NAME),
        }
        return {"profile": profile, "profileRevision": revision}

    def get_raw(self, save_id: str) -> dict[str, Any] | None:
        """Return raw internal record (all fields) or None if absent."""
        self._load()
        return dict(self._data[save_id]) if save_id in self._data else None

    def set(
        self,
        save_id: str,
        patch: dict[str, Any],
        expected_revision: int,
    ) -> tuple[str, dict[str, Any]]:
        """Apply patch if expected_revision matches current revision.

        Returns (status, payload) where status is 'confirmed' or 'rejected'.
        payload contains profileRevision and profile fields.
        """
        result: dict[str, Any] = {}
        status_holder: list[str] = ["confirmed"]

        def apply(record: dict[str, Any]) -> None:
            current_rev = int(record.get("profile_revision", 0))
            if current_rev != expected_revision:
                status_holder[0] = "rejected"
                result["reason"] = "STALE_REVISION"
                result["profileRevision"] = current_rev
                return

            # Apply allowed patch fields
            if "onboarded" in patch:
                record["onboarded"] = bool(patch["onboarded"])
            if "skipped" in patch:
                record["skipped"] = bool(patch["skipped"])
            if "playStyle" in patch:
                val = str(patch["playStyle"])
                if val not in PLAY_STYLES:
                    status_holder[0] = "rejected"
                    result["reason"] = f"INVALID_PLAY_STYLE:{val}"
                    result["profileRevision"] = current_rev
                    return
                record["play_style"] = val
            if "personality" in patch:
                val = str(patch["personality"])
                if val not in PERSONALITIES:
                    status_holder[0] = "rejected"
                    result["reason"] = f"INVALID_PERSONALITY:{val}"
                    result["profileRevision"] = current_rev
                    return
                record["personality"] = val
            if "careFrequency" in patch:
                val = str(patch["careFrequency"])
                if val not in CARE_FREQUENCIES:
                    status_holder[0] = "rejected"
                    result["reason"] = f"INVALID_CARE_FREQUENCY:{val}"
                    result["profileRevision"] = current_rev
                    return
                record["care_frequency"] = val
            if "companionName" in patch:
                name = str(patch["companionName"]).strip()
                if not (1 <= len(name) <= 12):
                    status_holder[0] = "rejected"
                    result["reason"] = "INVALID_COMPANION_NAME"
                    result["profileRevision"] = current_rev
                    return
                record["companion_name"] = name

            record["profile_revision"] = current_rev + 1
            new_rev = record["profile_revision"]
            result["profileRevision"] = new_rev
            result["profile"] = {
                "onboarded": bool(record.get("onboarded", False)),
                "skipped": bool(record.get("skipped", False)),
                "playStyle": record.get("play_style", "earn"),
                "personality": record.get("personality", "gentle"),
                "careFrequency": record.get("care_frequency", "moderate"),
                "companionName": record.get("companion_name", DEFAULT_COMPANION_NAME),
            }

        self._mutate(save_id, apply)
        return status_holder[0], result
