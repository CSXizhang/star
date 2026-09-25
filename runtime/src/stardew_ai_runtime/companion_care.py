"""Companion care service: rules engine for proactive companion messages.

Triggers:
  - morning: first snapshot of a new game day (kind=morning)
  - work-done: work success terminal (kind=work-done, commandId deduped)
  - evening: first timeOfDay >= 1900 for the day (kind=evening)

Frequency limits (per contract §2):
  - quiet: no proactive cares (0/day)
  - moderate: ≤ 2/day
  - chatty: ≤ 4/day
  - min 2 game-hours (120 minutes) between consecutive proactive cares

Persistence: run_dir/data/companion-care.json
  {saveId: {firedKeys: {eventKey: gameDate}, dayCounts: {gameDate: int},
            lastFiredTimeOfDay: int, lastFiredGameDate: str}}

eventKey = "{kind}:{gameDate}:{ref}" — persisted for cross-restart dedup.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("stardew_ai_runtime.companion_care")

_FREQUENCY_LIMITS = {"quiet": 0, "moderate": 2, "chatty": 4}
_MIN_GAME_HOUR_GAP = 120  # 2 game hours in game-minutes


class CompanionCareService:
    """Tracks which care events have been fired and enforces frequency rules."""

    def __init__(self, state_path: Path | str) -> None:
        self.state_path = Path(state_path)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ I/O
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
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

    def _mutate(self, save_id: str, fn: Any) -> Any:
        if not save_id:
            raise ValueError("save_id is required")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with open(lock_path, "a+b") as lock:
            self._lock(lock)
            try:
                self._load()
                record = self._data.setdefault(save_id, self._default_record())
                result = fn(record)
                self._write_unlocked()
                return result
            finally:
                self._unlock(lock)

    @staticmethod
    def _default_record() -> dict[str, Any]:
        return {
            "firedKeys": {},
            "dayCounts": {},
            "lastFiredTimeOfDay": -1,
            "lastFiredGameDate": "",
        }

    # ---------------------------------------------------------------- public API
    def maybe_fire(
        self,
        save_id: str,
        kind: str,
        game_date: str,
        ref: str,
        care_frequency: str,
        time_of_day: int,
    ) -> str | None:
        """Check if a care event should fire; return event_key if yes, None if no.

        The caller is responsible for actually generating the text via the
        life session model and sending the life.care message.
        """
        if care_frequency == "quiet":
            return None

        daily_limit = _FREQUENCY_LIMITS.get(care_frequency, 2)
        event_key = f"{kind}:{game_date}:{ref}"

        result_holder: list[str | None] = [None]

        def check_and_record(record: dict[str, Any]) -> None:
            fired_keys: dict[str, str] = record.get("firedKeys", {})
            day_counts: dict[str, int] = record.get("dayCounts", {})
            last_fired_tod = int(record.get("lastFiredTimeOfDay", -1))
            last_fired_date = str(record.get("lastFiredGameDate", ""))

            # Already fired this exact event (cross-restart dedup)
            if event_key in fired_keys:
                return

            # Must be today's event
            if fired_keys.get(event_key, game_date) != game_date:
                return

            # Daily count check
            today_count = int(day_counts.get(game_date, 0))
            if today_count >= daily_limit:
                return

            # Minimum 2-game-hour gap between fires on same day
            if last_fired_date == game_date and last_fired_tod >= 0:
                gap = time_of_day - last_fired_tod
                if gap < _MIN_GAME_HOUR_GAP:
                    return

            # All checks pass — record the fire
            fired_keys[event_key] = game_date
            day_counts[game_date] = today_count + 1
            record["firedKeys"] = fired_keys
            record["dayCounts"] = day_counts
            record["lastFiredTimeOfDay"] = time_of_day
            record["lastFiredGameDate"] = game_date
            result_holder[0] = event_key

        self._mutate(save_id, check_and_record)
        return result_holder[0]

    def is_fired(self, save_id: str, event_key: str) -> bool:
        """Check if an event has already been fired (read-only)."""
        self._load()
        record = self._data.get(save_id, {})
        return event_key in record.get("firedKeys", {})
