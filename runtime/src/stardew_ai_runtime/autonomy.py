"""Small, persisted autonomy policy for the three-day player-led trial.

This module deliberately does not call an LLM. It stores per-save preferences,
chooses at most one observable next action from a fresh snapshot, and leaves
execution to the existing scheduler/MCP path.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN_SECONDS = 300.0


def get_breaker_threshold() -> int:
    try:
        return max(1, int(os.getenv("STARDEW_AUTONOMY_BREAKER_THRESHOLD", str(DEFAULT_BREAKER_THRESHOLD))))
    except (TypeError, ValueError):
        return DEFAULT_BREAKER_THRESHOLD


def get_breaker_cooldown_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("STARDEW_AUTONOMY_BREAKER_COOLDOWN_SECONDS", str(DEFAULT_BREAKER_COOLDOWN_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_BREAKER_COOLDOWN_SECONDS


@dataclass
class SaveAutonomyState:
    enabled: bool = False
    mode: str = "command"
    paused: bool = False
    goal: str = ""
    budget_limit: int | None = None
    box_preference: str = "none"
    preferences_revision: int = 0
    decision_epoch: int = 0
    last_decision_fingerprint: str | None = None
    game_date: str | None = None
    daily_spend: int = 0
    spend_reservations: dict[str, int] = field(default_factory=dict)
    settled_spend_commands: list[str] = field(default_factory=list)
    day: int | None = None
    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    last_event_key: str | None = None
    daily_tokens: dict[str, dict[str, int | None]] = field(default_factory=dict)
    last_action_fingerprint: str | None = None
    failure_count: int = 0
    last_attempt_revision: int = -1
    breaker_tripped: bool = False
    breaker_cooldown_until: float | None = None
    breaker_reason: str | None = None

    def __post_init__(self) -> None:
        # ``enabled`` was the pre-free-mode public field.  Read old files as
        # free mode, but never grant spending permission during migration.
        if self.mode not in {"command", "free"}:
            self.mode = "free" if self.enabled else "command"
        if self.enabled and self.mode == "command":
            self.mode = "free"
        self.enabled = self.mode == "free"
        self.daily_spend = max(0, int(self.daily_spend or 0))


class AutonomyController:
    """Player-controlled, one-action-at-a-time autonomy state machine."""

    def __init__(self, state_path: Path | str):
        self.state_path = Path(state_path)
        self._states: dict[str, SaveAutonomyState] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            for save_id, value in raw.items():
                if isinstance(value, dict):
                    value = dict(value)
                    for wire, field_name in {
                        "preferencesRevision": "preferences_revision",
                        "decisionEpoch": "decision_epoch",
                        "lastDecisionFingerprint": "last_decision_fingerprint",
                        "gameDate": "game_date", "dailySpend": "daily_spend",
                        "spendReservations": "spend_reservations",
                        "settledSpendCommands": "settled_spend_commands",
                        "failureCount": "failure_count",
                        "breakerTripped": "breaker_tripped",
                        "breakerCooldownUntil": "breaker_cooldown_until",
                        "breakerReason": "breaker_reason",
                    }.items():
                        if wire in value and field_name not in value:
                            value[field_name] = value[wire]
                    self._states[save_id] = SaveAutonomyState(
                        **{k: value[k] for k in SaveAutonomyState.__dataclass_fields__ if k in value}
                    )
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self._states = {}

    def reload(self) -> None:
        """Refresh from the shared run-dir file before every cross-process read."""
        self._load()

    def _save(self) -> None:
        """Write the current map while holding the inter-process lock.

        Mutating callers use ``_mutate`` so the file is re-read while this
        lock is held.  Keeping the low-level writer separate prevents one
        process from replacing another process's newer save state.
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with open(lock_path, "a+b") as lock:
            try:
                self._lock(lock)
                self._write_unlocked()
            finally:
                self._unlock(lock)

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
        values = {}
        for key, state in self._states.items():
            item = asdict(state)
            item.update({
                "preferencesRevision": item.pop("preferences_revision"),
                "decisionEpoch": item.pop("decision_epoch"),
                "lastDecisionFingerprint": item.pop("last_decision_fingerprint"),
                "gameDate": item.pop("game_date"),
                "dailySpend": item.pop("daily_spend"),
                "spendReservations": item.pop("spend_reservations"),
                "settledSpendCommands": item.pop("settled_spend_commands"),
                "failureCount": item.get("failure_count", 0),
                "breakerTripped": item.pop("breaker_tripped"),
                "breakerCooldownUntil": item.pop("breaker_cooldown_until"),
                "breakerReason": item.pop("breaker_reason"),
            })
            values[key] = item
        temp.write_text(
            json.dumps(values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.state_path)

    def _mutate(self, save_id: str, mutate: Callable[[SaveAutonomyState], None]) -> SaveAutonomyState:
        if not save_id:
            raise ValueError("save_id is required")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with open(lock_path, "a+b") as lock:
            self._lock(lock)
            try:
                self._load()
                state = self._states.setdefault(save_id, SaveAutonomyState())
                mutate(state)
                self._write_unlocked()
                return state
            finally:
                self._unlock(lock)

    def state(self, save_id: str) -> SaveAutonomyState:
        if not save_id:
            raise ValueError("save_id is required")
        self._load()
        return self._states.setdefault(save_id, SaveAutonomyState())

    def set_enabled(self, save_id: str, enabled: bool) -> SaveAutonomyState:
        return self.set_mode(save_id, "free" if enabled else "command")

    def set_mode(self, save_id: str, mode: str) -> SaveAutonomyState:
        if mode not in {"command", "free"}:
            raise ValueError("mode must be command or free")
        def mutate(state: SaveAutonomyState) -> None:
            value = mode == "free"
            if value and state.mode != "free":
                state.last_action_fingerprint = None
                state.last_decision_fingerprint = None
                state.decision_epoch += 1
                state.paused = False
            state.failure_count = 0
            state.breaker_tripped = False
            state.breaker_cooldown_until = None
            state.breaker_reason = None
            state.mode = mode
            state.enabled = value
            if not value:
                state.paused = False
        return self._mutate(save_id, mutate)

    def set_paused(self, save_id: str, paused: bool) -> SaveAutonomyState:
        def mutate(state: SaveAutonomyState) -> None:
            state.paused = bool(paused)
            state.failure_count = 0
            state.breaker_tripped = False
            state.breaker_cooldown_until = None
            state.breaker_reason = None
        return self._mutate(save_id, mutate)

    def control(self, save_id: str, action: str, **params: Any) -> SaveAutonomyState:
        if action == "set_mode":
            return self.set_mode(save_id, str(params.get("mode", "command")))
        if action == "pause":
            return self.set_paused(save_id, True)
        if action == "resume":
            return self.set_paused(save_id, False)
        if action == "cancel":
            return self.set_mode(save_id, "command")
        if action == "set_preferences":
            return self.set_preferences(
                save_id,
                goal=params.get("goal"),
                budget_limit=params.get("budget_limit"),
                box_preference=params.get("box_preference"),
            )
        raise ValueError("unsupported autonomy control action")

    def set_preferences(
        self, save_id: str, *, goal: str | None = None, budget_limit: int | None = None,
        box_preference: str | None = None
    ) -> SaveAutonomyState:
        if budget_limit is not None and budget_limit < 0:
            raise ValueError("budget_limit must be non-negative")
        def mutate(state: SaveAutonomyState) -> None:
            changed = False
            if goal is not None and state.goal != goal.strip():
                state.goal = goal.strip()
                changed = True
            if budget_limit is not None and state.budget_limit != budget_limit:
                state.budget_limit = budget_limit
                changed = True
            if box_preference is not None:
                normalized = box_preference.strip()
                if normalized != state.box_preference:
                    changed = True
                state.box_preference = normalized or "none"
            if changed:
                state.preferences_revision += 1
                state.decision_epoch += 1
                state.last_decision_fingerprint = None
                state.failure_count = 0
                state.breaker_tripped = False
                state.breaker_cooldown_until = None
                state.breaker_reason = None
        return self._mutate(save_id, mutate)

    def on_day_started(self, save_id: str, day: int, *, game_date: str | None = None) -> SaveAutonomyState:
        def mutate(state: SaveAutonomyState) -> None:
            state.day = int(day)
            state.completed = []
            state.pending = []
            state.game_date = game_date or str(day)
            state.daily_spend = 0
            state.decision_epoch += 1
            state.last_decision_fingerprint = None
            state.failure_count = 0
            state.breaker_tripped = False
            state.breaker_cooldown_until = None
            state.breaker_reason = None
        return self._mutate(save_id, mutate)

    def request_job_decision(self, save_id: str) -> None:
        def mutate(state: SaveAutonomyState) -> None:
            state.decision_epoch += 1
            state.last_decision_fingerprint = None
        self._mutate(save_id, mutate)

    def is_cooling_down(self, save_id: str, now: float | None = None) -> bool:
        state = self.state(save_id)
        if not state.breaker_tripped or state.breaker_cooldown_until is None:
            return False
        current = now if now is not None else time.time()
        return current < state.breaker_cooldown_until

    def reset_breaker(self, save_id: str) -> SaveAutonomyState:
        def mutate(state: SaveAutonomyState) -> None:
            state.failure_count = 0
            state.breaker_tripped = False
            state.breaker_cooldown_until = None
            state.breaker_reason = None
        return self._mutate(save_id, mutate)

    def next_candidate(self, save_id: str, snapshot: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
        """Return one explainable task from fresh native data, or None when idle."""
        state = self.state(save_id)
        if not state.enabled or state.paused or self.is_cooling_down(save_id, now=now):
            return None
        world = snapshot.get("world") if isinstance(snapshot.get("world"), dict) else {}
        day = world.get("dayOfMonth")
        season = world.get("season", "unknown")
        day_key = f"{world.get('year', '?')}:{season}:{day}" if day is not None else None
        if day_key and state.game_date != day_key:
            self.on_day_started(save_id, int(day), game_date=day_key)
            state = self.state(save_id)
            if self.is_cooling_down(save_id, now=now):
                return None
        farm = snapshot.get("farmWork") if isinstance(snapshot.get("farmWork"), dict) else {}
        if farm.get("matureCropCount", 0) > 0:
            return {"kind": "harvest", "reason": "成熟作物待收", "max_tiles": 16}
        crops = farm.get("cropUnwateredTiles")
        if isinstance(crops, list) and crops:
            return {"kind": "water", "reason": "作物需要浇水", "tiles": crops[:64]}
        # One agent decision at enable/new-day/goal change is useful for
        # planting or other saved goals; it is not a recurring idle planner.
        decision_key = self.fingerprint(save_id, snapshot, {"kind": "decision", "reason": "daily-decision", "goal": state.goal}, state)
        if state.last_decision_fingerprint != f"{state.decision_epoch}:{decision_key}":
            return {"kind": "decision", "reason": "daily-decision", "goal": state.goal}
        return None

    @staticmethod
    def fingerprint(save_id: str, snapshot: dict[str, Any], candidate: dict[str, Any], state: SaveAutonomyState | None = None) -> str:
        world = snapshot.get("world") if isinstance(snapshot.get("world"), dict) else {}
        farm = snapshot.get("farmWork") if isinstance(snapshot.get("farmWork"), dict) else {}
        tiles = candidate.get("tiles") or []
        inventory_raw = snapshot.get("inventory") or {}
        slots = inventory_raw.get("slots", []) if isinstance(inventory_raw, dict) else []
        items = sorted((x.get("itemId"), x.get("stack", x.get("count", 0))) for x in slots if isinstance(x, dict) and not x.get("isTool"))
        chests_raw = snapshot.get("chests") or []
        if isinstance(chests_raw, dict):
            chests_raw = chests_raw.get("items", [])
        chests = chests_raw if isinstance(chests_raw, list) else []
        chest_summary = sorted((c.get("freeSlots"), c.get("capacity")) for c in chests if isinstance(c, dict))
        shop = snapshot.get("shop") if isinstance(snapshot.get("shop"), dict) else {}
        mature = farm.get("matureCrops") if isinstance(farm, dict) else []
        mature = mature if isinstance(mature, list) else []
        mature_summary = sorted((x.get("x"), x.get("y"), x.get("cropId")) for x in mature if isinstance(x, dict))
        pending = farm.get("cropUnwateredTiles", []) if isinstance(farm, dict) else []
        pending = pending if isinstance(pending, list) else []
        pending_summary = sorted((x.get("x"), x.get("y")) for x in pending if isinstance(x, dict))
        return (
            f"{save_id}:{world.get('year', '?')}:{world.get('season', '?')}:{world.get('dayOfMonth', '?')}:"
            f"{candidate.get('kind')}:{candidate.get('reason')}:{mature_summary}:{pending_summary}:"
            f"{candidate.get('goal', '')}:{json.dumps(tiles, sort_keys=True, ensure_ascii=False)}:"
            f"{json.dumps(items, sort_keys=True)}:{json.dumps(chest_summary)}:{shop.get('isOpen', shop.get('open'))}:"
            f"{state.budget_limit if state else None}:{state.preferences_revision if state else None}"
        )

    def record_world_event(self, save_id: str, fingerprint: str, revision: int) -> bool:
        """Accept only meaningful native state changes, with bounded retries."""
        accepted = False
        def mutate(state: SaveAutonomyState) -> None:
            nonlocal accepted
            if not state.enabled:
                return
            decision_key = f"{state.decision_epoch}:{fingerprint}"
            if state.last_decision_fingerprint == decision_key:
                return
            state.last_action_fingerprint = fingerprint
            state.last_decision_fingerprint = decision_key
            state.last_attempt_revision = int(revision)
            accepted = True
        self._mutate(save_id, mutate)
        return accepted

    def record_action_result(
        self,
        save_id: str,
        fingerprint: str | None = None,
        success: bool = True,
        *,
        reason: str | None = None,
        now: float | None = None,
    ) -> SaveAutonomyState:
        threshold = get_breaker_threshold()
        cooldown = get_breaker_cooldown_seconds()
        current_time = now if now is not None else time.time()

        def mutate(state: SaveAutonomyState) -> None:
            if fingerprint is not None and state.last_action_fingerprint != fingerprint:
                return
            if success:
                state.failure_count = 0
                state.breaker_tripped = False
                state.breaker_cooldown_until = None
                state.breaker_reason = None
            else:
                state.failure_count += 1
                state.breaker_reason = reason or state.breaker_reason or "action_failed"
                if state.failure_count >= threshold:
                    state.breaker_tripped = True
                    state.breaker_cooldown_until = current_time + max(0.0, cooldown)

        return self._mutate(save_id, mutate)

    def record_completion(self, save_id: str, action: str) -> SaveAutonomyState:
        action = action.strip()
        def mutate(state: SaveAutonomyState) -> None:
            if action and action not in state.completed:
                state.completed.append(action)
        return self._mutate(save_id, mutate)

    def record_event(self, save_id: str, event_key: str, event: str) -> bool:
        """Record one bridge/mod lifecycle event; duplicate event keys are ignored."""
        accepted = False
        def mutate(state: SaveAutonomyState) -> None:
            nonlocal accepted
            if state.last_event_key != event_key:
                state.last_event_key = event_key
                accepted = True
        self._mutate(save_id, mutate)
        return accepted

    def record_usage(self, save_id: str, day_key: str, usage: dict[str, Any] | int | None) -> SaveAutonomyState:
        if isinstance(usage, int):
            usage = {"total_tokens": usage}
        def mutate(state: SaveAutonomyState) -> None:
            current = state.daily_tokens.get(day_key) or {
                "input_tokens": None, "cache_read_tokens": None,
                "output_tokens": None, "total_tokens": None,
            }
            if usage is None:
                state.daily_tokens.setdefault(day_key, current)
                return
            for key in ("input_tokens", "cache_read_tokens", "output_tokens", "total_tokens"):
                value = usage.get(key)
                if value is None or not isinstance(value, int) or value < 0:
                    continue
                current[key] = (current.get(key) or 0) + value
            state.daily_tokens[day_key] = current
        return self._mutate(save_id, mutate)

    def reserve_spend(self, save_id: str, command_id: str, quoted_cost: int, limit: int | None = None) -> int:
        """Reserve a native quote once; duplicate command ids are idempotent."""
        if quoted_cost < 0:
            raise ValueError("quoted_cost must be non-negative")
        result = 0
        def mutate(state: SaveAutonomyState) -> None:
            nonlocal result
            if command_id in state.spend_reservations:
                result = state.spend_reservations[command_id]
                return
            if command_id in state.settled_spend_commands:
                return
            state_limit = state.budget_limit if state.budget_limit is not None else 0
            remaining = max(0, state_limit - state.daily_spend - sum(state.spend_reservations.values()))
            allowed = min(limit if limit is not None else state_limit, remaining)
            if quoted_cost > allowed:
                raise ValueError("daily autonomy budget exceeded")
            state.spend_reservations[command_id] = quoted_cost
            result = quoted_cost
        self._mutate(save_id, mutate)
        return result

    def settle_spend(self, save_id: str, command_id: str, actual_cost: int | None, *, unknown: bool = False) -> SaveAutonomyState:
        def mutate(state: SaveAutonomyState) -> None:
            reserved = state.spend_reservations.get(command_id)
            if reserved is None:
                return
            if unknown or actual_cost is None:
                return
            state.daily_spend += max(0, actual_cost)
            state.spend_reservations.pop(command_id, None)
            if command_id not in state.settled_spend_commands:
                state.settled_spend_commands.append(command_id)
        return self._mutate(save_id, mutate)
