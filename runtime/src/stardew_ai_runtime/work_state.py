"""Per-save persistent work state: goals, short task plans, todos, execution log.

Design constraints:

* One shared store file may hold several saves (``run-dir/data/work-state.json``).
* Atomic writes behind a cross-process lock, mirroring ``AutonomyController``.
* The harness, not the model, records execution outcomes: a step can only become
  ``completed``/``partial`` through :meth:`WorkStore.commit_step_result`, which
  requires the stable command id persisted before dispatch.
* A ready step is claimed under the lock with a lease so two worker processes
  cannot execute the same step.
* Waiting conditions are evaluated from a fresh native snapshot; the goal memory
  is never treated as the latest inventory fact.

The model may create/revise goals, plans and todos through MCP tools, but it
cannot mark an unexecuted task succeeded.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time as _time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GOAL_SOURCES = frozenset({"user", "agent"})
GOAL_STATUSES = frozenset({"active", "paused", "completed", "cancelled"})
TASK_STATUSES = frozenset(
    {"pending", "running", "waiting", "completed", "partial", "unknown", "cancelled", "failed"}
)
STEP_STATUSES = frozenset(
    {"pending", "running", "waiting", "completed", "partial", "unknown", "cancelled", "failed"}
)
TODO_STATUSES = frozenset({"pending", "due", "done", "cancelled", "expired"})
OUTCOMES = frozenset({"completed", "partial", "unknown", "cancelled", "failed"})
REQUEST_ORIGINS = frozenset({"player", "player-midflight", "autonomy"})

# Plan steps may only reference these existing MCP operations. No shell, no code.
ALLOWED_OPERATIONS = frozenset(
    {
        "observe_player_activities", "talk_npc", "interact_tile", "fish", "attack", "native_menu_select",
        "observe_building_services",
        "observe_production",
        "eat_food",
        "cut_grass",
        "build_building",
        "upgrade_building",
        "purchase_animal",
        "observe_farming_helpers",
        "observe_crafting",
        "observe_farm_space",
        "observe_machines",
        "observe_livestock",
        "refill_watering_can",
        "apply_fertilizer",
        "craft_items",
        "move_building",
        "place_items",
        "remove_items",
        "clear_debris",
        "pickup_items",
        "chop_tree",
        "insert_machine",
        "collect_machine",
        "pet_animal",
        "collect_animal_produce",
        "feed_animals",
        "toggle_animal_door",
        "get_work_overview",
        "get_status",
        "query_farm_work",
        "query_inventory",
        "query_chests",
        "query_planting_options",
        "query_route",
        "query_shop",
        "query_wiki",
        "water_zone",
        "water_tiles",
        "water_auto",
        "harvest_auto",
        "deposit_to_chest",
        "withdraw_from_chest",
        "organize_chest",
        "hoe_tiles",
        "plant_seeds",
        "ship_items",
        "purchase_items",
        "sleep",
        "open_shop",
        "open_chest",
        "native_advance",
        "native_answer",
        "navigate_to",
        "pause_task",
        "resume_task",
        "cancel_task",
    }
)
READ_ONLY_OPERATIONS = frozenset(
    {
        "observe_player_activities",
        "observe_building_services",
        "observe_production",
        "observe_farming_helpers",
        "observe_crafting",
        "observe_farm_space",
        "observe_machines",
        "observe_livestock",
        "get_work_overview",
        "get_status",
        "query_farm_work",
        "query_inventory",
        "query_chests",
        "query_planting_options",
        "query_shop",
        "query_route",
        "query_wiki",
    }
)

SEASON_ORDER = {"spring": 1, "summer": 2, "fall": 3, "winter": 4}
TERMINAL_STEP_STATUSES = frozenset({"completed", "partial", "cancelled", "failed"})


class WorkStateError(ValueError):
    """Invalid work-state mutation (validation failure)."""


PLANT_WORKFLOW_PLAN_ERROR = json.dumps({
    "errorCode": "COMPOSITE_STEP_UNSUPPORTED", "saved": False,
    "execution": "not_started", "operation": "plant_crop_workflow",
    "recommendedAction": "submit_plan",
    "message": "未执行、未保存作业。plant_crop_workflow 是旧多阶段兼容接口，不能作为一个可对账的持久化步骤。请按已观察的种子、箱子和地块，拆成独立 withdraw_from_chest / hoe_tiles / plant_seeds / water_tiles 步骤；需要时在同一任务中加入导航。不同类型、地图和数量的合法原子操作仍可组合。",
}, ensure_ascii=False)


# Supported explicit waiting conditions. A waiting step is only re-claimable when
# its own named condition is satisfied by a *fresh native snapshot* (or by the
# bounded transport backoff), never merely because another snapshot arrived.
WAIT_CONDITION_TYPES = frozenset(
    {
        "gameDay",  # a complete <year>:<season>:<day> has been reached
        "inventory",  # the companion inventory holds itemId >= minCount
        "cropState",  # a native crop reached the named state (mature/unwatered)
        "shopOpen",  # the named shop reports isOpen
        "tileClear",  # the named tile is no longer occupied by the player
        "obstacleChange",  # a named obstacle marker disappeared from world state
        "transportRetry",  # bounded retry backoff for transient transport errors
    }
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _calendar_key(year: Any, season: Any, day: Any) -> tuple[int, int, int] | None:
    try:
        season_index = SEASON_ORDER.get(str(season).lower())
        if season_index is None or year is None or day is None:
            return None
        return (int(year), season_index, int(day))
    except (TypeError, ValueError):
        return None


def _day_key(year: Any, season: Any, day: Any) -> str | None:
    """Stable ``<year>:<season>:<day>`` key for the day-rollover idempotency guard."""
    if year is None or season is None or day is None:
        return None
    return f"{year}:{season}:{day}"


def _parse_day_key(key: str | None) -> tuple[int, int, int] | None:
    if not key or not isinstance(key, str):
        return None
    parts = key.split(":")
    if len(parts) != 3:
        return None
    return _calendar_key(parts[0], parts[1], parts[2])


@dataclass
class StepWaitCondition:
    """One explicit, natively checkable reason a step is parked.

    ``attemptsAllowed`` bounds ``transportRetry`` so a transient transport fault
    can never turn into an unbounded retry loop; every other condition is
    re-evaluated against real native state.
    """

    type: str
    params: dict[str, Any] = field(default_factory=dict)
    reason_code: str | None = None
    created_at: str = field(default_factory=_now_iso)
    attempts_allowed: int = 1
    attempts_used: int = 0
    retry_after: float = 0.0


def describe_wait_condition(condition: Any) -> str | None:
    """Render a short human/model-readable wait reason (never invents state)."""
    if condition is None:
        return None
    if isinstance(condition, StepWaitCondition):
        kind, params = condition.type, condition.params
        reason = condition.reason_code
    elif isinstance(condition, dict):
        kind = condition.get("type")
        params = condition.get("params") if isinstance(condition.get("params"), dict) else {}
        reason = condition.get("reasonCode")
    else:
        return None
    detail = ""
    if kind == "gameDay":
        detail = f"{params.get('year')}-{params.get('season')}-{params.get('day')}"
    elif kind == "inventory":
        detail = f"{params.get('itemId')}>={params.get('minCount')}"
    elif kind == "cropState":
        detail = f"{params.get('cropId')}:{params.get('state')}"
    elif kind == "shopOpen":
        detail = str(params.get("shopId"))
    elif kind == "tileClear":
        detail = f"({params.get('x')},{params.get('y')})"
    elif kind == "obstacleChange":
        detail = str(params.get("marker") or params.get("reasonCode") or "")
    elif kind == "transportRetry":
        detail = f"backoff attemptsUsed={params.get('attemptsUsed')}"
    text = f"wait:{kind}"
    if detail:
        text = f"{text}:{detail}"
    return f"{text} ({reason})" if reason else text


def _parse_iso_epoch(value: str | None) -> float:
    """Parse a persisted ISO timestamp into epoch seconds (0.0 when unparseable)."""
    if not value or not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _step_wait_from_dict(value: Any) -> StepWaitCondition | None:
    """Rebuild a persisted wait condition; unknown/legacy shapes become ``None``.

    Persisted shapes are the dataclass field names (snake_case); camelCase keys
    are accepted as a fallback for hand-written/older payloads.
    """
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    if kind not in WAIT_CONDITION_TYPES:
        return None
    params = value.get("params") if isinstance(value.get("params"), dict) else {}
    attempts_allowed = value.get("attempts_allowed", value.get("attemptsAllowed", 1))
    attempts_used = value.get("attempts_used", value.get("attemptsUsed", 0))
    retry_after = value.get("retry_after", value.get("retryAfter", 0.0))
    created_at = value.get("created_at", value.get("createdAt"))
    reason_code = value.get("reason_code", value.get("reasonCode"))
    return StepWaitCondition(
        type=str(kind),
        params=dict(params),
        reason_code=reason_code,
        created_at=str(created_at or _now_iso()),
        attempts_allowed=int(attempts_allowed) if isinstance(attempts_allowed, (int, float)) else 1,
        attempts_used=int(attempts_used) if isinstance(attempts_used, (int, float)) else 0,
        retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else 0.0,
    )


@dataclass
class Step:
    id: str
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    command_id: str | None = None
    outcome: str | None = None
    reason_code: str | None = None
    effects: list[dict[str, Any]] = field(default_factory=list)
    feedback: dict[str, Any] = field(default_factory=dict)
    lease_owner: str | None = None
    lease_until: float = 0.0
    attempts: int = 0
    decision_requested: bool = False
    # Explicit native condition that gates re-claiming this step. ``None`` means
    # the step is simply pending; a waiting step always carries one.
    wait: StepWaitCondition | None = None


@dataclass
class Task:
    id: str
    goal_id: str
    title: str
    completion_condition: str = ""
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"
    steps: list[Step] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    decision_token: str | None = None


@dataclass
class Goal:
    id: str
    text: str
    source: str
    priority: int = 0
    constraints: dict[str, Any] = field(default_factory=dict)
    # Advisory design/phase/open questions; execution facts remain in the ledger.
    project: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    # Goal hierarchy: exactly one active ``main`` goal per save; ``stage`` and
    # ``daily`` goals hang off it via ``parent_id``. Day settlement records
    # progress on the main goal but never closes it.
    kind: str = "stage"
    parent_id: str | None = None
    epoch: int = 0
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


GOAL_KINDS = frozenset({"main", "stage", "daily"})
_PROGRESS_LOG_LIMIT = 30
_PROGRESS_NOTE_LIMIT = 500


@dataclass
class Todo:
    id: str
    intent: str
    trigger: dict[str, Any]
    goal_id: str | None = None
    status: str = "pending"
    expiry: dict[str, Any] | None = None
    created_at: str = field(default_factory=_now_iso)


@dataclass
class ExecutionEntry:
    command_id: str
    task_id: str
    step_id: str
    operation: str
    outcome: str
    effects: list[dict[str, Any]] = field(default_factory=list)
    reason_code: str | None = None
    snapshot_revision: int | None = None
    game_date: str | None = None
    at: str = field(default_factory=_now_iso)
    location_id: str | None = None
    game_time: int | None = None
    request_id: str | None = None
    goal_id: str | None = None


@dataclass
class SaveWorkState:
    goals: list[Goal] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    executions: list[ExecutionEntry] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)
    paused: bool = False
    scheduler_epoch: int = 0
    decision: dict[str, Any] = field(default_factory=dict)
    last_job: dict[str, Any] = field(default_factory=dict)
    # Last complete game day this save was settled for, as "<year>:<season>:<day>".
    # The day rollover is idempotent on this key: settling the same day twice is a
    # no-op, and loading an older save is never treated as a new day.
    last_settled_day: str | None = None
    # Completed/cancelled task facts archived by the day rollover. Bounded.
    archive: list[dict[str, Any]] = field(default_factory=list)
    branch_failures: list[dict[str, Any]] = field(default_factory=list)
    # Original request contracts (text plus selected-task binding). Bounded to
    # the newest 50; requests still owed a delivery are never trimmed.
    requests: list[dict[str, Any]] = field(default_factory=list)
    # Persistent terminal-fact outbox. Undelivered events are never trimmed;
    # only the newest 100 consumed events are bounded away.
    outbox: list[dict[str, Any]] = field(default_factory=list)


# Retired engine allowance fields never become model instructions again. Player
# prose and one-time reservedFunds remain untouched; archives keep source history.
def _current_constraints(value: Any) -> Any:
    retired = {"purchaseBudget", "dailyPurchaseBudget", "perPurchaseLimit", "daily_budget", "per_purchase_max"}
    if isinstance(value, dict):
        return {key: _current_constraints(item) for key, item in value.items() if key not in retired}
    if isinstance(value, list):
        return [_current_constraints(item) for item in value]
    return value


def bind_execution_scope_params(operation: str, params: dict[str, Any],
                                goal_constraints: dict[str, Any] | None) -> dict[str, Any]:
    """Project an accepted exact scope into auto selection without mutating a plan."""
    result = dict(params)
    if operation not in {"water_auto", "harvest_auto"}:
        return result
    objective = (goal_constraints or {}).get("objectiveScope")
    scope = objective.get("executionScope") if isinstance(objective, dict) else None
    if not isinstance(scope, dict):
        return result
    location = scope.get("locationId")
    if not isinstance(location, str) or not location.strip():
        raise WorkStateError("Saved executionScope requires an observed locationId")

    def coordinates(value: Any) -> list[dict[str, int]]:
        if not isinstance(value, list) or any(
            not isinstance(tile, dict) or any(type(tile.get(key)) is not int or tile[key] < 0
                                             for key in ("x", "y")) for tile in value
        ):
            raise WorkStateError("executionScope target tiles require non-negative integer x/y coordinates")
        return [{"x": x, "y": y} for x, y in sorted({(tile["x"], tile["y"]) for tile in value})]

    tiles = coordinates(scope.get("tiles"))
    if result.get("target_tiles") is not None:
        requested = {(tile["x"], tile["y"]) for tile in coordinates(result["target_tiles"])}
        tiles = [tile for tile in tiles if (tile["x"], tile["y"]) in requested]
    result["target_tiles"] = tiles
    result["location_id"] = location
    return result


def _outbox_event_from_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Fill the persistent delivery/notify structure, keeping recorded state."""
    event = dict(value)
    delivery = {"state": "pending", "attempts": 0, "sentAt": None, "consumedAt": None}
    delivery.update(event.get("delivery") or {})
    event["delivery"] = delivery
    notify = {
        "key": f"{event.get('commandId')}:{event.get('outcome')}",
        "state": "pending",
        "deliveredAt": None,
    }
    notify.update(event.get("notify") or {})
    event["notify"] = notify
    return event


def _request_is_pending(state: SaveWorkState, request: dict[str, Any]) -> bool:
    if request.get("origin") == "player-midflight" and not request.get("consumedByDelivery"):
        return True
    request_id = request.get("id")
    if any(g.status == "active" and g.constraints.get("playerRequestId") == request_id
           for g in state.goals):
        return True
    if state.decision.get("requestId") == request_id and not state.decision.get("finished"):
        return True
    if any(t.id in request.get("taskIds", []) and t.status in {"pending", "running", "waiting", "unknown"}
           for t in state.tasks):
        return True
    return any(
        event.get("requestId") == request_id
        and (event.get("delivery") or {}).get("state") != "consumed"
        for event in state.outbox
    )


def _request_is_preset_bound(request: dict[str, Any]) -> bool:
    """Preset autostart requests stay forever: they are the binding source that
    ``preset_day_boundary`` uses to recover/verify the preset run's goalId, so
    the 50-request bound must never trim them away."""
    request_id = str(request.get("id") or "")
    return request_id.startswith("preset-")


def _trim_requests(state: SaveWorkState) -> None:
    # Only the oldest resolved requests are bounded away; pending contracts and
    # preset-bound originals stay.
    while len(state.requests) > 50:
        stale = next(
            (i for i, request in enumerate(state.requests)
             if not _request_is_preset_bound(request)
             and not _request_is_pending(state, request)),
            None,
        )
        if stale is None:
            return
        state.requests.pop(stale)


def _bounded_outbox(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def resolved(event):
        notify = event.get("notify") or {}
        return ((event.get("delivery") or {}).get("state") == "consumed"
                and (not notify.get("reply") or notify.get("state") == "delivered"))
    consumed = sum(1 for event in events if resolved(event))
    drop = max(0, consumed - 100)
    if drop == 0:
        return events
    kept: list[dict[str, Any]] = []
    for event in events:
        if drop and resolved(event):
            drop -= 1
            continue
        kept.append(event)
    return kept


def _append_outbox_event(state: SaveWorkState, event: dict[str, Any]) -> dict[str, Any]:
    explicit_kind = event.get("kind") or "terminal"
    command_id = str(event.get("commandId"))
    kind = explicit_kind
    unknown_fact = any(
        e.get("commandId") == command_id and e.get("outcome") == "unknown"
        for e in state.outbox
    )
    if kind == "terminal" and unknown_fact and event.get("outcome") != "unknown":
        kind = "correction"
    if kind == "correction":
        reason = event.get("reasonCode")
        existing = next(
            (
                e
                for e in state.outbox
                if e.get("kind") == "correction"
                and e.get("commandId") == command_id
                and e.get("outcome") == event.get("outcome")
                and e.get("reasonCode") == reason
            ),
            None,
        )
    else:
        existing = next(
            (
                e
                for e in state.outbox
                if e.get("kind") == "terminal" and e.get("commandId") == command_id
            ),
            None,
        )
    if existing is not None and (existing.get("delivery") or {}).get("state") == "consumed":
        return dict(existing)
    stored = _outbox_event_from_dict({**event, "kind": kind})
    if existing is not None:
        stored["id"] = existing.get("id") or _new_id("obx")
        stored["delivery"] = existing["delivery"]
        previous_notify = existing.get("notify") or {}
        stored["notify"]["state"] = previous_notify.get("state", "pending")
        stored["notify"]["deliveredAt"] = previous_notify.get("deliveredAt")
        existing.clear()
        existing.update(stored)
        return dict(existing)
    stored.setdefault("id", _new_id("obx"))
    state.outbox.append(stored)
    return dict(stored)


class WorkStore:
    """Cross-process, per-save work state store."""

    def __init__(self, state_path: Path | str):
        self.state_path = Path(state_path)
        self._states: dict[str, SaveWorkState] = {}
        self._thread_lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------ IO
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self._states = {}
            return
        if not isinstance(raw, dict):
            self._states = {}
            return
        states: dict[str, SaveWorkState] = {}
        try:
            for save_id, value in raw.items():
                if not isinstance(value, dict):
                    continue
                states[save_id] = SaveWorkState(
                    goals=[
                        Goal(
                            **{
                                **g,
                                "kind": g.get("kind") or ("main" if g.get("source") == "preset" else "stage"),
                                "parent_id": g.get("parent_id") if g.get("parent_id") is not None else g.get("parentId"),
                                "constraints": _current_constraints(g.get("constraints") or {}),
                            }
                        )
                        for g in value.get("goals", []) if isinstance(g, dict)
                    ],
                    tasks=[
                        Task(
                            **{
                                **t,
                                "steps": [
                                    Step(**{**s, "wait": _step_wait_from_dict(s.get("wait"))})
                                    for s in t.get("steps", [])
                                    if isinstance(s, dict)
                                ],
                            }
                        )
                        for t in value.get("tasks", [])
                        if isinstance(t, dict)
                    ],
                    todos=[Todo(**t) for t in value.get("todos", []) if isinstance(t, dict)],
                    executions=[
                        ExecutionEntry(**e)
                        for e in value.get("executions", [])
                        if isinstance(e, dict)
                    ],
                    notices=[n for n in value.get("notices", []) if isinstance(n, dict)],
                    paused=bool(value.get("paused", False)),
                    scheduler_epoch=int(value.get("schedulerEpoch", 0)),
                    decision=dict(value.get("decision") or {}),
                    last_job=dict(value.get("lastJob") or {}),
                    last_settled_day=(
                        str(value["lastSettledDay"])
                        if value.get("lastSettledDay") is not None
                        else None
                    ),
                    archive=[a for a in value.get("archive", []) if isinstance(a, dict)],
                    branch_failures=[b for b in value.get("branchFailures", []) if isinstance(b, dict)],
                    requests=[r for r in value.get("requests", []) if isinstance(r, dict)],
                    outbox=[_outbox_event_from_dict(o) for o in value.get("outbox", []) if isinstance(o, dict)],
                )
        except (TypeError, ValueError):
            states = {}
        self._states = states

    def reload(self) -> None:
        with self._thread_lock:
            self._load()

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
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        payload: dict[str, Any] = {}
        for save_id, state in self._states.items():
            payload[save_id] = {
                "goals": [asdict(g) for g in state.goals],
                "tasks": [asdict(t) for t in state.tasks],
                "todos": [asdict(t) for t in state.todos],
                "executions": [asdict(e) for e in state.executions],
                "notices": state.notices,
                "paused": state.paused,
                "schedulerEpoch": state.scheduler_epoch,
                "decision": state.decision,
                "lastJob": state.last_job,
                "lastSettledDay": state.last_settled_day,
                # Bound the archived fact log so it never grows without limit.
                "archive": state.archive[-200:],
                "branchFailures": state.branch_failures[-60:],
                "requests": state.requests,
                "outbox": _bounded_outbox(state.outbox),
            }
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(4):
            try:
                temp.replace(self.state_path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                _time.sleep(0.05 * (attempt + 1))

    def _mutate(self, save_id: str, mutate: Callable[[SaveWorkState], Any]) -> Any:
        # Reads reload this instance's state map. Protect it while another thread
        # prepares a mutation, or a read can replace the map before commit and
        # silently write the old decision back despite the cross-process lock.
        with self._thread_lock:
            return self._mutate_locked(save_id, mutate)

    def _mutate_locked(self, save_id: str, mutate: Callable[[SaveWorkState], Any]) -> Any:
        if not save_id:
            raise WorkStateError("save_id is required")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with open(lock_path, "a+b") as lock:
            self._lock(lock)
            try:
                self._load()
                state = self._states.setdefault(save_id, SaveWorkState())
                self._settle_cancelled_selection(state)
                result = mutate(state)
                self._settle_cancelled_selection(state)
                self._write_unlocked()
                return result
            except Exception:
                # A rejected mutation or failed replacement is not persisted
                # work. Discard its prepared in-memory goals/todos as well.
                self._load()
                raise
            finally:
                self._unlock(lock)

    def state(self, save_id: str) -> SaveWorkState:
        if not save_id:
            raise WorkStateError("save_id is required")
        with self._thread_lock:
            self._load()
            return self._states.setdefault(save_id, SaveWorkState())

    def request_player_decision(self, save_id: str, goal_id: str, message: str) -> dict[str, Any]:
        """Explicit model signal, separate from routine completion text."""
        text = message.strip()
        if not text or len(text) > 1000:
            raise WorkStateError("Player decision message must contain 1..1000 characters")
        def mutate(state: SaveWorkState) -> dict[str, Any]:
            self._find_goal(state, goal_id)
            previous = next((n for n in state.notices if n["goalId"] == goal_id and n["message"] == text and not n.get("answeredAt")), None)
            if previous:
                return dict(previous)
            notice = {"id": _new_id("notice"), "goalId": goal_id, "message": text, "delivered": False}
            state.notices.append(notice)
            # Both pending questions and answered receipts are durable. A client
            # may miss the confirmation and reconnect much later; pruning its
            # answer would leave that client's persisted question pending forever.
            return dict(notice)
        return self._mutate(save_id, mutate)

    def pending_player_notice(self, save_id: str) -> dict[str, Any] | None:
        notice = next((n for n in self.state(save_id).notices if not n.get("delivered") and not n.get("answeredAt") and not n.get("dismissedAt")), None)
        return dict(notice) if notice else None

    def unresolved_player_notices(self, save_id: str) -> list[dict[str, Any]]:
        return [dict(n) for n in self.state(save_id).notices if not n.get("answeredAt") and not n.get("dismissedAt")]

    def set_notice_visibility(self, save_id: str, notice_ids: list[str], dismissed: bool) -> None:
        """Player-owned tombstones; hiding a question grants no execution authority."""
        def mutate(state: SaveWorkState) -> None:
            notices = [next((n for n in state.notices if n.get("id") == identifier), None) for identifier in notice_ids]
            if any(n is None for n in notices):
                raise WorkStateError("Player decision is missing")
            for notice in notices:
                if notice.get("answeredAt"):
                    continue
                if dismissed:
                    notice.setdefault("dismissedAt", _now_iso())
                    notice["delivered"] = True
                else:
                    notice.pop("dismissedAt", None)
                    notice["delivered"] = False
        self._mutate(save_id, mutate)

    def answer_player_notice(self, save_id: str, notice_id: str, text: str, request_id: str) -> None:
        def mutate(state: SaveWorkState) -> None:
            notice = next((n for n in state.notices if n.get("id") == notice_id), None)
            if notice is None:
                raise WorkStateError("Player decision is missing")
            if notice.get("dismissedAt"):
                raise WorkStateError("Player decision is dismissed")
            if notice.get("answeredAt"):
                return
            notice.update(answer=text, answerRequestId=request_id, answeredAt=_now_iso())
        self._mutate(save_id, mutate)

    def acknowledge_player_notice(self, save_id: str, notice_id: str) -> None:
        def mutate(state: SaveWorkState) -> None:
            notice = next((n for n in state.notices if n.get("id") == notice_id), None)
            if notice:
                notice["delivered"] = True
        self._mutate(save_id, mutate)

    # ------------------------------------------------------------- goals
    @staticmethod
    def _active_main_goal(state: SaveWorkState) -> Goal | None:
        return next(
            (g for g in state.goals if g.status == "active" and g.kind == "main"),
            None,
        )

    def add_goal(
        self,
        save_id: str,
        text: str,
        source: str = "user",
        priority: int = 0,
        constraints: dict[str, Any] | None = None,
        *,
        kind: str | None = None,
        parent_id: str | None = None,
    ) -> Goal:
        clean = (text or "").strip()
        if not clean:
            raise WorkStateError("goal text cannot be empty")
        if source not in GOAL_SOURCES:
            raise WorkStateError("goal source must be 'user' or 'agent'")
        if kind is not None and kind not in GOAL_KINDS:
            raise WorkStateError(f"invalid goal kind '{kind}'")

        goal: Goal | None = None
        resolved_kind: str | None = None
        resolved_parent: str | None = parent_id

        def mutate(state: SaveWorkState) -> None:
            nonlocal goal, resolved_kind, resolved_parent
            active_main = self._active_main_goal(state)
            if kind is None:
                # Default hierarchy: a preset assignment is always the main
                # goal; anything else becomes the first main goal when none is
                # active yet, otherwise a stage hung off the active main.
                resolved_kind = (
                    "main"
                    if source == "preset" or active_main is None
                    else "stage"
                )
            else:
                resolved_kind = kind
                if kind == "main" and active_main is not None:
                    raise WorkStateError(
                        "MAIN_GOAL_EXISTS: this save already has an active main goal; "
                        "attach new work as a stage/daily goal or finish the existing "
                        "main goal before starting another one"
                    )
            if resolved_kind in {"stage", "daily"} and resolved_parent is None and active_main is not None:
                resolved_parent = active_main.id
            goal = Goal(
                id=_new_id("goal"),
                text=clean,
                source=source,
                priority=int(priority),
                constraints=dict(constraints or {}),
                kind=resolved_kind,
                parent_id=resolved_parent,
            )
            state.goals.append(goal)
            state.scheduler_epoch += 1

        self._mutate(save_id, mutate)
        assert goal is not None
        return goal

    def reconcile_verified_preset_history(self, save_id: str, native_day: int | None) -> list[str]:
        """Restore post-save terminal receipts before accepting a different run.

        Native serialization precedes the sleep callback. Only the durable,
        exact-save/request/goal boundary with a verified native sleep can close
        an active goal restored from that older checkpoint. Human pauses and
        cancellations, unrelated goals and incomplete receipts are untouched.
        """
        if type(native_day) is not int:
            return []
        from .snapshot_cache import read_cache
        try:
            rows = read_cache(self.state_path.parent / "preset-day-boundary.json")
        except (OSError, ValueError):
            return []
        completed = []
        for request_id, row in rows.items():
            if not isinstance(row, dict) or row.get("saveId") != save_id or row.get("phase") != "completed":
                continue
            receipt = row.get("nativeSleep") or {}
            start, end, maximum = row.get("startDay"), receipt.get("endDay"), row.get("maxGameDays")
            if (receipt.get("via") != "native-sleep" or receipt.get("kind") != "day-advanced"
                    or type(start) is not int or type(end) is not int or type(maximum) is not int
                    or maximum < 1 or maximum > 28 or receipt.get("startDay") != start
                    or end < start + maximum or native_day < end
                    or row.get("requestId") != request_id or row.get("unfinishedTaskIdsAtBoundary")):
                continue
            goal = next((g for g in self.state(save_id).goals if g.id == row.get("goalId")), None)
            if (goal is None or goal.kind != "main" or goal.status != "active"
                    or goal.constraints.get("presetRequestId") != request_id):
                continue
            def restore_terminal(state, goal_id=goal.id, expected_request=request_id, boundary_receipt=receipt):
                restored = self._find_goal(state, goal_id)
                if restored.status != "active" or restored.constraints.get("presetRequestId") != expected_request:
                    return False
                if any(t.status not in {"completed", "cancelled"} for t in state.tasks if t.goal_id == restored.id):
                    raise WorkStateError("PROJECT_UNRESOLVED: saved preset still has unreconciled work")
                # Do not consult the newly configured run's budget here: this
                # receipt belongs to the exact older request just verified.
                restored.status = "completed"
                restored.project = {**restored.project, "phase": "completed",
                                    "completionAssessment": "restored-verified-native-sleep-boundary",
                                    "nativeBoundary": boundary_receipt}
                restored.updated_at = _now_iso()
                restored.epoch += 1
                self._retire_child_authorizations(state, restored)
                state.scheduler_epoch += 1
                for todo in state.todos:
                    if todo.goal_id == restored.id and todo.status in {"pending", "due"}:
                        todo.status = "done"
                return True
            if self._mutate(save_id, restore_terminal):
                completed.append(goal.id)
        return completed

    def ensure_preset_main_goal(self, save_id: str, request_id: str, authorized_text: str) -> Goal:
        """Accept a verified isolated preset once, before its first model turn.

        This records only the authorized objective, never selects native work.
        A model may start by waiting; that must still have a resumable main scope.
        """
        def mutate(state: SaveWorkState) -> Goal:
            for old_parent in state.goals:
                if old_parent.kind == "main" and old_parent.status in {"completed", "cancelled"}:
                    self._retire_child_authorizations(state, old_parent)
            root = next((r for r in state.requests if r.get("id") == request_id), None)
            if root is None or root.get("origin") != "player" or root.get("text") != authorized_text:
                raise WorkStateError("PRESET_REQUEST_MISMATCH")
            if root.get("goalId"):
                return self._find_goal(state, root["goalId"])
            if root.get("workStopped"):
                raise WorkStateError("WORK_STOPPED: cancelled preset cannot be revived")
            if self._active_main_goal(state) is not None:
                raise WorkStateError("MAIN_GOAL_EXISTS: preset cannot replace another active objective")
            goal = Goal(id=_new_id("goal"), text=authorized_text, source="user", kind="main",
                        constraints={"playerRequestId": request_id, "presetRequestId": request_id})
            state.goals.append(goal)
            root["goalId"] = goal.id
            state.scheduler_epoch += 1
            return goal
        return self._mutate(save_id, mutate)

    def bind_preset_main_goal(self, save_id: str, goal_id: str, authorized_text: str | None = None) -> Goal:
        """Bind the trusted isolated preset to its sole main goal, idempotently."""
        clean = authorized_text.strip() if authorized_text else None
        current = self._find_goal(self.state(save_id), goal_id)
        if current.status != "active" or (current.kind == "main" and (clean is None or current.text == clean)):
            return current
        def mutate(state: SaveWorkState) -> Goal:
            goal = self._find_goal(state, goal_id)
            if goal.status != "active" or (goal.kind == "main" and (clean is None or goal.text == clean)):
                return goal
            other = self._active_main_goal(state)
            if other is not None and other.id != goal_id:
                raise WorkStateError("MAIN_GOAL_EXISTS: cannot rebind another active main goal")
            if clean is not None and goal.text != clean:
                goal.project.setdefault("reason", goal.text)
                goal.text = clean
            goal.kind = "main"
            goal.parent_id = None
            goal.epoch += 1
            goal.updated_at = _now_iso()
            state.scheduler_epoch += 1
            return goal
        return self._mutate(save_id, mutate)

    def revise_goal(
        self,
        save_id: str,
        goal_id: str,
        *,
        text: str | None = None,
        priority: int | None = None,
        constraints: dict[str, Any] | None = None,
        status: str | None = None,
        project: dict[str, Any] | None = None,
    ) -> Goal:
        preset_finalize = False
        if status == "completed":
            preset_finalize = self._check_preset_completion(save_id, goal_id)
        def mutate(state: SaveWorkState) -> Goal:
            goal = self._find_goal(state, goal_id)
            if status == "completed":
                self._assert_main_goal_closable(state, goal, preset_finalize=preset_finalize)
            if goal.status == "cancelled" and status is not None and status != "cancelled":
                raise WorkStateError("cancelled goals cannot be resumed; create a new goal for a new assignment")
            if text is not None:
                if not text.strip():
                    raise WorkStateError("goal text cannot be empty")
                goal.text = text.strip()
            if priority is not None:
                goal.priority = int(priority)
            if constraints is not None:
                goal.constraints = dict(constraints)
            if project is not None:
                merged = {**goal.project, **project}
                if len(json.dumps(merged, ensure_ascii=False)) > 24000:
                    raise WorkStateError("project notes must fit within 24000 characters")
                goal.project = merged
            if status is not None:
                if status not in GOAL_STATUSES:
                    raise WorkStateError(f"invalid goal status '{status}'")
                goal.status = status
            goal.updated_at = _now_iso()
            goal.epoch += 1
            if status == "cancelled":
                self._cancel_goal_children(state, goal_id)
            state.scheduler_epoch += 1
            return goal

        return self._mutate(save_id, mutate)

    def sync_milestone_work(
        self, save_id: str, milestone_id: str, *, text: str,
        constraints: dict[str, Any], todos: list[dict[str, Any]], active: bool,
        existing_goal_id: str | None = None,
        before_write: Callable[[str, dict[str, str]], None] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Atomically reconcile a milestone's authorization, including retry recovery.

        Stable milestone identity prevents duplicate goals after a failed milestone
        file write. Changed terms invalidate old child plans before replacement.
        When supplied, before_write persists the canonical milestone and its
        prepared links while this work lock is held, before any work-file write.
        It also runs for an unchanged retry; its failure leaves work untouched.
        """
        def mutate(state: SaveWorkState) -> tuple[str, dict[str, str]]:
            goal = next((g for g in state.goals
                         if g.constraints.get("milestoneId") == milestone_id
                         or g.id == existing_goal_id), None)
            desired = {**constraints, "milestoneId": milestone_id}
            if goal is not None and goal.status == "cancelled":
                raise WorkStateError("ARRANGEMENT_WITHDRAWN: a cancelled arrangement requires a new player assignment")
            signature = {"text": text, "constraints": desired, "todos": todos}
            if goal is None:
                goal = Goal(id=_new_id("goal"), text=text, source="user", constraints=desired)
                state.goals.append(goal)
            unchanged = goal.constraints.get("milestoneSpec") == signature
            if unchanged and goal.status == ("active" if active else "paused"):
                ids = {
                    t.intent.split("：", 1)[0]: t.id for t in state.todos
                    if t.goal_id == goal.id and t.status in {"pending", "due", "done"}
                }
                if before_write is not None:
                    before_write(goal.id, dict(ids))
                return goal.id, ids
            self._cancel_goal_children(state, goal.id)
            goal.text = text
            goal.constraints = {**desired, "milestoneSpec": signature}
            goal.status = "active" if active else "paused"
            goal.epoch += 1
            goal.updated_at = _now_iso()
            state.scheduler_epoch += 1
            ids: dict[str, str] = {}
            if active:
                for spec in todos:
                    todo = Todo(id=_new_id("todo"), intent=spec["intent"],
                                trigger=dict(spec["trigger"]), goal_id=goal.id,
                                expiry=dict(spec["expiry"]) if spec.get("expiry") else None)
                    state.todos.append(todo)
                    ids[spec["key"]] = todo.id
            if before_write is not None:
                before_write(goal.id, dict(ids))
            return goal.id, ids

        return self._mutate(save_id, mutate)

    def _check_preset_completion(self, save_id: str, goal_id: str) -> bool:
        # Only the explicitly opted-in independent preset owns this day budget.
        # A daily report is not completion of its multi-day parent project.
        # Returns True exactly when this goal is the preset-bound goal whose
        # verified day boundary allows completion (the preset finalize path).
        from .preset_day_boundary import PresetDayBoundary
        boundary = PresetDayBoundary(self.state_path.parent.parent)
        if not boundary.enabled:
            return False
        row = boundary.record(save_id, self)
        if not row or row.get("goalId") != goal_id:
            return False
        try:
            snapshot = json.loads((self.state_path.parent / "life-snapshot.json").read_text(encoding="utf-8-sig"))
            payload = snapshot.get("payload", snapshot)
        except (OSError, ValueError):
            payload = None
        day = boundary.native_day(payload)
        if row.get("phase") != "completed" and (row.get("startDay") is None or day is None
                or day < row["startDay"] + row["maxGameDays"]):
            raise WorkStateError("PRESET_DAY_NOT_FINISHED: this is a multi-day goal; keep it active, record today's progress and plan the next day after native sleep/save")
        return True

    def _assert_main_goal_closable(self, state: SaveWorkState, goal: Goal, *, preset_finalize: bool) -> None:
        """The single long-term main goal is never closed by the model.

        A main goal may close only at a verified preset day boundary (the
        preset finalize path, whose budget check already passed) or by the
        player (source='user'). Model turns record daily progress instead.
        """
        if goal.kind != "main" or goal.status == "completed" or preset_finalize:
            return
        if goal.source == "user":
            return
        raise WorkStateError(
            "MAIN_GOAL_LOCKED: the long-term main goal stays active across day "
            "settlements; record today's progress on it instead, or ask the "
            "player to close it. Only a verified preset day boundary may end it."
        )

    def complete_goal(self, save_id: str, goal_id: str, *, verified_native_boundary: dict | None = None) -> Goal:
        """Close the model's project assessment without inventing native effects."""
        preset_finalize = self._check_preset_completion(save_id, goal_id)
        def mutate(state: SaveWorkState) -> Goal:
            goal = self._find_goal(state, goal_id)
            if goal.status == "completed":
                return goal
            self._assert_main_goal_closable(state, goal, preset_finalize=preset_finalize)
            tasks = [t for t in state.tasks if t.goal_id == goal_id]
            if any(t.status not in {"completed", "cancelled"} for t in tasks):
                raise WorkStateError("PROJECT_UNRESOLVED: finish or reconcile outstanding work before closing the project")
            completed = [t.id for t in tasks if t.status == "completed"]
            completed += [entry["task"]["id"] for entry in state.archive
                          if entry.get("task", {}).get("goal_id") == goal_id
                          and entry["task"].get("status") == "completed"]
            restored = bool(verified_native_boundary and verified_native_boundary.get("via") == "native-sleep"
                            and verified_native_boundary.get("kind") == "day-advanced"
                            and isinstance(verified_native_boundary.get("startDay"), int)
                            and isinstance(verified_native_boundary.get("endDay"), int)
                            and verified_native_boundary["endDay"] > verified_native_boundary["startDay"])
            if not completed and not restored:
                raise WorkStateError("PROJECT_NO_EXECUTION: no completed native task evidence; cancel an unneeded goal instead")
            goal.status = "completed"
            goal.project = {**goal.project, "phase": "completed", "completionAssessment": "model-reviewed",
                            "completedTaskIds": completed[-64:]}
            goal.updated_at = _now_iso()
            goal.epoch += 1
            self._retire_child_authorizations(state, goal)
            state.scheduler_epoch += 1
            for todo in state.todos:
                if todo.goal_id == goal_id and todo.status in {"pending", "due"}:
                    todo.status = "done"
            return goal
        return self._mutate(save_id, mutate)

    @staticmethod
    def _retire_child_authorizations(state: SaveWorkState, parent: Goal) -> list[str]:
        """End a closed project's child authorization, preserving its knowledge.

        A retained independent intent must explicitly declare carryAcrossGoals
        and a reason. Ordinary design notes are history, not perpetual authority.
        Never manufacture completed native work or cancel an unresolved command.
        """
        retired = []
        children = [g for g in state.goals if g.parent_id == parent.id and g.status in {"active", "paused"}]
        for child in children:
            if child.constraints.get("carryAcrossGoals") is True and isinstance(child.constraints.get("carryReason"), str) and child.constraints["carryReason"].strip():
                child.status = "paused"
                child.project = {**child.project, "phase": "retained-intent-awaiting-reacceptance", "closedParentId": parent.id,
                                 "retirementReason": "Explicit independent intent retained; closed parent cannot authorize further dispatch. Reaccept under an authorized main goal before resuming."}
                continue
            tasks = [t for t in state.tasks if t.goal_id == child.id]
            if any(s.command_id and s.status in {"running", "unknown", "waiting"} and s.operation not in READ_ONLY_OPERATIONS for t in tasks for s in t.steps):
                child.status = "paused"
                child.project = {**child.project, "phase": "needs-reconciliation", "closedParentId": parent.id}
                continue
            for task in tasks:
                if task.status not in {"completed", "cancelled"}:
                    WorkStore._cancel_task_steps(task)
            child.status = "cancelled"
            child.project = {**child.project, "phase": "retired-with-parent", "closedParentId": parent.id,
                             "retirementReason": "Parent authorization ended; facts and design notes remain available as history, not completed work."}
            child.updated_at = _now_iso()
            child.epoch += 1
            for todo in state.todos:
                if todo.goal_id == child.id and todo.status in {"pending", "due"}:
                    todo.status = "cancelled"
            retired.append(child.id)
        return retired

    def reconcile_closed_goal_children(self, save_id: str) -> list[str]:
        """Repair historical parent/child scope without inventing task success."""
        current = self.state(save_id)
        closed = {g.id for g in current.goals if g.kind == "main" and g.status in {"completed", "cancelled"}}
        def needs_repair(g):
            if g.parent_id not in closed or g.kind not in {"stage", "daily"} or g.status not in {"active", "paused"}:
                return False
            if g.status == "paused" and g.project.get("closedParentId") == g.parent_id:
                if (g.project.get("phase") == "retained-intent-awaiting-reacceptance"
                        and g.constraints.get("carryAcrossGoals") is True
                        and str(g.constraints.get("carryReason") or "").strip()):
                    return False
                if (g.project.get("phase") == "needs-reconciliation"
                        and any(s.command_id and s.status in {"running", "unknown", "waiting"}
                                and s.operation not in READ_ONLY_OPERATIONS
                                for t in current.tasks if t.goal_id == g.id for s in t.steps)):
                    return False
            return True
        if not any(needs_repair(g) for g in current.goals):
            return []  # A normal overview is a read, not an unnecessary file replacement.
        def mutate(state):
            retired = []
            for parent in state.goals:
                if parent.kind == "main" and parent.status in {"completed", "cancelled"}:
                    retired.extend(self._retire_child_authorizations(state, parent))
            if retired:
                state.scheduler_epoch += 1
            return retired
        return self._mutate(save_id, mutate)

    def add_progress_note(self, save_id: str, goal_id: str, note: str, game_date: str | None = None) -> Goal:
        """Append one bounded progress note to the goal's project progressLog.

        Day settlement and model summaries record progress here; the log keeps
        at most 30 notes of 500 characters each, trimming the oldest first.
        """
        clean = (note or "").strip()
        if not clean:
            raise WorkStateError("progress note cannot be empty")

        def mutate(state: SaveWorkState) -> Goal:
            goal = self._find_goal(state, goal_id)
            log = goal.project.get("progressLog")
            if not isinstance(log, list):
                log = []
            log.append({
                "date": game_date or "unknown",
                "note": clean[:_PROGRESS_NOTE_LIMIT],
                "at": _now_iso(),
            })
            goal.project = {**goal.project, "progressLog": log[-_PROGRESS_LOG_LIMIT:]}
            goal.updated_at = _now_iso()
            goal.epoch += 1
            return goal

        return self._mutate(save_id, mutate)

    def record_decision_note(self, save_id: str, goal_id: str, note: str, category: str,
                             location_id: str, basis_command_id: str | None = None) -> dict[str, Any]:
        """Persist bounded model interpretation/intent without creating authority."""
        if category not in {"observation", "hypothesis", "intent", "failure"} or not isinstance(note,str) or not note.strip() or len(note)>800:
            raise WorkStateError("Explicit category and a note of 1..800 characters required")
        if not isinstance(location_id,str) or not location_id.strip():
            raise WorkStateError("Explicit observation/intent map scope required")
        def mutate(state):
            goal=self._find_goal(state,goal_id)
            if goal.status!="active":
                raise WorkStateError("Decision notes require an active goal")
            basis=next((e for e in reversed(state.executions) if e.command_id==basis_command_id),None)
            row={"category":category,"text":note.strip(),"locationId":location_id,"recordedAt":_now_iso(),
                 "source":"model-interpretation" if category=="observation" else "model",
                 "basisCommandId":basis_command_id,"basisOperation":basis.operation if basis else None,
                 "verified": bool(basis and basis.outcome == "completed"),
                 "gameDate":basis.game_date if basis else state.last_settled_day,
                 "rule":"Current native evidence overrides this dated note. Intent is advisory, not additional authorization."}
            notes=goal.project.get("decisionNotes") or []
            if not any(n.get("text")==row["text"] and n.get("category")==category for n in notes if isinstance(n,dict)):
                goal.project={**goal.project,"decisionNotes":[*notes,row][-16:]}
                goal.updated_at=_now_iso()
            return row
        return self._mutate(save_id,mutate)

    def retire_budget_intents(self, save_id: str, goal_id: str) -> list[str]:
        """Expire unexecuted remainder at a verified native day boundary.

        This closes authorization, never claims failed/partial work succeeded.
        Dispatched commands lacking a terminal remain a reconciliation barrier.
        """
        def mutate(state):
            tasks = [t for t in state.tasks if t.goal_id == goal_id]
            if any(s.command_id and s.status in {"running", "unknown", "waiting"}
                   and s.operation not in READ_ONLY_OPERATIONS
                   for t in tasks for s in t.steps):
                raise WorkStateError("BOUNDARY_RECONCILIATION_REQUIRED: dispatched command lacks a terminal")
            retired = []
            for task in tasks:
                if task.status not in {"completed", "cancelled"}:
                    self._cancel_task_steps(task)
                    # Unavailable observations performed no native mutation.
                    # Preserve their unknown execution facts, but retire intent.
                    for step in task.steps:
                        if step.operation in READ_ONLY_OPERATIONS and step.status == "unknown":
                            step.status = "cancelled"
                    retired.append(task.id)
            for todo in state.todos:
                if todo.goal_id == goal_id and todo.status in {"pending", "due"}:
                    todo.status = "cancelled"
            state.scheduler_epoch += 1
            return retired
        return self._mutate(save_id, mutate)

    def cancel_goal(self, save_id: str, goal_id: str) -> Goal:
        return self.revise_goal(save_id, goal_id, status="cancelled")

    @staticmethod
    def _settle_cancelled_selection(state: SaveWorkState) -> None:
        """Release retired intent, but never invent a dispatched command's result.

        Run at the transaction boundary so every cancellation path, including
        persisted pre-fix state, obeys the same ownership rule. This is not a
        native result and must not replace last_job or enqueue a completion.
        """
        decision = state.decision
        if not decision.get("selected") or decision.get("finished"):
            return
        task = next((t for t in state.tasks if t.id == decision.get("taskId")), None)
        if task is None or task.status != "cancelled":
            return
        if any(s.command_id and s.status in {"running", "unknown", "waiting"} for s in task.steps):
            return
        decision.update(finished=True, retirementReason="TASK_CANCELLED")

    @staticmethod
    def _cancel_task_steps(task: Task) -> None:
        task.status = "cancelled"
        task.updated_at = _now_iso()
        for step in task.steps:
            if step.status in {"pending", "running", "unknown", "waiting"}:
                # Retiring intent cannot invent a terminal for a dispatched command.
                if step.command_id and step.status in {"running", "unknown", "waiting"}:
                    step.status = "unknown"
                    step.reason_code = step.reason_code or "UNCONFIRMED_AFTER_CANCEL"
                else:
                    step.status = "cancelled"
                step.lease_owner = None
                step.lease_until = 0.0

    def _cancel_goal_children(self, state: SaveWorkState, goal_id: str) -> None:
        for task in state.tasks:
            if task.goal_id == goal_id and task.status != "completed":
                self._cancel_task_steps(task)
        for todo in state.todos:
            if todo.goal_id == goal_id and todo.status in {"pending", "due"}:
                todo.status = "cancelled"

    def list_goals(self, save_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
        state = self.state(save_id)
        goals = state.goals
        if active_only:
            goals = [g for g in goals if g.status == "active"]
        return [asdict(g) for g in sorted(goals, key=lambda g: (-g.priority, g.created_at))]

    # -------------------------------------------------------------- plans
    def create_plan(self, save_id: str, goal_id: str, tasks: list[dict[str, Any]]) -> list[Task]:
        if not tasks:
            raise WorkStateError("plan must contain at least one task")

        created: list[Task] = []

        def mutate(state: SaveWorkState) -> None:
            created.extend(self._append_plan(state, goal_id, tasks))
            state.scheduler_epoch += 1

        self._mutate(save_id, mutate)
        return created

    def _append_plan(
        self, state: SaveWorkState, goal_id: str, tasks: list[dict[str, Any]]
    ) -> list[Task]:
        """Validate and append new tasks for an active goal (caller holds the lock)."""
        goal = self._find_goal(state, goal_id)
        if goal.status != "active":
            raise WorkStateError(f"goal '{goal_id}' is not active")

        plan: list[Task] = []
        existing_ids = {t.id for t in state.tasks}
        new_ids: set[str] = set()
        for raw in tasks:
            if not isinstance(raw, dict):
                raise WorkStateError("each task must be an object")
            task_id = str(raw.get("id") or _new_id("task"))
            if task_id in existing_ids or task_id in new_ids:
                raise WorkStateError(f"duplicate task id '{task_id}'")
            new_ids.add(task_id)
            title = str(raw.get("title") or "").strip()
            if not title:
                raise WorkStateError("task title cannot be empty")
            deps = [str(d) for d in (raw.get("dependencies") or [])]
            steps: list[Step] = []
            step_ids: set[str] = set()
            for raw_step in raw.get("steps") or []:
                if not isinstance(raw_step, dict):
                    raise WorkStateError("each step must be an object")
                operation = str(raw_step.get("operation") or "")
                if operation == "plant_crop_workflow":
                    raise WorkStateError(PLANT_WORKFLOW_PLAN_ERROR)
                if operation not in ALLOWED_OPERATIONS:
                    raise WorkStateError(
                        f"operation '{operation}' is not an allowed plan operation"
                    )
                params = raw_step.get("params") or {}
                if not isinstance(params, dict):
                    raise WorkStateError("step params must be an object")
                step_id = str(raw_step.get("id") or _new_id("step"))
                if step_id in step_ids:
                    raise WorkStateError(f"duplicate step id '{step_id}'")
                step_ids.add(step_id)
                # A plan may declare an explicit native wait condition; anything
                # unsupported is rejected rather than silently ignored.
                wait = None
                if raw_step.get("wait") is not None:
                    wait = self._coerce_wait_condition(raw_step.get("wait"), reason_code=None)
                steps.append(
                    Step(
                        id=step_id,
                        operation=operation,
                        params=dict(params),
                        wait=wait,
                        # A step that declares an explicit native condition starts
                        # parked (waiting), never pending-and-claimable: otherwise
                        # the first worker tick dispatched it once before the
                        # condition was ever evaluated (round6 waiting-test bug).
                        status="waiting" if wait is not None else "pending",
                    )
                )
            if not steps:
                raise WorkStateError(f"task '{task_id}' must contain at least one step")
            plan.append(
                Task(
                    id=task_id,
                    goal_id=goal_id,
                    title=title,
                    completion_condition=str(raw.get("completionCondition") or ""),
                    dependencies=deps,
                    steps=steps,
                    status="waiting" if any(s.wait is not None for s in steps) else "pending",
                )
            )

        known = existing_ids | new_ids
        for task in plan:
            for dep in task.dependencies:
                if dep not in known:
                    raise WorkStateError(f"task '{task.id}' depends on unknown task '{dep}'")

        # Cycle detection over existing + new tasks.
        graph: dict[str, list[str]] = {t.id: list(t.dependencies) for t in state.tasks}
        graph.update({t.id: list(t.dependencies) for t in plan})
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(node: str) -> None:
            if node in done:
                return
            if node in visiting:
                raise WorkStateError(f"dependency cycle detected at task '{node}'")
            visiting.add(node)
            for dep in graph.get(node, []):
                if dep in graph:
                    visit(dep)
            visiting.discard(node)
            done.add(node)

        for task in plan:
            visit(task.id)

        state.tasks.extend(plan)
        return plan

    def begin_decision(self, save_id: str, token: str, *, require_idle: bool = False,
                       goal_scope: str | None = None, goal_default: str | None = None,
                       request_id: str | None = None, request_ids: list[str] | None = None) -> None:
        """A fresh provider invocation, not a goal/todo, may select one short job."""
        def mutate(state: SaveWorkState) -> None:
            current = state.decision
            if any(t.status == "cancelled" and any(
                    s.command_id and s.status in {"running", "unknown", "waiting"} for s in t.steps)
                   for t in state.tasks):
                raise WorkStateError("GAME_BUSY: a cancelled native command still awaits its terminal result")
            selected_ids = current.get("taskIds") or [current.get("taskId")]
            if (current.get("selected") and not current.get("finished")
                    and any(t.id in selected_ids and t.status in {"pending", "running", "waiting", "unknown"}
                            for t in state.tasks)):
                raise WorkStateError("GAME_BUSY: the selected native job retains its decision until it settles")
            if require_idle:
                if state.paused or any(t.status in {"pending", "running", "waiting", "unknown"} for t in state.tasks):
                    raise WorkStateError("GAME_BUSY: resolve or cancel existing work, and resume paused work before starting an external turn")
                current = state.decision
                if current.get("expires", 0) > _time.time() and not current.get("finished"):
                    if current.get("token") == token and not current.get("selected"):
                        return  # Idempotent retry by this MCP connection.
                    raise WorkStateError("GAME_BUSY: another decision is still active")
            state.scheduler_epoch += 1
            state.decision = {"token": token, "epoch": state.scheduler_epoch,
                              "expires": _time.time() + 1200, "selected": False}
            known_requests = {r.get("id") for r in state.requests}
            if request_id in known_requests:
                state.decision["requestId"] = request_id
            offered = [r for r in request_ids or [] if r in known_requests]
            if offered:
                state.decision["requestIds"] = offered
            active_goals = [goal for goal in state.goals if goal.status == "active"]
            if goal_scope:
                if not any(g.id == goal_scope for g in active_goals):
                    raise WorkStateError("Scoped project is not active")
                state.decision["goalId"] = goal_scope
                state.decision["goalScope"] = goal_scope
            elif goal_default and any(g.id == goal_default for g in active_goals):
                # Soft thread continuity (e.g. an outbox delivery turn): an
                # unqualified submit_plan defaults to the originating goal, while
                # an explicit different goal_id remains legal new business.
                state.decision["goalId"] = goal_default
            elif len(active_goals) == 1:
                state.decision["goalId"] = active_goals[0].id
        self._mutate(save_id, mutate)

    @staticmethod
    def _assert_decision_valid(state: SaveWorkState, token: str | None, *, allow_append: bool = False) -> None:
        d = state.decision
        if not token or d.get("token") != token:
            raise WorkStateError("STALE_DECISION: decision token missing or revoked")
        if d.get("expires", 0) <= _time.time():
            raise WorkStateError("STALE_DECISION: decision token expired")
        if d.get("selected"):
            task = next((task for task in state.tasks if task.id == d.get("taskId")), None)
            if (not allow_append or d.get("finished") or task is None or task.status != "pending"
                    or any(step.status != "pending" or step.command_id or step.lease_owner for step in task.steps)):
                raise WorkStateError("JOB_IN_PROGRESS: the dispatched job must settle before it can be changed")

    @staticmethod
    def _decision_valid(state: SaveWorkState, token: str | None) -> bool:
        d = state.decision
        return bool(token and d.get("token") == token
                    and d.get("expires", 0) > _time.time() and not d.get("selected"))

    def revoke_decision(self, save_id: str) -> None:
        def mutate(state: SaveWorkState) -> None:
            state.scheduler_epoch += 1
            state.decision = {}
        self._mutate(save_id, mutate)

    def stop_request(self, save_id: str, request_id: str, *, reason: str) -> None:
        """Close scheduling intent independently of the native cancellation result."""
        def mutate(state: SaveWorkState) -> None:
            request = next((r for r in state.requests if r.get("id") == request_id), None)
            if request is not None:
                request.update(workStopped=True, workStoppedReason=reason, workStoppedAt=_now_iso())
                if request.get("origin") == "player-midflight":
                    request["consumedByDelivery"] = True
        self._mutate(save_id, mutate)

    def select_direct_job(self, save_id: str, token: str | None, operation: str) -> None:
        def mutate(state: SaveWorkState) -> None:
            self._assert_decision_valid(state, token)
            state.decision.update(selected=True, operation=operation)
        self._mutate(save_id, mutate)

    def finish_job(self, save_id: str, feedback: dict[str, Any], *, task_id: str | None = None, token: str | None = None) -> None:
        def mutate(state: SaveWorkState) -> None:
            if (task_id and state.decision.get("taskId") != task_id) or (token and state.decision.get("token") != token):
                return
            state.last_job = dict(feedback)
            state.last_job["decisionId"] = state.decision.get("token")
            if task_id:
                state.last_job["taskId"] = task_id
                task = next((t for t in state.tasks if t.id == task_id), None)
                if task:
                    state.last_job["taskTitle"] = task.title
                    state.last_job["goalId"] = task.goal_id
            state.decision["finished"] = True
        self._mutate(save_id, mutate)

    @staticmethod
    def validate_short_job(tasks: list[dict[str, Any]]) -> None:
        """Select one serial job; native operations validate their own prerequisites."""
        if len(tasks) != 1:
            raise WorkStateError("ONE_SHORT_JOB_REQUIRED: submit exactly one task with sequential steps")
        task = tasks[0]
        steps = task.get("steps") or []
        if not 1 <= len(steps) <= 32 or task.get("dependencies"):
            raise WorkStateError("Job requires 1..32 sequential steps and no task dependencies")
        if any(s.get("wait") for s in steps):
            raise WorkStateError("FUTURE_WAIT_IS_INTENT: select a job only when its prerequisites hold")
        if any(s.get("operation") == "plant_crop_workflow" for s in steps):
            raise WorkStateError(PLANT_WORKFLOW_PLAN_ERROR)

    def submit_plan(
        self,
        save_id: str,
        *,
        tasks: list[dict[str, Any]],
        goal_id: str | None = None,
        goal_text: str | None = None,
        replace: bool = False,
        source: str = "agent",
        decision_token: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Model-facing plan entry point: submit or revise a short plan for a goal.

        Either attaches to an existing active ``goal_id`` or resolves/creates a goal
        from ``goal_text`` (model-created goals are always ``source='agent'`` so the
        model can never forge a user instruction). ``replace=True`` cancels the
        goal's still-pending/waiting tasks — work already running, partial or
        unknown is preserved so it is never silently dropped or replayed.
        """
        self.validate_short_job(tasks)
        if not tasks:
            raise WorkStateError("plan must contain at least one task")
        if source not in GOAL_SOURCES:
            raise WorkStateError("goal source must be 'user' or 'agent'")

        created: list[Task] = []
        superseded: list[str] = []
        resolved_goal_id: str = ""
        resolved_goal_source: str = ""
        created_goal = False

        def mutate(state: SaveWorkState) -> None:
            nonlocal resolved_goal_id, resolved_goal_source, created_goal
            self._assert_decision_valid(state, decision_token, allow_append=True)
            queued = next((task for task in state.tasks if task.id == state.decision.get("taskId")), None)
            owning_request_id = request_id or state.decision.get("requestId")
            offered = [state.decision.get("requestId"), *state.decision.get("requestIds", [])]
            if not request_id and len({r for r in offered if r}) > 1:
                raise WorkStateError("REQUEST_ID_REQUIRED: choose the original instruction from this decision's request IDs using submit_plan(request_id=...) or call_capability(request_id=...). No job selected.")
            if request_id and request_id not in offered:
                raise WorkStateError("Request is not part of this decision")
            owning_request = next((r for r in state.requests if r.get("id") == owning_request_id), None)
            if owning_request and owning_request.get("workStopped"):
                raise WorkStateError("WORK_STOPPED: this original request was cancelled by the player; a new player instruction is required before selecting more work for it.")
            scope = state.decision.get("goalScope")
            if scope and goal_id and goal_id != scope:
                raise WorkStateError("GOAL_SCOPE_MISMATCH: autonomous project cannot dispatch unrelated work")
            if scope and not goal_id:
                scoped_goal = self._find_goal(state, scope)
                if scoped_goal.status != "active":
                    raise WorkStateError("Scoped project is not active")
                goal = scoped_goal
            elif goal_id:
                goal = self._find_goal(state, goal_id)
                if goal.status != "active":
                    raise WorkStateError(f"goal '{goal_id}' is not active")
            elif (goal_text or "").strip():
                text = goal_text.strip()
                goal = next(
                    (
                        g
                        for g in state.goals
                        if g.status == "active" and g.text == text
                    ),
                    None,
                )
                if goal is None:
                    # Model-submitted plans hang a stage goal off the active
                    # main goal; the plan never opens a second main goal.
                    parent = self._active_main_goal(state)
                    goal = Goal(id=_new_id("goal"), text=text, source=source,
                                kind="stage",
                                parent_id=parent.id if parent is not None else None)
                    state.goals.append(goal)
                    created_goal = True
            else:
                bound_goal_id = state.decision.get("goalId")
                if not bound_goal_id:
                    raise WorkStateError("submit_plan requires goal_id or goal_text: this decision has no uniquely bound active goal. Use an active goal id from context or provide goal_text; nothing selected.")
                goal = self._find_goal(state, bound_goal_id)
                if goal.status != "active":
                    raise WorkStateError(f"Bound goal '{bound_goal_id}' is no longer active; supply an active goal_id or goal_text. Nothing selected.")

            for spec in tasks:
                for candidate in spec.get("steps", []):
                    operation = candidate.get("operation")
                    params = candidate.get("params") or {}
                    scoped = bind_execution_scope_params(operation, params, goal.constraints)
                    requested = params.get("target_tiles")
                    # An explicit empty request still means no work. A nonempty
                    # request erased by a different saved arrangement is a plan
                    # conflict, not evidence that the crops have disappeared.
                    if (operation in {"water_auto", "harvest_auto"}
                            and (requested and scoped.get("target_tiles") == []
                                 or params.get("location_id") and scoped.get("location_id")
                                 and params["location_id"] != scoped["location_id"])):
                        raise WorkStateError(
                            "EXECUTION_SCOPE_CONFLICT: requested targets do not overlap the accepted "
                            f"arrangement for goal {goal.id} "
                            f"(node={goal.constraints.get('milestoneId')}, revision={goal.epoch}). "
                            "No job selected and no actions dispatched. Revise that existing arrangement "
                            "to the player's latest instructions before submitting its replacement job; "
                            "replace=True only replaces short tasks, not the accepted scope. "
                            "Do not report NO_WORK or continue to shipping."
                        )
                    target = self._branch_target(params)
                    blocked = next((b for b in state.branch_failures if b.get("blocked")
                        and b.get("goalId") == goal.id and b.get("operation") == operation
                        and b.get("target") == target), None)
                    if blocked:
                        raise WorkStateError("BRANCH_BLOCKED: " + str(blocked.get("reasonCode"))
                            + "; " + str(blocked.get("recoveryCondition") or
                                "修复该原因并取得对应对象或路线的新原生可执行证据")
                            + "; choose another branch")

            if queued and (queued.goal_id != goal.id or owning_request_id != state.decision.get("requestId")):
                raise WorkStateError("QUEUED_JOB_SCOPE_MISMATCH: additions must belong to the same goal and player request")
            if replace:
                for existing in state.tasks:
                    if existing.goal_id != goal.id:
                        continue
                    settled_partial = (existing.status == "partial"
                                       and not any(s.status in {"running", "unknown"} for s in existing.steps))
                    if existing.status not in {"pending", "waiting"} and not settled_partial:
                        # Running/unknown commands remain protected. Replacing a
                        # settled partial cancels only its unexecuted remainder;
                        # partial step outcomes/effects stay in the native ledger.
                        continue
                    existing.status = "cancelled"
                    existing.updated_at = _now_iso()
                    for step in existing.steps:
                        if step.status in {"pending", "running", "unknown"}:
                            step.status = "cancelled"
                            step.lease_owner = None
                            step.lease_until = 0.0
                    superseded.append(existing.id)

            appended = self._append_plan(state, goal.id, tasks)
            if queued and not replace:
                steps = [step for task in appended for step in task.steps]
                if {step.id for step in queued.steps} & {step.id for step in steps}:
                    raise WorkStateError("duplicate step id in queued job")
                queued.steps.extend(steps)
                queued.title = "；".join([queued.title, *(task.title for task in appended)])
                queued.updated_at = _now_iso()
                for task in appended:
                    state.tasks.remove(task)
                created.append(queued)
            else:
                created.extend(appended)
            for task in created:
                task.decision_token = decision_token
                if owning_request is not None:
                    if task.id not in owning_request.setdefault("taskIds", []):
                        owning_request["taskIds"].append(task.id)
                    owning_request["goalId"] = goal.id
                    if owning_request.get("origin") == "player-midflight":
                        owning_request["consumedByDelivery"] = True
            state.scheduler_epoch += 1
            state.decision.update(selected=True, taskId=created[0].id, goalId=goal.id,
                                  epoch=state.scheduler_epoch)
            if owning_request is not None:
                state.decision["requestId"] = owning_request_id
            resolved_goal_id = goal.id
            resolved_goal_source = goal.source

        self._mutate(save_id, mutate)
        return {
            "goalId": resolved_goal_id,
            "goalSource": resolved_goal_source,
            "goalCreated": created_goal,
            "supersededTaskIds": superseded,
            "tasks": [asdict(t) for t in created],
        }

    def remember_intent(
        self,
        save_id: str,
        *,
        intent: str,
        kind: str = "goal",
        goal_id: str | None = None,
        priority: int = 0,
        trigger: dict[str, Any] | None = None,
        expiry: dict[str, Any] | None = None,
        source: str = "agent",
    ) -> dict[str, Any]:
        """Model-facing memory entry point: a long-term goal or a future todo.

        Model-created goals are recorded with ``source='agent'``; only the player
        control path may write ``source='user'``.
        """
        text = (intent or "").strip()
        if not text:
            raise WorkStateError("intent cannot be empty")
        if kind not in {"goal", "todo"}:
            raise WorkStateError("kind must be 'goal' or 'todo'")
        if kind == "goal":
            if source not in GOAL_SOURCES:
                raise WorkStateError("goal source must be 'user' or 'agent'")
            # Long-term intents are recorded as a stage of the current main
            # goal (or the first main goal when none is active); they never
            # create a second parallel main goal.
            goal = Goal(id=_new_id("goal"), text=text, source=source,
                        priority=int(priority), kind="stage")

            def mutate_goal(state: SaveWorkState) -> None:
                active_main = self._active_main_goal(state)
                if active_main is None:
                    goal.kind = "main"
                else:
                    goal.parent_id = active_main.id
                state.goals.append(goal)
                state.scheduler_epoch += 1

            self._mutate(save_id, mutate_goal)
            return {"kind": "goal", "goal": asdict(goal)}

        if not trigger:
            raise WorkStateError(
                "kind='todo' requires an explicit trigger object; there is no default "
                'day. Use {"type":"calendar","year":Y,"season":"spring|summer|fall|winter",'
                '"day":1..28}, {"type":"inventory","itemId":"(O)368","minCount":5} or '
                '{"type":"crop","cropId":"(O)24","state":"mature"}. '
                'Call discover_capabilities("memory") for the full schema.'
            )
        self._validate_trigger(trigger)
        todo = Todo(
            id=_new_id("todo"),
            intent=text,
            trigger=dict(trigger or {}),
            goal_id=goal_id,
            expiry=dict(expiry) if expiry else None,
        )

        def mutate_todo(state: SaveWorkState) -> None:
            if goal_id is not None:
                self._find_goal(state, goal_id)
            state.todos.append(todo)

        self._mutate(save_id, mutate_todo)
        return {"kind": "todo", "todo": asdict(todo)}

    def list_tasks(self, save_id: str, goal_id: str | None = None) -> list[dict[str, Any]]:
        state = self.state(save_id)
        tasks = [t for t in state.tasks if goal_id is None or t.goal_id == goal_id]
        return [asdict(t) for t in tasks]

    def blocked_dependencies(self, save_id: str) -> list[dict[str, Any]]:
        """Pending tasks whose dependencies are not confirmed completed.

        ``partial``/``unknown`` dependencies block the dependent task; the harness
        surfaces them so the model can decide instead of silently progressing.
        """
        state = self.state(save_id)
        by_id = {t.id: t for t in state.tasks}
        blocked: list[dict[str, Any]] = []
        for task in state.tasks:
            if task.status not in {"pending", "waiting"} or not task.dependencies:
                continue
            bad = []
            for dep in task.dependencies:
                dependency = by_id.get(dep)
                if dependency is None or dependency.status != "completed":
                    bad.append(
                        {
                            "taskId": dep,
                            "status": dependency.status if dependency else "missing",
                        }
                    )
            if bad:
                blocked.append({"taskId": task.id, "blockedBy": bad})
        return blocked

    def execution_log(self, save_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent harness-recorded executions for this save."""
        entries = self.state(save_id).executions
        return [asdict(e) for e in entries[-limit:]]

    def recent_execution_summary(self, save_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Bounded player-facing projection of the native ledger, oldest first.

        Missing legacy/recovered game dates stay unknown; wall-clock timestamps
        never become invented game dates. A command is shown once after recovery.
        """
        state = self.state(save_id)
        titles = {entry["task"]["id"]: entry["task"].get("title", "农场工作")
                  for entry in state.archive if isinstance(entry.get("task"), dict)}
        titles.update({task.id: task.title for task in state.tasks})
        tasks = {task.id: task for task in state.tasks}
        labels = {"completed": "已完成", "partial": "部分完成", "unknown": "结果待核实",
                  "failed": "未完成", "cancelled": "已取消"}
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        # The panel reports a semantic job, while the ledger retains each native
        # command. Group confirmed commands by job and native date for display.
        commands = {entry.command_id or f"{entry.task_id}:{entry.step_id}": entry for entry in state.executions}
        grouped: dict[tuple[str, str | None], ExecutionEntry] = {}
        severity = {"completed": 0, "cancelled": 1, "failed": 2, "partial": 3, "unknown": 4}
        for entry in commands.values():
            if entry.operation in READ_ONLY_OPERATIONS:
                continue
            key = (entry.task_id, entry.game_date)
            previous = grouped.get(key)
            outcome = entry.outcome if previous is None or severity.get(entry.outcome, 4) >= severity.get(previous.outcome, 4) else previous.outcome
            grouped[key] = replace(entry, effects=(previous.effects if previous else []) + entry.effects, outcome=outcome)
        for entry in reversed(list(grouped.values())):
            if entry.operation in READ_ONLY_OPERATIONS:
                continue
            identity = entry.command_id or f"{entry.task_id}:{entry.step_id}"
            if identity in seen:
                continue
            seen.add(identity)
            task = tasks.get(entry.task_id)
            # A confirmed navigation step cannot complete a job whose later
            # native command remains unconfirmed after restart.
            projected_outcome = "unknown" if task and task.status == "unknown" else entry.outcome
            label = labels.get(projected_outcome, "结果待核实")
            if projected_outcome == "completed" and task and task.status in {"pending", "running", "waiting"}:
                label = "步骤完成，任务继续中"
            summary = label
            if entry.effects:
                summary += "；" + self.effect_summary(entry.effects)
            reason = None
            if entry.reason_code:
                reason = self.reason_summary(entry.reason_code)
                if entry.outcome != "completed":
                    summary += "；" + reason
            rows.append({"commandId": identity, "taskTitle": str(titles.get(entry.task_id) or "任务名称未记录")[:80],
                         "gameDate": entry.game_date, "gameTime": entry.game_time, "recordedAt": entry.at,
                         "outcome": projected_outcome, "summary": summary[:180],
                         "reasonCode": entry.reason_code, "reason": reason})
            if len(rows) >= max(1, min(int(limit), 8)):
                break
        return list(reversed(rows))

    @staticmethod
    def reason_summary(reason_code: str | None, message: str | None = None) -> str:
        """Translate recorded classifications without guessing a missing cause."""
        code = str(reason_code or "").strip()
        known = {
            "OUT_OF_SEEDS": "没有可用种子，剩余地块尚未播种",
            "NO_SEEDS": "没有可用种子，尚未开始播种",
            "OUT_OF_WATER": "水壶没水了，需要补水后继续",
            "NEEDS_WATER_CAN": "还没有可用的水壶",
            "STAMINA_EXHAUSTED": "体力不足",
            "INSUFFICIENT_STAMINA": "体力不足",
            "STEP_PARTIAL": "还有未完成的地块",
            "REMAINING_WORK": "还有后续工作",
            "WATER_INCOMPLETE": "还有作物待浇水",
            "BUDGET_EXCEEDED": "本次作业时间已用完",
            "WAITING_TIMEOUT": "等待条件超过时限",
            "PATH_BLOCKED": "通路受阻，暂时无法到达目标",
            "NO_PATH": "没有找到可到达目标的路线",
            "TARGET_BLOCKED": "目标位置被占用，暂时无法操作",
            "TARGET_NOT_FOUND": "没有找到指定目标",
            "LOCATION_MISMATCH": "当前位置与任务要求不一致",
            "INVENTORY_FULL": "背包已满，需要先腾出空间",
            "INSUFFICIENT_FUNDS": "可用金币不足",
            "SHOP_CLOSED": "商店尚未营业",
            "NATIVE_TERMINAL_UNCONFIRMED": "连接中断，尚未确认实际动作结果",
            "STEP_DISPATCH_REJECTED": "游戏未接受这次动作",
            "NO_WORK": "没有找到当前可执行的目标",
            "CANCELLED": "工作已取消",
            "BEDTIME": "临近就寝，剩余工作留待醒后继续",
        }
        if code in known:
            return known[code]
        detail = str(message or "").strip()
        if code:
            # An unfamiliar exact classification remains visible, not replaced
            # with a made-up explanation or the unhelpful 'reason recorded'.
            return f"原因类别：{code[:100]}（暂未提供详细说明）"
        if detail:
            return detail[:180]
        return "尚未收到具体失败原因，需要核实实际结果"

    @staticmethod
    def effect_summary(effects: list[dict[str, Any]]) -> str:
        """Describe every confirmed native category, without using the job title."""
        excluded = {"skipped", "unknown", "failed", "rejected"}
        navigation = [e for e in effects if isinstance(e, dict) and (
            e.get("state") in {"navigated", "arrived"} or
            (e.get("state") is None and isinstance(e.get("location"), str)
             and type(e.get("pathLength")) is int and e["pathLength"] >= 0))]
        native_sleeps = [e for e in effects if isinstance(e, dict)
                         and e.get("kind") == "day-advanced" and e.get("via") == "native-sleep"
                         and type(e.get("startDay")) is int and type(e.get("endDay")) is int
                         and e["endDay"] == e["startDay"] + 1]
        effects = [e for e in effects if isinstance(e, dict)
                   and e.get("state") is not None and e.get("state") not in excluded]
        if not effects and not navigation and not native_sleeps:
            return "未确认实际变化"
        states = [e.get("state") for e in effects]
        summaries = []
        handled = {"navigated", "arrived"}

        def tiles(kinds: tuple[str, ...], label: str) -> None:
            handled.update(kinds)
            count = sum(states.count(kind) for kind in kinds)
            if count:
                summaries.append(f"{label} {count} 格")

        def items(state: str, label: str, unit: str = "件", default: int = 1) -> None:
            handled.add(state)
            count = sum(int(e.get("stack", e.get("count", default)))
                        for e in effects if e.get("state") == state)
            if count:
                summaries.append(f"{label} {count} {unit}")

        if navigation:
            summaries.append(f"已记录 {len(navigation)} 段行程")
        if native_sleeps:
            summaries.append(f"已确认 {len(native_sleeps)} 次原生睡眠保存并进入新一天")
        handled.add("refilled")
        if "refilled" in states:
            count = states.count("refilled")
            summaries.append("水壶已补满" + (f" {count} 次" if count > 1 else ""))
        handled.add("ate-food")
        count = sum(int(e.get("stack", 1)) for e in effects if e.get("state") == "ate-food")
        if count:
            summaries.append(f"已吃 {count} 份食物；恢复量以原生回执为准")
        tiles(("cleared", "cleared-weeds", "cleared-stone", "cleared-twig", "cleared-dead-crop", "clump-cleared", "cut-grass"), "已清理")
        tiles(("tree-felled",), "已砍倒")
        tiles(("hoed", "tilled"), "已开垦")
        tiles(("planted",), "已种下")
        tiles(("watered",), "已浇水")
        tiles(("fertilized",), "已施肥")
        tiles(("harvested",), "已收获")
        tiles(("placed",), "已放置")
        for state, label in (("withdrawn", "已取出"), ("deposited", "已入箱"), ("shipped", "已出货"),
                             ("inserted", "已投入原料"), ("collected", "已收取"),
                             ("picked-up", "已拾取"), ("crafted", "已制作"),
                             ("removed-and-recovered", "已回收")):
            items(state, label)
        handled.add("purchased")
        purchases = [e for e in effects if e.get("state") == "purchased"]
        count = sum(int(e.get("count", e.get("stack", 0))) for e in purchases)
        cost = sum(int(e.get("subtotal", 0)) for e in purchases)
        if count:
            summaries.append(f"已买入 {count} 件" + (f"，花费 {cost} 金" if cost else ""))
        items("animal-purchased", "已买入", "只动物")
        handled.add("petted")
        if "petted" in states:
            summaries.append(f"已抚摸 {states.count('petted')} 只动物")
        items("fed", "已放入", "份饲料", default=0)
        for state, label in (("building-construction-ordered", "建造已下单，等待施工"),
                             ("building-upgrade-ordered", "扩建已下单，等待施工"),
                             ("building-moved", "建筑已移动"), ("door-opened", "动物门已打开"),
                             ("door-closed", "动物门已关闭")):
            handled.add(state)
            if state in states:
                summaries.append(label)
        other = sum(state not in handled for state in states)
        if other:
            summaries.append(f"记录 {other} 项实际变化")
        return "，".join(summaries) if summaries else f"记录 {len(effects)} 项实际变化"

    # ----------------------------------------------------------- claiming
    @staticmethod
    def _ready_step(
        state: SaveWorkState,
        *,
        now: float,
        now_wall: float | None = None,
        snapshot: dict[str, Any] | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> tuple[Task, Step] | None:
        """The single ready-step predicate shared by claim and readiness peek.

        A task is only schedulable when its goal is active and every dependency is
        ``completed`` — ``partial``/``unknown`` dependencies block the dependent
        task instead of advancing on unconfirmed work. Steps whose lease is still
        held by another worker are not ready.

        Waiting is never "try again on the next snapshot": a waiting step is only
        returned once its own explicit condition is satisfied by the fresh native
        snapshot (or by the bounded transport backoff). Steps in one task run in
        their declared order: an unresolved earlier step blocks later actions.
        """
        if state.paused:
            return None
        wall = now_wall if now_wall is not None else _time.time()
        completed = {t.id for t in state.tasks if t.status == "completed"}
        candidates = sorted(state.tasks, key=lambda t: (t.created_at, t.id))
        for task in candidates:
            d = state.decision
            if d.get("taskId") != task.id or d.get("finished") or d.get("expires", 0) <= _time.time():
                continue
            if task.status in {"completed", "cancelled", "failed", "unknown", "partial", "running"}:
                continue
            goal = next((g for g in state.goals if g.id == task.goal_id), None)
            if goal is None or goal.status != "active":
                continue
            if any(dep not in completed for dep in task.dependencies):
                continue
            for step in task.steps:
                if step.status == "completed":
                    continue
                # Gate on the step's own explicit condition whether the task is
                # already parked (waiting) or still pending: a freshly submitted
                # step with a declared condition must never dispatch before the
                # condition is observable. Waiting is never "retry next snapshot".
                if step.status not in {"pending", "waiting"}:
                    return None
                if step.wait is not None:
                    if step.wait.type == "transportRetry":
                        # Keep the persisted condition in step with the real
                        # attempt counter the executor uses for its bound.
                        step.wait.attempts_used = max(step.wait.attempts_used, step.attempts)
                    if not WorkStore._wait_satisfied(
                        step.wait, snapshot=snapshot, game_date=game_date, now=wall
                    ):
                        return None
                if step.lease_owner and step.lease_until > now:
                    return None
                return task, step
        return None

    @staticmethod
    def _wait_satisfied(
        condition: StepWaitCondition | None,
        *,
        snapshot: dict[str, Any] | None,
        game_date: dict[str, Any] | None,
        now: float,
    ) -> bool:
        """Evaluate one explicit native waiting condition against fresh state.

        ``now`` is wall-clock epoch seconds (matching ``created_at``), used only for
        the bounded transport backoff; everything else is native state.
        """
        if condition is None:
            # No explicit condition = no supported native signal: stay parked
            # rather than re-attempting on every snapshot.
            return False
        kind = condition.type
        params = condition.params or {}
        if kind == "transportRetry":
            if condition.attempts_used >= max(1, condition.attempts_allowed):
                return False
            # A bounded exponential backoff: the window must actually elapse, so a
            # transient transport fault cannot turn into a spin.
            backoff = params.get("backoffSeconds")
            backoff = float(backoff) if isinstance(backoff, (int, float)) and backoff >= 0 else 30.0
            due = condition.retry_after if condition.retry_after > 0 else _parse_iso_epoch(
                condition.created_at
            ) + backoff
            return now >= due
        if kind == "gameDay":
            current = _calendar_key(
                (game_date or {}).get("year"),
                (game_date or {}).get("season"),
                (game_date or {}).get("day"),
            )
            trigger = _calendar_key(params.get("year"), params.get("season"), params.get("day"))
            return bool(current and trigger and current >= trigger)
        payload = (snapshot or {}).get("payload")
        if not isinstance(payload, dict):
            payload = snapshot if isinstance(snapshot, dict) else {}
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        if kind == "inventory":
            inventory = payload.get("inventory") if isinstance(payload.get("inventory"), dict) else {}
            slots = inventory.get("slots") if isinstance(inventory.get("slots"), list) else []
            wanted = str(params.get("itemId"))
            minimum = params.get("minCount", 1)
            minimum = minimum if isinstance(minimum, int) else 1
            count = sum(
                int(slot.get("stack", slot.get("count", 0)) or 0)
                for slot in slots
                if isinstance(slot, dict)
                and str(slot.get("itemId") or slot.get("name")) == wanted
            )
            return count >= minimum
        if kind == "cropState":
            farm = payload.get("farmWork") if isinstance(payload.get("farmWork"), dict) else {}
            wanted = str(params.get("cropId"))
            state_name = params.get("state", "mature")
            mature = farm.get("matureCrops") if isinstance(farm.get("matureCrops"), list) else []
            if state_name in {"mature", "harvestable"}:
                return any(
                    str(crop.get("cropId")) == wanted for crop in mature if isinstance(crop, dict)
                )
            return False
        if kind == "shopOpen":
            shop = payload.get("shop") if isinstance(payload.get("shop"), dict) else {}
            wanted = params.get("shopId")
            if wanted is not None and str(shop.get("shopId")) != str(wanted):
                return False
            return shop.get("isOpen") is True
        if kind == "tileClear":
            player = world.get("player") if isinstance(world.get("player"), dict) else {}
            players = world.get("players") if isinstance(world.get("players"), list) else []
            targets = [player, *[p for p in players if isinstance(p, dict)]]
            for occupant in targets:
                if not occupant:
                    continue
                tile = occupant.get("tile") if isinstance(occupant.get("tile"), dict) else occupant
                x = occupant.get("tileX", tile.get("x") if isinstance(tile, dict) else None)
                y = occupant.get("tileY", tile.get("y") if isinstance(tile, dict) else None)
                if x == params.get("x") and y == params.get("y"):
                    return False
            return True
        if kind == "obstacleChange":
            marker = params.get("marker")
            if marker:
                obstacles = world.get("obstacles") if isinstance(world.get("obstacles"), list) else []
                return not any(
                    isinstance(obs, dict) and str(obs.get("marker")) == str(marker)
                    for obs in obstacles
                )
            # No named marker: only a genuinely newer native revision counts.
            revision = (snapshot or {}).get("worldRevision")
            since = params.get("sinceRevision")
            return bool(
                isinstance(revision, int) and isinstance(since, int) and revision > since
            )
        return False

    def has_ready_step(
        self,
        save_id: str,
        *,
        now: float | None = None,
        snapshot: dict[str, Any] | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> bool:
        """Read-only readiness check; never mutates or leases anything."""
        current = now if now is not None else _time.monotonic()
        try:
            state = self.state(save_id)
        except WorkStateError:
            return False
        return (
            self._ready_step(state, now=current, snapshot=snapshot, game_date=game_date) is not None
        )

    def claim_next_step(
        self,
        save_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 120.0,
        now: float | None = None,
        snapshot: dict[str, Any] | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Claim the next ready step under the lock; returns None when idle."""
        if not worker_id:
            raise WorkStateError("worker_id is required")
        self.clean_stale_waiting_tasks(save_id)
        current = now if now is not None else _time.monotonic()
        claimed: dict[str, Any] | None = None

        def mutate(state: SaveWorkState) -> None:
            nonlocal claimed
            ready = self._ready_step(
                state, now=current, snapshot=snapshot, game_date=game_date
            )
            if ready is None:
                return
            task, step = ready
            previous_command_id = step.command_id
            was_waiting = step.status == "waiting"
            if was_waiting and step.wait is not None:
                step.wait.attempts_used += 1
            step.status = "running"
            step.lease_owner = worker_id
            step.lease_until = current + lease_seconds
            step.attempts += 1
            task.status = "running"
            task.updated_at = _now_iso()
            claimed = {
                "taskId": task.id,
                "goalId": task.goal_id,
                "stepId": step.id,
                "operation": step.operation,
                "params": dict(step.params),
                "feedback": dict(step.feedback),
                "attempt": step.attempts,
                "leaseUntil": step.lease_until,
                "releasedWait": was_waiting,
                "waitType": step.wait.type if step.wait else None,
                "waitReasonCode": step.wait.reason_code if step.wait else None,
                # Persisted id from an earlier attempt; the executor reconciles it
                # against the native result before dispatching anything new.
                "previousCommandId": previous_command_id,
            }

        self._mutate(save_id, mutate)
        return claimed

    def assign_command_id(
        self, save_id: str, task_id: str, step_id: str, command_id: str
    ) -> None:
        """Persist the stable command id before dispatch."""
        if not command_id:
            raise WorkStateError("command_id is required")

        def mutate(state: SaveWorkState) -> None:
            task = self._find_task(state, task_id)
            step = self._find_step(task, step_id)
            if step.status not in {"running", "unknown"}:
                raise WorkStateError(f"step '{step_id}' is not running")
            step.command_id = command_id

        self._mutate(save_id, mutate)

    def release_unstarted_claim(
        self, save_id: str, task_id: str, step_id: str,
        worker_id: str, command_id: str,
    ) -> None:
        """Return a claimed step to pending when no native dispatch was sent."""
        def mutate(state: SaveWorkState) -> None:
            task = self._find_task(state, task_id)
            step = self._find_step(task, step_id)
            if (step.status != "running" or step.lease_owner != worker_id
                    or step.command_id != command_id):
                return
            step.status = "pending"
            step.command_id = None
            step.lease_owner = None
            step.lease_until = 0.0
            step.attempts = max(0, step.attempts - 1)
            task.status = "pending"
            task.updated_at = _now_iso()
        self._mutate(save_id, mutate)

    def record_step_feedback(
        self, save_id: str, task_id: str, step_id: str, command_id: str,
        feedback: dict[str, Any],
    ) -> None:
        """Keep observed operation scope while its native command is still running."""
        def mutate(state: SaveWorkState) -> None:
            step = self._find_step(self._find_task(state, task_id), step_id)
            if step.command_id != command_id:
                raise WorkStateError("Feedback command id does not match the persisted step command")
            if step.status == "running":
                step.feedback = dict(feedback)
        self._mutate(save_id, mutate)

    @staticmethod
    def _step_location(operation: str, params: dict[str, Any]) -> str | None:
        """Normalize verified dispatch defaults, not coordinate-based guesses.

        Legacy package-r6 execute_plant_seeds defaults location_id to Farm;
        execute_water_zone always dispatches execute_tiles on Farm. water_auto
        instead uses its observed companion map, so has no inferred default here.
        """
        for name in ("location_id", "locationId"):
            if name in params:
                value = params[name]
                return value if isinstance(value, str) and value else None
        return "Farm" if operation in {"plant_seeds", "water_zone", "water_tiles"} else None

    @staticmethod
    def _execution_location(state: SaveWorkState, task: Task, step: Step,
                            outcome: str, effects: list[dict[str, Any]],
                            game_day: str | None = None) -> str | None:
        """Retain native map provenance before bounded task archives are pruned."""
        if outcome == "unknown":
            return None
        maps = {e["locationId"] for e in effects if isinstance(e, dict)
                and isinstance(e.get("locationId"), str) and e["locationId"]}
        if len(maps) == 1:
            return next(iter(maps))
        if len(maps) > 1:
            return None  # Each effect keeps its own map; no shared entry map.
        if step.operation == "navigate_to":
            location = effects[-1].get("location") if outcome == "completed" and effects else None
            return location if isinstance(location, str) and location else None
        location = WorkStore._step_location(step.operation, step.params)
        if isinstance(location, str) and location:
            return location
        for previous in reversed(state.executions):
            if game_day and previous.game_date != game_day:
                break
            if previous.operation == "navigate_to" and previous.outcome != "completed":
                break
            if previous.operation == "navigate_to" and previous.task_id != task.id:
                break  # A newer task's navigation invalidates older task-map evidence.
            if previous.task_id != task.id:
                continue
            if previous.outcome == "unknown":
                return None
            if previous.operation == "navigate_to":
                if previous.outcome != "completed" or not previous.effects:
                    return None
                location = previous.effects[-1].get("location")
                return location if isinstance(location, str) and location else previous.location_id
            if (previous.outcome == "completed" and previous.location_id
                    and any(isinstance(e, dict) and e.get("state") not in {None, "skipped"}
                            for e in previous.effects)):
                return previous.location_id
        # Legacy mapless protocols may use the latest actual actor navigation,
        # but only on the same explicitly recorded native day, never a default map.
        if game_day:
            for previous in reversed(state.executions):
                if previous.game_date != game_day:
                    break
                if previous.operation != "navigate_to":
                    continue
                if previous.outcome != "completed" or not previous.effects:
                    break
                location = previous.effects[-1].get("location")
                return location if isinstance(location, str) and location else previous.location_id
        return None

    def commit_step_result(
        self,
        save_id: str,
        *,
        task_id: str,
        step_id: str,
        outcome: str,
        effects: list[dict[str, Any]] | None = None,
        feedback: dict[str, Any] | None = None,
        reason_code: str | None = None,
        snapshot_revision: int | None = None,
        command_id: str | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Harness-only completion commit; requires a matching executed command id."""
        if outcome not in OUTCOMES:
            raise WorkStateError(f"invalid outcome '{outcome}'")
        if outcome == "completed" and not command_id:
            raise WorkStateError("cannot mark a step completed without an executed command id")

        def mutate(state: SaveWorkState) -> dict[str, Any]:
            task = self._find_task(state, task_id)
            step = self._find_step(task, step_id)
            revoked = task.status == "cancelled"
            # The scope-retirement path and the original executor can receive
            # the same terminal concurrently. Keep its effects exactly once.
            if (revoked and command_id and command_id == step.command_id
                    and step.status in TERMINAL_STEP_STATUSES
                    and step.outcome == outcome and step.reason_code == reason_code
                    and step.effects == list(effects or [])):
                if feedback:
                    step.feedback = dict(feedback)
                return {"taskId": task.id, "stepId": step.id,
                        "taskStatus": task.status, "outcome": step.outcome}
            if step.status not in {"running", "unknown"} and not (revoked and step.status == "waiting" and step.command_id):
                raise WorkStateError(f"step '{step_id}' is not awaiting a result")
            if outcome == "completed" and not step.command_id:
                raise WorkStateError(
                    "cannot mark a step completed before its command id is persisted"
                )
            if command_id and step.command_id and command_id != step.command_id:
                raise WorkStateError("Result command id does not match the persisted step command")
            if command_id and not step.command_id:
                step.command_id = command_id

            step.status = outcome
            step.outcome = outcome
            step.reason_code = reason_code
            step.effects = list(effects or [])
            step.feedback = dict(feedback or {})
            step.lease_owner = None
            step.lease_until = 0.0
            request = next((r for r in state.requests if task.id in r.get("taskIds", [])), None)
            state.executions.append(
                ExecutionEntry(
                    command_id=command_id or step.command_id or "",
                    task_id=task_id,
                    step_id=step_id,
                    operation=step.operation,
                    outcome=outcome,
                    request_id=request.get("id") if request else None,
                    goal_id=task.goal_id,
                    effects=list(effects or []),
                    reason_code=reason_code,
                    snapshot_revision=snapshot_revision,
                    game_date=(_day_key(game_date.get("year"), game_date.get("season"), game_date.get("day"))
                               if isinstance(game_date, dict) else None),
                    game_time=game_date.get("timeOfDay") if isinstance(game_date, dict) else None,
                    location_id=self._execution_location(
                        state, task, step, outcome, effects or [],
                        _day_key(game_date.get("year"), game_date.get("season"), game_date.get("day"))
                        if isinstance(game_date, dict) else None),
                )
            )
            self._record_branch_result(state, task, step, outcome, reason_code,
                                       command_id or step.command_id or "")

            if revoked or outcome == "cancelled":
                self._cancel_task_steps(task)
            elif outcome == "completed":
                remaining = [s for s in task.steps if s.status not in TERMINAL_STEP_STATUSES]
                if not remaining:
                    task.status = "completed"
                else:
                    task.status = "pending"
            elif outcome == "partial":
                task.status = "partial"
            elif outcome in {"failed", "rejected"}:
                task.status = "failed"
            else:
                task.status = "unknown"
            task.updated_at = _now_iso()
            state.scheduler_epoch += 1
            if task.status != "pending" or outcome != "completed":
                self._queue_task_result(state, task, step, outcome=outcome)
            return {
                "taskId": task.id,
                "stepId": step.id,
                "outcome": outcome,
                "taskStatus": task.status,
            }

        return self._mutate(save_id, mutate)

    @staticmethod
    def _queue_task_result(state: SaveWorkState, task: Task, step: Step,
                           *, outcome: str | None = None) -> None:
        """Commit the delivery obligation with the native outcome, before callbacks."""
        request = next((r for r in state.requests if task.id in r.get("taskIds", [])), None)
        if not step.command_id:
            return
        # job_feedback imports WorkStore for its shared effect summary; import
        # here after both modules have initialized, rather than create a cycle.
        from stardew_ai_runtime.job_feedback import compact_task_feedback

        outcome = outcome or step.outcome or step.status
        feedback = compact_task_feedback(task, {
            **step.feedback, "commandId": step.command_id,
            "effects": step.effects, "reasonCode": step.reason_code,
        }, operation=step.operation, status=outcome)
        # The latest native result belongs to this terminal transaction, even
        # when cancellation has already revoked the selected decision.
        state.last_job = {**feedback, "taskId": task.id, "taskTitle": task.title,
                          "goalId": task.goal_id, "decisionId": state.decision.get("token")}
        if request is None or request.get("origin") not in {"player", "player-midflight"}:
            return
        entry = next((e for e in reversed(state.executions)
                      if e.command_id == step.command_id and e.task_id == task.id), None)
        _append_outbox_event(state, {
            "kind": "terminal", "commandId": step.command_id,
            "taskId": task.id, "goalId": task.goal_id, "operation": step.operation,
            "outcome": outcome, "reasonCode": step.reason_code,
            "effects": list(step.effects), "feedback": feedback,
            "snapshotRevision": entry.snapshot_revision if entry else None,
            "gameDate": entry.game_date if entry else state.last_settled_day,
            "requestId": request["id"],
        })

    @staticmethod
    def _branch_target(params: dict[str, Any]) -> dict[str, Any]:
        aliases = {"locationId": "location_id", "shopId": "shop_id", "itemId": "item_id", "npcName": "npc_name"}
        normalized = {aliases.get(k, k): v for k, v in params.items()}
        if "machine_x" in params and "machine_y" in params:
            normalized["tile"] = {"x": params["machine_x"], "y": params["machine_y"]}
        if isinstance(normalized.get("tiles"), list) and len(normalized["tiles"]) == 1:
            normalized["tile"] = normalized.pop("tiles")[0]
        return {k: normalized[k] for k in ("location_id", "shop_id", "tile", "tiles",
                                          "item_id", "seed_item_id", "animal_id", "animal_name",
                                          "building_name", "chest_x", "chest_y",
                                          "center_x", "center_y", "radius", "landmark", "npc_name", "facing", "choice_index")
                if k in normalized}

    def _record_branch_result(self, state, task, step, outcome, reason, command_id):
        if outcome == "partial" and reason and not any(
            e.get("state") not in {None, "skipped", "unknown", "failed", "rejected"}
            for e in step.effects if isinstance(e, dict)):
            outcome = "failed"  # Known terminal with no productive effects.
        if outcome not in {"completed", "failed", "rejected"} or not command_id:
            return  # Unknown/partial outcomes require reconciliation, not retry counting.
        target = self._branch_target(step.params)
        same_target = [b for b in state.branch_failures if b.get("goalId") == task.goal_id
                       and b.get("operation") == step.operation and b.get("target") == target]
        if outcome == "completed":
            state.branch_failures = [b for b in state.branch_failures if b not in same_target]
            if step.operation == "query_route":
                facts = ((step.feedback or {}).get("observation") or {}).get("facts") or {}
                if isinstance(facts, dict):
                    route_target = {"location_id": step.params.get("location_id"),
                                    "tile": facts.get("resolvedDestinationTile") or step.params.get("tile")}
                    self._recover_navigation_branches(state, "navigate_to", route_target, facts)
            if step.operation == "observe_player_activities":
                facts = ((step.feedback or {}).get("observation") or {}).get("facts") or {}
                activities = facts.get("activities") or (facts.get("details") or {}).get("activities")
                if isinstance(activities, dict):
                    self._recover_native_interaction_branches(state, activities, command_id)
            for b in state.branch_failures:
                cause = str(b.get("reasonCode") or "").lower()
                if any(code in cause for code in ("unknown", "unconfirmed", "in_progress")):
                    continue
                if ((step.operation == "eat_food" and any(s in cause for s in ("stamina", "exhaust", "energy")))
                        or (step.operation == "refill_watering_can" and "water" in cause)):
                    b.update(blocked=False, failures=0, recoveryEvidence=command_id)
            return
        # One native command remains one attempt even if reconciliation later
        # changes its reason bucket. Executions persist beyond the three-item
        # per-branch display window and survive process restarts.
        def counted(entry):
            return entry.outcome in {"completed", "failed", "rejected"} or (
                entry.outcome == "partial" and entry.reason_code and not any(
                    e.get("state") not in {None, "skipped", "unknown", "failed", "rejected"}
                    for e in entry.effects if isinstance(e, dict)))
        if any(command_id in b.get("commands", []) for b in state.branch_failures) or any(
                entry.command_id == command_id and counted(entry)
                for entry in state.executions[:-1]):
            return
        branch = next((b for b in same_target if b.get("reasonCode") == reason), None)
        if branch is None:
            branch = {"goalId": task.goal_id, "operation": step.operation, "target": target,
                      "reasonCode": reason, "failures": 0, "commands": [], "attempts": [],
                      "recoveryCondition": ("原生碰撞不是路线估计可达的证明：需观察阻碍变化或实际原生通行；不得仅重复 query_route 后重试同一段"
                                            if step.operation == "navigate_to" and str(reason).lower() == "native_collision" else
                                            "对同一 location_id 和 tile 调用 query_route；新原生结果 reachable=true 后可重新导航（包括商店营业后）"
                                            if step.operation == "navigate_to" else
                                            "修复该原因并取得对应对象的新原生可执行证据")}
            state.branch_failures.append(branch)
        if command_id in branch["commands"]:
            return
        branch["commands"].append(command_id)
        branch["attempts"].append({"commandId": command_id, "params": step.params, "reasonCode": reason})
        branch["commands"] = branch["commands"][-3:]
        branch["attempts"] = branch["attempts"][-3:]
        branch["failures"] += 1
        branch["blocked"] = branch["failures"] >= 3
        branch["updatedAt"] = _now_iso()

    def observe_branch_recovery(self, save_id: str, *, operation: str,
                                target: dict[str, Any], evidence: dict[str, Any]) -> None:
        """A fresh native route proof for this exact target, never an unknown receipt."""
        self._mutate(save_id, lambda state: self._recover_navigation_branches(state, operation, target, evidence))

    def _recover_navigation_branches(self, state, operation, target, evidence) -> None:
        if operation != "navigate_to" or evidence.get("reachable") is not True:
            return
        for branch in state.branch_failures:
            reason = str(branch.get("reasonCode") or "").lower()
            # A geometry preview can still cross a native event/NPC/animated
            # barrier. It is not evidence that an observed walking collision
            # disappeared; a successful native navigation clears this branch
            # through _record_branch_result instead.
            if reason == "native_collision" and not (evidence.get("nativeCollisionChecked") is True
                    and evidence.get("nativeUiClear") is True
                    and evidence.get("collisionEvidenceScope")=="current-location-native-pathfinding-no-effects"):
                continue
            geometry = evidence.get("nativeMovementGeometry")
            repaired_budget = (reason == "budget_exceeded" and geometry == "standing-center-v2"
                               and branch.get("budgetRecoveryGeometry") != geometry)
            if (branch.get("operation") == operation
                    and branch.get("target") == self._branch_target(target)
                    and not any(code in reason for code in ("unknown", "unconfirmed", "in_progress"))
                    and (repaired_budget or reason == "target_unavailable"
                         or (reason == "native_ui_active" and evidence.get("nativeUiClear") is True)
                         or any(code in reason for code in ("path", "route", "unreachable", "obstacle", "collision", "entrance", "warp")))):
                branch.update(blocked=False, failures=0, recoveryEvidence=evidence)
                if repaired_budget:
                    branch["budgetRecoveryGeometry"] = geometry

    @staticmethod
    def _recover_native_interaction_branches(state, activities: dict[str, Any], command_id: str | None = None) -> None:
        """Fresh exact adjacency repairs NOT_ADJACENT only, never other native failures."""
        location = activities.get("locationId")
        player = activities.get("playerTile")
        revision = activities.get("worldRevision")
        if not isinstance(location, str) or not isinstance(player, dict) or type(revision) is not int:
            return
        if any(type(player.get(k)) is not int for k in ("x", "y")):
            return
        def adjacent(tile):
            return (isinstance(tile, dict) and all(type(tile.get(k)) is int for k in ("x", "y"))
                    and abs(tile["x"] - player["x"]) + abs(tile["y"] - player["y"]) <= 1)
        for branch in state.branch_failures:
            target = branch.get("target") or {}
            if str(branch.get("reasonCode") or "").upper() != "NOT_ADJACENT" or target.get("location_id") != location:
                continue
            failed_revisions = [entry.snapshot_revision for entry in state.executions
                if entry.command_id in branch.get("commands", []) and type(entry.snapshot_revision) is int]
            if failed_revisions and revision < max(failed_revisions):
                continue
            operation = branch.get("operation")
            ready = False
            if operation == "talk_npc":
                name = target.get("npc_name")
                if not name:
                    # Narrow old name-less records only when every saved attempt identifies one NPC.
                    names = {(a.get("params") or {}).get("npc_name") for a in branch.get("attempts", [])}
                    if len(names) == 1 and None not in names:
                        name = next(iter(names))
                        branch["target"] = {**target, "npc_name": name}
                ready = bool(name) and any(isinstance(n, dict) and n.get("name") == name
                    and n.get("canInitiateConversation") is True and adjacent(n.get("tile"))
                    for n in activities.get("npcs", []))
            elif operation == "interact_tile":
                ready = any(isinstance(a, dict) and a.get("tile") == target.get("tile")
                    and a.get("canInteractNow") is True and adjacent(a.get("tile"))
                    for a in activities.get("interactions", []) + [f for f in activities.get("fixtures", [])
                        if isinstance(f, dict) and f.get("operation") == "interact_tile"])
            if ready:
                branch.update(blocked=False, failures=0, recoveryEvidence={"commandId": command_id,
                    "worldRevision": revision, "locationId": location, "playerTile": player,
                    "basis": "fresh-native-current-interaction-adjacency"})

    def observe_interaction_recovery(self, save_id: str, activities: dict[str, Any]) -> None:
        self._mutate(save_id, lambda state: self._recover_native_interaction_branches(state, activities))

    def observe_native_ui_recovery(self, save_id: str, native: dict[str, Any], *,
                                   world_revision: int, game_session_id: str) -> None:
        """A live native snapshot can retire the specific festival advance guard.

        Called only from the connected snapshot reader, never from stored prompt
        context. Ending a festival is not proof that other UI failures recovered.
        Failed command identities/receipts and provider retry holds are retained.
        """
        if (not isinstance(native, dict) or type(world_revision) is not int or not game_session_id):
            return
        festival_ended = native.get("eventUp") is False and native.get("eventIsFestival") is False
        dialogue_ready = (native.get("activeMenu") == "DialogueBox"
                          and native.get("dialogueUp") is True and native.get("isQuestionDialogue") is False)
        if not festival_ended and not dialogue_ready:
            return

        def applicable(branch):
            return (branch.get("operation") == "native_advance"
                    and branch.get("reasonCode") == "FESTIVAL_FREE_ROAM"
                    and (branch.get("blocked") or branch.get("failures", 0) > 0))

        if not any(applicable(branch) for branch in self.state(save_id).branch_failures):
            return

        def mutate(state):
            for branch in state.branch_failures:
                if applicable(branch):
                    evidence = {"basis": "live-native-festival-ended" if festival_ended else "live-native-dialogue-can-advance",
                                "gameSessionId": game_session_id, "worldRevision": world_revision,
                                "eventUp": native.get("eventUp"), "eventIsFestival": native.get("eventIsFestival")}
                    if dialogue_ready and not festival_ended:
                        evidence.update(activeMenu="DialogueBox", dialogueUp=True, isQuestionDialogue=False)
                    branch.update(blocked=False, failures=0, recoveryEvidence=evidence)

        self._mutate(save_id, mutate)

    def observe_machine_recovery(self, save_id: str, location_id: str, machines: list[dict]) -> None:
        def mutate(state):
            for branch in state.branch_failures:
                reason = str(branch.get("reasonCode") or "").lower()
                if any(code in reason for code in ("unknown", "unconfirmed", "in_progress")):
                    continue
                if branch.get("operation") != "collect_machine" or not any(s in reason for s in ("ready", "output", "processing")):
                    continue
                target = branch.get("target") or {}
                if target.get("location_id", "Farm") != location_id:
                    continue
                tiles = target.get("tiles") or ([target["tile"]] if "tile" in target else [])
                if tiles and all(any(m.get("tile") == tile and m.get("isReady") is True for m in machines) for tile in tiles):
                    branch.update(blocked=False, failures=0, recoveryEvidence={"machinesReady": tiles})
        self._mutate(save_id, mutate)

    def recover(self, save_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
        """Resolve leased steps after a restart without replaying completed work."""
        self.clean_stale_waiting_tasks(save_id)
        current = now if now is not None else _time.monotonic()
        decisions: list[dict[str, Any]] = []

        def mutate(state: SaveWorkState) -> None:
            logged = {entry.command_id: entry for entry in state.executions if entry.command_id}
            for task in state.tasks:
                for step in task.steps:
                    legacy_cancelled_command = (step.status == "cancelled" and step.command_id
                                                and step.outcome not in TERMINAL_STEP_STATUSES)
                    if not legacy_cancelled_command and (step.status != "running" or step.lease_until > current):
                        continue
                    entry = logged.get(step.command_id or "")
                    if entry is not None:
                        step.status = entry.outcome if entry.outcome in STEP_STATUSES else "unknown"
                        step.outcome = entry.outcome
                        step.reason_code = entry.reason_code
                        step.effects = list(entry.effects)
                        step.lease_owner = None
                        step.lease_until = 0.0
                        continue
                    # Cannot confirm: keep unknown, request a decision once, never re-charge.
                    step.status = "unknown"
                    step.reason_code = "UNCONFIRMED_AFTER_RESTART"
                    step.lease_owner = None
                    step.lease_until = 0.0
                    if task.status == "running":
                        task.status = "unknown"
                    if not step.decision_requested:
                        step.decision_requested = True
                        decisions.append(
                            {
                                "taskId": task.id,
                                "stepId": step.id,
                                "operation": step.operation,
                                "commandId": step.command_id,
                                "reasonCode": "UNCONFIRMED_AFTER_RESTART",
                            }
                        )
                    self._queue_task_result(state, task, step, outcome="unknown")

            # A process may have stopped after committing the native outcome,
            # before releasing its client and publishing finish_job. Its durable
            # fact is enough to settle that decision on the next owned boundary.
            selected = next((t for t in state.tasks if t.id == state.decision.get("taskId")), None)
            event = next((e for e in reversed(state.outbox)
                          if selected is not None and e.get("taskId") == selected.id), None)
            if (selected is not None and event is not None
                    and not any(s.status == "running" for s in selected.steps)
                    and not state.decision.get("finished")):
                state.last_job = {**event.get("feedback", {}), "taskId": selected.id,
                                  "taskTitle": selected.title, "goalId": selected.goal_id,
                                  "decisionId": state.decision.get("token")}
                state.decision["finished"] = True

        self._mutate(save_id, mutate)
        return decisions

    # -------------------------------------------------------------- waiting
    def mark_task_waiting(
        self,
        save_id: str,
        task_id: str,
        *,
        condition: dict[str, Any] | StepWaitCondition | None = None,
        step_id: str | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        """Harness-only: park a task behind one explicit native condition.

        An unconditional "wait and retry on the next snapshot" is intentionally
        rejected: without a supported condition the correct behaviour is to keep
        waiting for a user/model revision, not to re-attempt forever. This never
        marks work successful; it only releases a running lease and records the
        condition that must be observed before the step is claimable again.
        """
        wait = self._coerce_wait_condition(condition, reason_code=reason_code)

        def mutate(state: SaveWorkState) -> dict[str, Any]:
            task = self._find_task(state, task_id)
            task.status = "waiting"
            task.updated_at = _now_iso()
            parked: list[str] = []
            for step in task.steps:
                if step_id is not None and step.id != step_id:
                    continue
                if step.status in {"running", "waiting", "pending"}:
                    step.status = "waiting"
                    step.wait = wait
                    step.lease_owner = None
                    step.lease_until = 0.0
                    step.reason_code = reason_code or step.reason_code
                    parked.append(step.id)
            if not parked:
                raise WorkStateError(f"task '{task_id}' has no step to park")
            state.scheduler_epoch += 1
            if wait.type != "transportRetry":
                for step in task.steps:
                    if step.id in parked:
                        self._queue_task_result(state, task, step, outcome="waiting")
            return {
                "taskId": task.id,
                "status": task.status,
                "reasonCode": reason_code,
                "waitingStepIds": parked,
                "waitCondition": asdict(wait),
                "waitDescription": describe_wait_condition(wait),
            }

        return self._mutate(save_id, mutate)

    @staticmethod
    def _coerce_wait_condition(
        condition: dict[str, Any] | StepWaitCondition | None,
        *,
        reason_code: str | None,
    ) -> StepWaitCondition:
        if isinstance(condition, StepWaitCondition):
            wait = condition
        elif isinstance(condition, dict):
            wait = _step_wait_from_dict(condition)
            if wait is None:
                raise WorkStateError(
                    "unsupported waiting condition; expected one of "
                    + ", ".join(sorted(WAIT_CONDITION_TYPES))
                )
            allowed = condition.get("attemptsAllowed")
            if isinstance(allowed, (int, float)) and int(allowed) > 0:
                wait.attempts_allowed = int(allowed)
        else:
            raise WorkStateError(
                "an explicit waiting condition is required (no unconditional retry)"
            )
        if reason_code and not wait.reason_code:
            wait.reason_code = reason_code
        return wait

    def wait_conditions(self, save_id: str) -> list[dict[str, Any]]:
        """Waiting steps and their explicit conditions, for injected context/F8."""
        state = self.state(save_id)
        rows: list[dict[str, Any]] = []
        for task in state.tasks:
            if task.status != "waiting":
                continue
            for step in task.steps:
                if step.status != "waiting":
                    continue
                rows.append(
                    {
                        "taskId": task.id,
                        "taskTitle": task.title,
                        "stepId": step.id,
                        "operation": step.operation,
                        "waitCondition": asdict(step.wait) if step.wait else None,
                        "waitDescription": describe_wait_condition(step.wait),
                        "reasonCode": step.reason_code,
                    }
                )
        return rows

    # -------------------------------------------------------------- todos
    def add_todo(
        self,
        save_id: str,
        *,
        intent: str,
        trigger: dict[str, Any],
        goal_id: str | None = None,
        expiry: dict[str, Any] | None = None,
    ) -> Todo:
        if not (intent or "").strip():
            raise WorkStateError("todo intent cannot be empty")
        self._validate_trigger(trigger)
        todo = Todo(
            id=_new_id("todo"),
            intent=intent.strip(),
            trigger=dict(trigger),
            goal_id=goal_id,
            expiry=dict(expiry) if expiry else None,
        )

        def mutate(state: SaveWorkState) -> None:
            if goal_id is not None:
                self._find_goal(state, goal_id)
            state.todos.append(todo)

        self._mutate(save_id, mutate)
        return todo

    @staticmethod
    def _validate_trigger(trigger: dict[str, Any]) -> None:
        if not isinstance(trigger, dict):
            raise WorkStateError("trigger must be an object")

        allowed = (
            'allowed trigger types: "calendar" {"type","year","season","day"}, '
            '"inventory" {"type","itemId","minCount"}, '
            '"crop" {"type","cropId","state"}. '
            '"gameTime" {"type","year","season","day","timeOfDay"}, '
            '"resource" {"type","resource":"stamina"|"water","minAmount"}, '
            '"machineReady" {"type","locationId","tile":{"x","y"}}. '
            'Examples: {"type":"calendar","year":2,"season":"spring","day":3}; '
            '{"type":"inventory","itemId":"(O)368","minCount":5}; '
            '{"type":"crop","cropId":"(O)24","state":"mature"}. '
            '"daily"/"date"/"next_day" are not supported trigger types.'
        )

        kind = trigger.get("type")
        if kind in {"calendar", "gameDay", "gameTime"}:
            if _calendar_key(trigger.get("year"), trigger.get("season"), trigger.get("day")) is None:
                raise WorkStateError(
                    "calendar trigger requires year (integer), season "
                    "(spring|summer|fall|winter) and day (1..28). " + allowed
                )
            if kind == "gameTime":
                time_of_day = trigger.get("timeOfDay")
                if type(time_of_day) is not int or not 600 <= time_of_day <= 2600 or time_of_day % 100 >= 60:
                    raise WorkStateError("gameTime requires native timeOfDay HHMM (600..2600). " + allowed)
        elif kind == "resource":
            amount = trigger.get("minAmount")
            if trigger.get("resource") not in {"stamina", "water"} or type(amount) not in {int, float} or not 0 < amount <= 10000:
                raise WorkStateError("resource requires stamina|water and positive minAmount. " + allowed)
        elif kind == "machineReady":
            tile = trigger.get("tile")
            if (not isinstance(trigger.get("locationId"), str) or not trigger["locationId"].strip()
                    or not isinstance(tile, dict) or any(type(tile.get(k)) is not int or tile[k] < 0 for k in ("x", "y"))):
                raise WorkStateError("machineReady requires observed locationId and tile x/y. " + allowed)
        elif kind == "inventory":
            if not trigger.get("itemId") or not isinstance(trigger.get("minCount", 1), int):
                raise WorkStateError(
                    "inventory trigger requires itemId (string) and integer minCount. " + allowed
                )
        elif kind == "crop":
            if not trigger.get("cropId"):
                raise WorkStateError("crop trigger requires cropId (string). " + allowed)
            if trigger.get("state", "mature") not in {"mature", "harvestable", "unwatered"}:
                raise WorkStateError(
                    "unsupported crop trigger state; use mature|harvestable|unwatered. " + allowed
                )
        else:
            raise WorkStateError(f"unsupported trigger type {kind!r}. " + allowed)

    def list_todos(self, save_id: str) -> list[dict[str, Any]]:
        return [asdict(t) for t in self.state(save_id).todos]

    def complete_todo(self, save_id: str, todo_id: str, status: str = "done") -> Todo:
        if status not in TODO_STATUSES:
            raise WorkStateError(f"invalid todo status '{status}'")

        def mutate(state: SaveWorkState) -> Todo:
            todo = next((t for t in state.todos if t.id == todo_id), None)
            if todo is None:
                raise WorkStateError(f"unknown todo '{todo_id}'")
            todo.status = status
            return todo

        return self._mutate(save_id, mutate)

    def evaluate_todos(
        self,
        save_id: str,
        *,
        snapshot: dict[str, Any] | None,
        game_date: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Return due todos using only the fresh native snapshot and game date."""
        state = self.state(save_id)
        due: list[dict[str, Any]] = []
        current_key = _calendar_key(
            (game_date or {}).get("year"),
            (game_date or {}).get("season"),
            (game_date or {}).get("day"),
        )
        for todo in state.todos:
            if todo.status not in {"pending", "due"}:
                continue
            if todo.expiry and current_key is not None:
                expiry_key = _calendar_key(
                    todo.expiry.get("year"), todo.expiry.get("season"), todo.expiry.get("day")
                )
                if expiry_key is not None and current_key > expiry_key:
                    continue
            if self._trigger_satisfied(todo.trigger, snapshot, current_key):
                due.append(asdict(todo))
        return due

    def _trigger_satisfied(
        self,
        trigger: dict[str, Any],
        snapshot: dict[str, Any] | None,
        current_key: tuple[int, int, int] | None,
    ) -> bool:
        payload = (snapshot or {}).get("payload")
        if not isinstance(payload, dict):
            payload = snapshot or {}
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        kind = trigger.get("type")
        if kind in {"calendar", "gameDay"}:
            trigger_key = _calendar_key(trigger.get("year"), trigger.get("season"), trigger.get("day"))
            return bool(trigger_key and current_key and current_key >= trigger_key)
        if kind == "gameTime":
            day = _calendar_key(trigger.get("year"), trigger.get("season"), trigger.get("day"))
            time_of_day = world.get("timeOfDay")
            return bool(day and current_key and (current_key > day or
                        (current_key == day and type(time_of_day) is int and time_of_day >= trigger["timeOfDay"])))
        if kind == "resource":
            companion = payload.get("companion") or {}
            field = "stamina" if trigger.get("resource") == "stamina" else "waterCanLevel"
            amount = companion.get(field)
            return type(amount) in {int, float} and amount >= trigger["minAmount"]
        if kind == "machineReady":
            # Native cross-map readiness facts; never infer readiness from a
            # countdown, stale tool result, or the absence of an observed machine.
            signals = payload.get("productionSignals") or []
            return any(isinstance(m, dict) and m.get("locationId") == trigger["locationId"]
                       and m.get("tile") == trigger["tile"] and m.get("isReady") is True
                       for m in signals if isinstance(signals, list))
        if kind == "inventory":
            inventory = payload.get("inventory") or world.get("inventory") or {}
            slots = inventory.get("slots", []) if isinstance(inventory, dict) else []
            count = sum(
                int(s.get("stack", s.get("count", 0)))
                for s in slots
                if isinstance(s, dict) and str(s.get("itemId")) == str(trigger.get("itemId"))
            )
            return count >= int(trigger.get("minCount", 1))
        if kind == "crop":
            farm = payload.get("farmWork") or world.get("farmWork") or {}
            crops = farm.get("matureCrops", []) if isinstance(farm, dict) else []
            crop_id = str(trigger.get("cropId"))
            return any(str(c.get("cropId")) == crop_id for c in crops if isinstance(c, dict))
        return False

    # ----------------------------------------------------- day rollover
    def settle_game_day(
        self, save_id: str, *, year: Any, season: Any, day: Any, funds: Any = None
    ) -> dict[str, Any]:
        """Settle one real, complete game day exactly once.

        Triggered only by the native ``year/season/day`` actually advancing. The
        guard is idempotent on ``<year>:<season>:<day>``:

        * the same day already settled -> ``ALREADY_SETTLED`` (no work, no replay);
        * first time this save is observed -> record the day without settling, so a
          first load never double-settles;
        * a day older than the last settled day (reloaded old save / time travel)
          -> ``NOT_A_NEW_DAY`` and the recorded day is never regressed.

        On a real advance it archives completed/cancelled task facts, migrates
        unfinished and waiting tasks forward, and leaves ``unknown``/``partial``
        work in place as anomalies for a decision — in-progress facts are never
        compressed away and never replayed.

        Day settlement records one progress note on the active main goal
        (date + completed-task count + funds); it never closes goal statuses.
        """
        key = _day_key(year, season, day)
        if key is None:
            raise WorkStateError("settle_game_day requires year, season and day")
        current = _calendar_key(year, season, day)
        if current is None:
            raise WorkStateError("settle_game_day received an invalid calendar day")

        result: dict[str, Any] = {}

        def mutate(state: SaveWorkState) -> None:
            nonlocal result
            if state.last_settled_day == key:
                result = {
                    "settled": False,
                    "reasonCode": "ALREADY_SETTLED",
                    "day": key,
                }
                return
            previous = state.last_settled_day
            previous_key = _parse_day_key(previous)
            if previous_key is None:
                # First observation of this save: remember the day without settling.
                state.last_settled_day = key
                result = {
                    "settled": False,
                    "reasonCode": "FIRST_OBSERVATION",
                    "day": key,
                    "carriedTaskIds": [
                        t.id for t in state.tasks if t.status not in {"completed", "cancelled"}
                    ],
                }
                return
            if current < previous_key:
                result = {
                    "settled": False,
                    "reasonCode": "NOT_A_NEW_DAY",
                    "day": key,
                    "lastSettledDay": previous,
                }
                return

            state.scheduler_epoch += 1
            state.decision = {}
            archived_ids: list[str] = []
            for task in list(state.tasks):
                if task.status in {"completed", "cancelled"}:
                    state.archive.append(
                        {
                            "settledFromDay": previous,
                            "settledToDay": key,
                            "task": asdict(task),
                        }
                    )
                    archived_ids.append(task.id)
                    state.tasks.remove(task)

            carried_ids: list[str] = []
            still_waiting: list[str] = []
            for task in state.tasks:
                # Waiting is condition-gated, never "the day changed so retry".
                # A calendar wait whose target day has arrived is the one case the
                # day rollover can release; everything else keeps its explicit
                # native condition and is re-evaluated from the next fresh snapshot.
                if task.status == "waiting":
                    released = False
                    for step in task.steps:
                        if step.status != "waiting":
                            continue
                        condition = step.wait
                        if condition is not None and condition.type == "gameDay":
                            trigger = _calendar_key(
                                condition.params.get("year"),
                                condition.params.get("season"),
                                condition.params.get("day"),
                            )
                            if trigger is not None and current >= trigger:
                                step.status = "pending"
                                step.wait = None
                                released = True
                    if released and not any(step.status == "waiting" for step in task.steps):
                        task.status = "pending"
                        task.updated_at = _now_iso()
                    else:
                        still_waiting.append(task.id)
                carried_ids.append(task.id)

            state.archive = state.archive[-200:]
            state.last_settled_day = key
            state.scheduler_epoch += 1
            # Day settlement records progress on the long-term main goal; the
            # goal itself stays active across days (only a verified preset day
            # boundary or the player may close a main goal).
            main = self._active_main_goal(state)
            if main is not None:
                completed_today = len(archived_ids)
                note = f"日结 {key}：当日完成 {completed_today} 项任务"
                if funds is not None and not isinstance(funds, bool):
                    note += f"，资金 {funds}"
                log = main.project.get("progressLog")
                if not isinstance(log, list):
                    log = []
                log.append({"date": key, "note": note[:_PROGRESS_NOTE_LIMIT], "at": _now_iso()})
                main.project = {**main.project, "progressLog": log[-_PROGRESS_LOG_LIMIT:]}
                main.updated_at = _now_iso()
                main.epoch += 1
            result = {
                "settled": True,
                "reasonCode": "DAY_ADVANCED",
                "fromDay": previous,
                "day": key,
                "archivedCount": len(archived_ids),
                "archivedTaskIds": archived_ids,
                "carriedTaskIds": carried_ids,
                "stillWaitingTaskIds": still_waiting,
                "anomalyTaskIds": [
                    t.id for t in state.tasks if t.status in {"unknown", "partial"}
                ],
            }

        self._mutate(save_id, mutate)
        return result

    def last_settled_day(self, save_id: str) -> str | None:
        return self.state(save_id).last_settled_day

    # ----------------------------------------------------------- controls
    def set_paused(self, save_id: str, paused: bool) -> SaveWorkState:
        def mutate(state: SaveWorkState) -> None:
            state.paused = bool(paused)

        self._mutate(save_id, mutate)
        return self.state(save_id)

    def epoch(self, save_id: str) -> int:
        return self.state(save_id).scheduler_epoch

    def clean_stale_waiting_tasks(
        self,
        save_id: str,
        *,
        timeout_seconds: float | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Expires waiting tasks that made no progress within timeout_seconds."""
        if timeout_seconds is None:
            try:
                timeout_seconds = float(os.getenv("STARDEW_WAITING_TIMEOUT_SECONDS", "1800"))
            except (ValueError, TypeError):
                timeout_seconds = 1800.0

        expired: list[dict[str, Any]] = []

        now_dt = datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)

        def waiting_elapsed(task: Task) -> float:
            if task.status != "waiting":
                return float("-inf")
            try:
                ts_dt = datetime.fromisoformat(task.updated_at or task.created_at)
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=UTC)
                return (now_dt - ts_dt).total_seconds()
            except (ValueError, TypeError):
                return 0.0

        # Snapshot/UI reads must not serialize the entire ledger when nothing
        # expires. Recheck the predicate under the mutation lock if repair is due.
        if not any(waiting_elapsed(task) >= timeout_seconds for task in self.state(save_id).tasks):
            return expired

        def mutate(state: SaveWorkState) -> None:
            for task in state.tasks:
                if task.status != "waiting":
                    continue
                elapsed = waiting_elapsed(task)

                if elapsed >= timeout_seconds:
                    task.status = "failed"
                    task.updated_at = _now_iso()
                    for step in task.steps:
                        if step.status == "waiting":
                            step.status = "failed"
                            step.outcome = "failed"
                            step.reason_code = "WAITING_TIMEOUT"
                            state.executions.append(
                                ExecutionEntry(
                                    command_id=step.command_id or f"timeout:{task.id}:{step.id}",
                                    task_id=task.id,
                                    step_id=step.id,
                                    operation=step.operation,
                                    outcome="failed",
                                    reason_code="WAITING_TIMEOUT",
                                )
                            )
                            expired.append({
                                "taskId": task.id,
                                "stepId": step.id,
                                "operation": step.operation,
                                "reasonCode": "WAITING_TIMEOUT",
                                "elapsedSeconds": elapsed,
                            })
            if expired:
                state.scheduler_epoch += 1

        self._mutate(save_id, mutate)
        return expired

    @staticmethod
    def _planting_evidence(state: SaveWorkState) -> dict[str, Any]:
        """Project confirmed pairs and remaining batches without changing goals.

        Ledger insertion order is native result order. Task/archive params only
        supply the map and seed identity; coordinates always come from effects.
        Legacy mapless actions use a verified operation default or confirmed
        navigation on the same native day; unproven map identities stay unknown.
        """
        tasks = {a["task"]["id"]: a["task"] for a in state.archive
                 if isinstance(a.get("task"), dict) and a["task"].get("id")}
        tasks.update({task.id: asdict(task) for task in state.tasks})
        steps = {(task_id, step["id"]): step for task_id, task in tasks.items()
                 for step in task.get("steps", []) if isinstance(step, dict) and step.get("id")}
        task_maps: dict[str, str] = {}
        pending: dict[tuple[str, int, int], tuple[str, str, str]] = {}
        batch_counts: dict[tuple[str, str, str], dict[str, int]] = {}
        recent_batches: dict[tuple[str, str, str], None] = {}
        actor_map: str | None = None
        actor_day: str | None = None
        seen = set()
        for entry in state.executions:
            if entry.game_date != actor_day:
                actor_map = None
                task_maps.clear()
                actor_day = entry.game_date
            if entry.outcome == "unknown":
                task_maps.pop(entry.task_id, None)
                if entry.operation == "navigate_to":
                    actor_map = None
                    task_maps.clear()
                continue
            if not entry.command_id or entry.outcome not in {"completed", "partial", "failed", "cancelled"}:
                continue
            identity = (entry.task_id, entry.step_id, entry.command_id)
            if identity in seen:
                continue
            seen.add(identity)
            step = steps.get((entry.task_id, entry.step_id), {})
            params = step.get("params") or {}
            if entry.operation == "navigate_to":
                actor_map = None
                task_maps.clear()
                if entry.outcome == "completed" and entry.effects:
                    location = entry.effects[-1].get("location")
                    if isinstance(location, str) and location:
                        task_maps[entry.task_id] = location
                        if entry.game_date:
                            actor_map = location
                continue
            seen_effects = set()
            for effect in entry.effects:
                if not isinstance(effect, dict):
                    continue
                kind = effect.get("state")
                if kind not in {"planted", "watered", "harvested", "cleared-dead-crop"}:
                    continue
                tile = effect.get("tile")
                if not isinstance(tile, dict) or any(
                    not isinstance(tile.get(axis), int) or isinstance(tile.get(axis), bool)
                    for axis in ("x", "y")
                ):
                    continue
                location = (effect.get("locationId") or entry.location_id
                            or (WorkStore._step_location(entry.operation, params) if step else None)
                            or task_maps.get(entry.task_id)
                            or actor_map)
                if not isinstance(location, str) or not location:
                    continue  # No map evidence: never match coordinates across maps.
                task_maps[entry.task_id] = location
                key = (location, tile["x"], tile["y"])
                effect_identity = (kind, key)
                if effect_identity in seen_effects:
                    continue
                seen_effects.add(effect_identity)
                if kind == "planted":
                    seed = effect.get("itemId") or params.get("seed_item_id")
                    if isinstance(seed, str) and seed:
                        batch = (entry.task_id, seed, location)
                        counts = batch_counts.setdefault(batch, {
                            "originalPlantedCount": 0, "confirmedWateredCount": 0,
                            "removedBeforeWaterCount": 0,
                        })
                        counts["originalPlantedCount"] += 1
                        recent_batches.pop(batch, None)
                        recent_batches[batch] = None
                        pending.pop(key, None)
                        pending[key] = batch
                else:
                    batch = pending.pop(key, None)
                    if batch is not None:
                        counter = ("confirmedWateredCount" if kind == "watered"
                                   else "removedBeforeWaterCount")
                        batch_counts[batch][counter] += 1
                        recent_batches.pop(batch, None)
                        recent_batches[batch] = None
        grouped: dict[tuple[str, str, str], list[dict[str, int]]] = {}
        # Newest batches first; keep the whole outstanding count even when the
        # compact coordinate budget cannot fit all historic unmatched plants.
        for (_location, x, y), batch in reversed(list(pending.items())):
            grouped.setdefault(batch, []).append({"x": x, "y": y})
        remaining = 64
        batches = []
        for (task_id, seed, location), tiles in grouped.items():
            if remaining == 0:
                break
            shown = tiles[:remaining]
            batches.append({"taskId": task_id, "seedItemId": seed, "locationId": location,
                            **batch_counts[(task_id, seed, location)],
                            "pendingCount": len(tiles), "tiles": shown,
                            "truncated": len(shown) < len(tiles)})
            remaining -= len(shown)
        recent = [
            {"taskId": task_id, "seedItemId": seed, "locationId": location,
             **batch_counts[(task_id, seed, location)],
             "pendingCount": len(grouped.get((task_id, seed, location), []))}
            for task_id, seed, location in reversed(list(recent_batches))
        ][:8]
        return {"pendingCount": len(pending), "batchCount": len(grouped), "batches": batches,
                "confirmedWateredCount": sum(c["confirmedWateredCount"] for c in batch_counts.values()),
                "countProvenance": "ledgerConfirmed; not automatic goal completion",
                "countScope": "all retained same-map planted-then-watered pairs, including historical completed batches; total, not a new delta",
                "recentBatches": recent, "recentBatchesTruncated": len(recent_batches) > 8,
                "truncated": len(pending) > 64,
                "basis": "confirmed planted effects without later same-map watered/harvested/cleared-dead-crop effects"}

    def overview(
        self,
        save_id: str,
        *,
        snapshot: dict[str, Any] | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compact, relevant-only view handed to the model."""
        self.clean_stale_waiting_tasks(save_id)
        state = self.state(save_id)
        active_goals = [asdict(g) for g in state.goals if g.status == "active"]
        tasks = [
            asdict(t)
            for t in state.tasks
            if t.status in {"pending", "running", "waiting", "partial", "unknown"}
        ]
        # nextStep mirrors exactly what the worker would claim next, using the
        # same readiness predicate (dependencies, goal status, lease, pause, and
        # the explicit native waiting condition).
        next_step = None
        ready = (
            None
            if state.paused
            else self._ready_step(
                state, now=_time.monotonic(), snapshot=snapshot, game_date=game_date
            )
        )
        if ready is not None:
            ready_task, ready_step = ready
            next_step = {
                "taskId": ready_task.id,
                "stepId": ready_step.id,
                "operation": ready_step.operation,
                "params": ready_step.params,
            }
        logged = {(e.task_id, e.step_id, e.command_id): e for e in state.executions}
        current_ids = {state.decision.get("taskId"), state.last_job.get("taskId")}
        ranked_anomalies = []
        for task in state.tasks:
            for step in task.steps:
                entry = logged.get((task.id, step.id, step.command_id))
                confirmed = ((entry.outcome if entry else step.outcome) in TERMINAL_STEP_STATUSES)
                uncertain = step.status == "unknown" or (
                    task.status in {"cancelled", "completed"} and step.command_id and not confirmed)
                if not uncertain and (task.status in {"cancelled", "completed"}
                                      or step.status not in {"partial", "failed"}):
                    continue
                # Actual native result time wins over later edits/cancellation
                # timestamps. No ledger entries or historic outcomes are removed.
                at = _parse_iso_epoch(entry.at if entry else task.created_at)
                rank = (bool(uncertain), task.id in current_ids, at)
                ranked_anomalies.append((rank, {
                    "taskId": task.id, "stepId": step.id,
                    "status": "unknown" if uncertain else step.status,
                    "reasonCode": step.reason_code or (
                        "UNCONFIRMED_AFTER_CANCEL" if uncertain and task.status == "cancelled"
                        else "UNCONFIRMED_NATIVE_RESULT" if uncertain else None),
                    "outcome": step.outcome, "commandId": step.command_id,
                }))
        anomalies = [row for _, row in sorted(ranked_anomalies, key=lambda pair: pair[0], reverse=True)]
        commands = {entry.command_id: entry for entry in state.executions
                    if entry.command_id and entry.outcome in TERMINAL_STEP_STATUSES}
        totals = self._execution_totals(commands.values())
        request_by_task = {task_id: request for request in state.requests
                           for task_id in request.get("taskIds", [])}
        requests = {request["id"]: request for request in state.requests}
        grouped: dict[str, list[ExecutionEntry]] = {}
        for entry in commands.values():
            # Legacy facts retain exact request.taskIds even after reload has
            # retired their Task objects. A shared goal is never an owner guess.
            request_id = entry.request_id or request_by_task.get(entry.task_id, {}).get("id")
            if request_id:
                grouped.setdefault(request_id, []).append(entry)
        return {
            "saveId": save_id,
            "paused": state.paused,
            "decision": dict(state.decision),
            "schedulerEpoch": state.scheduler_epoch,
            "lastSettledDay": state.last_settled_day,
            "archivedTaskCount": len(state.archive),
            "goals": active_goals,
            "tasks": tasks,
            "nextStep": next_step,
            "anomalies": anomalies[:5],
            "blockedDependencies": self.blocked_dependencies(save_id)[:5],
            "blockedBranches": [{k: b.get(k) for k in ("goalId", "operation", "target", "reasonCode",
                "failures", "blocked", "recoveryCondition")} for b in state.branch_failures
                if b.get("failures", 0) > 0][-5:],
            "recentExecutions": self.execution_log(save_id, limit=5),
            "confirmedExecutionTotals": {
                "source": "native_terminal_ledger",
                "scope": "all retained unique commands for this save; effect counts, not inventory or a new delta",
                "excludes": "inventory itself; only recorded native effects are counted, including retained history; unretained history is unknown",
                **totals,
                "hasUnconfirmedCommands": any(step.command_id and step.command_id not in commands
                    for task in state.tasks for step in task.steps),
            },
            "confirmedRequestTotals": {
                request_id: {**self._execution_totals(entries), "scope": "original_player_request",
                             "requestId": request_id,
                             "requestText": (None if requests.get(request_id, {}).get("origin") == "autonomy"
                                             else requests.get(request_id, {}).get("text")),
                             "source": "native_terminal_ledger",
                             "excludes": "other requests; inventory; planned counts"}
                for request_id, entries in grouped.items()
            },
            "plantingEvidence": self._planting_evidence(state),
            "waitingConditions": self.wait_conditions(save_id),
            "hasExecutableWork": next_step is not None,
            "lastJob": state.last_job,
            "executionPolicy": "one_model_selected_short_job",
        }

    # ---------------------------------------------------- requests & outbox
    @staticmethod
    def _execution_totals(entries) -> dict[str, Any]:
        entries = list(entries)
        effect_counts: dict[str, int] = {}
        item_quantities: dict[str, dict[str, Any]] = {}
        for entry in entries:
            for effect in entry.effects:
                kind = effect.get("state")
                if isinstance(kind, str) and kind != "skipped":
                    effect_counts[kind] = effect_counts.get(kind, 0) + 1
                if kind in {"withdrawn", "deposited", "harvested", "shipped"}:
                    quantity = item_quantities.setdefault(kind, {
                        "unit": "items", "count": 0, "knownCount": 0, "unknownStackEffects": 0})
                    stack = effect.get("count") if kind == "shipped" else effect.get("stack")
                    if isinstance(stack, int) and not isinstance(stack, bool) and stack > 0:
                        quantity["knownCount"] += stack
                    else:
                        quantity["unknownStackEffects"] += 1
                    quantity["count"] = None if quantity["unknownStackEffects"] else quantity["knownCount"]
        return {"commandCount": len(entries), "effectCounts": effect_counts,
                "effectCountUnit": "native_effect_records", "itemQuantities": item_quantities}

    def record_request(
        self, save_id: str, request_id: str, text: str, origin: str = "player"
    ) -> dict[str, Any]:
        if not request_id:
            raise WorkStateError("request_id is required")
        clean = (text or "").strip()
        if not clean:
            raise WorkStateError("request text cannot be empty")
        if origin not in REQUEST_ORIGINS:
            raise WorkStateError("request origin must be 'player', 'player-midflight' or 'autonomy'")

        def mutate(state: SaveWorkState) -> dict[str, Any]:
            existing = next((r for r in state.requests if r.get("id") == request_id), None)
            if existing is not None:
                return dict(existing)
            state.requests.append(
                {
                    "id": request_id,
                    "text": clean,
                    "origin": origin,
                    "createdAt": _now_iso(),
                    "taskIds": [],
                    "goalId": None,
                }
            )
            _trim_requests(state)
            return dict(state.requests[-1])

        return self._mutate(save_id, mutate)

    def bind_request_task(
        self, save_id: str, request_id: str, task_id: str, goal_id: str
    ) -> dict[str, Any]:
        def mutate(state: SaveWorkState) -> dict[str, Any]:
            request = next((r for r in state.requests if r.get("id") == request_id), None)
            if request is None:
                raise WorkStateError(f"unknown request '{request_id}'")
            task_ids = request.setdefault("taskIds", [])
            if task_id not in task_ids:
                task_ids.append(task_id)
            request["goalId"] = goal_id
            return dict(request)

        return self._mutate(save_id, mutate)

    def bind_goal_request(self, save_id: str, goal_id: str, request_id: str) -> None:
        """Retain the literal player instruction behind an accepted arrangement."""
        def mutate(state: SaveWorkState) -> None:
            if not any(r.get("id") == request_id for r in state.requests):
                raise WorkStateError(f"unknown request '{request_id}'")
            self._find_goal(state, goal_id).constraints["playerRequestId"] = request_id
        self._mutate(save_id, mutate)

    def find_request_for_task(self, save_id: str, task_id: str) -> dict[str, Any] | None:
        state = self.state(save_id)
        request = next(
            (r for r in reversed(state.requests) if task_id in (r.get("taskIds") or [])),
            None,
        )
        return dict(request) if request is not None else None

    def append_outbox_event(self, save_id: str, event: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(event, dict) or not str(event.get("commandId") or "").strip():
            raise WorkStateError("outbox event requires a commandId")
        explicit_kind = event.get("kind") or "terminal"
        if explicit_kind not in ("terminal", "correction"):
            raise WorkStateError(f"invalid outbox event kind '{explicit_kind}'")

        return self._mutate(save_id, lambda state: _append_outbox_event(state, event))

    def pending_outbox(self, save_id: str) -> list[dict[str, Any]]:
        state = self.state(save_id)
        return [
            dict(event)
            for event in state.outbox
            if (event.get("delivery") or {}).get("state") in {"pending", "sent"}
        ]

    def mark_outbox_sent(self, save_id: str, ids: list[str]) -> None:
        wanted = set(ids or [])
        if not wanted:
            return

        def mutate(state: SaveWorkState) -> None:
            for event in state.outbox:
                delivery = event.get("delivery") or {}
                if event.get("id") not in wanted or delivery.get("state") == "consumed":
                    continue
                delivery["attempts"] = int(delivery.get("attempts") or 0) + 1
                delivery["sentAt"] = _now_iso()
                delivery["state"] = "sent"

        self._mutate(save_id, mutate)

    def set_outbox_retry_block(self, save_id: str, reason: str | None) -> None:
        """Retain undelivered facts while a non-retryable provider error is resolved."""
        def mutate(state: SaveWorkState) -> None:
            for event in state.outbox:
                delivery = event.get("delivery") or {}
                if delivery.get("state") not in {"pending", "sent"}:
                    continue
                if reason:
                    delivery["retryBlocked"] = reason
                else:
                    delivery.pop("retryBlocked", None)
        self._mutate(save_id, mutate)

    def mark_outbox_consumed(self, save_id: str, ids: list[str]) -> None:
        wanted = set(ids or [])
        if not wanted:
            return

        def mutate(state: SaveWorkState) -> None:
            for event in state.outbox:
                delivery = event.get("delivery") or {}
                if event.get("id") not in wanted or delivery.get("state") == "consumed":
                    continue
                delivery["state"] = "consumed"
                delivery["consumedAt"] = _now_iso()

        self._mutate(save_id, mutate)

    def mark_outbox_notified(self, save_id: str, ids: list[str]) -> None:
        wanted = set(ids or [])
        if not wanted:
            return

        def mutate(state: SaveWorkState) -> None:
            for event in state.outbox:
                notify = event.get("notify") or {}
                if event.get("id") not in wanted or notify.get("state") == "delivered":
                    continue
                notify["state"] = "delivered"
                notify["deliveredAt"] = _now_iso()

        self._mutate(save_id, mutate)

    def queue_outbox_notification(self, save_id: str, ids: list[str], reply: dict[str, Any]) -> None:
        """Persist the verbatim model reply before sending; replay never runs work."""
        wanted = set(ids)
        def mutate(state: SaveWorkState) -> None:
            for event in state.outbox:
                if event.get("id") in wanted and event["notify"].get("state") != "delivered":
                    event["notify"]["reply"] = reply
        self._mutate(save_id, mutate)

    def pending_outbox_notifications(self, save_id: str) -> list[dict[str, Any]]:
        replies = {}
        for event in self.state(save_id).outbox:
            notify = event.get("notify") or {}
            reply = notify.get("reply")
            if notify.get("state") == "pending" and isinstance(reply, dict):
                replies[reply["payload"]["requestId"]] = reply
        return list(replies.values())

    def acknowledge_outbox_notification(self, save_id: str, request_id: str, ids: list[str]) -> None:
        wanted = set(ids)
        def mutate(state: SaveWorkState) -> None:
            for event in state.outbox:
                notify = event.get("notify") or {}
                payload = (notify.get("reply") or {}).get("payload") or {}
                if (event.get("id") in wanted and payload.get("requestId") == request_id
                        and payload.get("saveId") == save_id):
                    notify.update(state="delivered", deliveredAt=_now_iso())
        self._mutate(save_id, mutate)

    def pending_midflight_requests(self, save_id: str) -> list[dict[str, Any]]:
        state = self.state(save_id)
        return [
            dict(request)
            for request in state.requests
            if request.get("origin") == "player-midflight"
            and not request.get("consumedByDelivery")
        ]

    def mark_midflight_consumed(self, save_id: str, request_ids: list[str]) -> None:
        wanted = set(request_ids or [])
        if not wanted:
            return

        def mutate(state: SaveWorkState) -> None:
            for request in state.requests:
                if request.get("id") in wanted and not request.get("consumedByDelivery"):
                    request["consumedByDelivery"] = True

        self._mutate(save_id, mutate)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _find_goal(state: SaveWorkState, goal_id: str) -> Goal:
        goal = next((g for g in state.goals if g.id == goal_id), None)
        if goal is None:
            raise WorkStateError(f"unknown goal '{goal_id}'")
        return goal

    @staticmethod
    def _find_task(state: SaveWorkState, task_id: str) -> Task:
        task = next((t for t in state.tasks if t.id == task_id), None)
        if task is None:
            raise WorkStateError(f"unknown task '{task_id}'")
        return task

    @staticmethod
    def _find_step(task: Task, step_id: str) -> Step:
        step = next((s for s in task.steps if s.id == step_id), None)
        if step is None:
            raise WorkStateError(f"unknown step '{step_id}'")
        return step
