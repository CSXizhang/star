"""Shared harness-side plan executor.

Both entry points that advance a committed short plan use this single worker:

* the MCP tool ``run_next_step`` (explicit model/harness call), and
* the chat bridge's internal plan worker (no model turn per step).

The executor owns the whole commit discipline so neither entry point can skip it:

1. claim one ready step under the store lock (lease, so two processes cannot
   execute the same step);
2. derive a deterministic stable command id and **persist it before dispatch**;
3. dispatch through the same real operation implementation the normal tool path
   uses, passing that id down so the native command is idempotent on retry;
4. commit the real outcome (never fabricate success).

A step whose previous attempt cannot be confirmed stays ``unknown`` and the
worker asks for one model/player decision instead of silently re-dispatching and
double-spending.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from stardew_ai_runtime.work_state import WorkStateError, WorkStore

logger = logging.getLogger("stardew_ai_runtime.plan_executor")

Dispatch = Callable[[str, dict[str, Any], str], Awaitable[Any]]
Reconcile = Callable[[str], Awaitable[Any]]
# Returns (world.snapshot payload, game date dict) for the latest native state.
SnapshotProvider = Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]]

SUPPORTED_WAIT_TYPES = frozenset(
    {
        "gameDay",
        "inventory",
        "cropState",
        "shopOpen",
        "tileClear",
        "obstacleChange",
        "transportRetry",
    }
)

# Native reason codes that name a real, checkable precondition. Everything else
# stays a deviation for a model/player decision.
_BLOCKED_WAIT_PARAMS = ("blockingTile", "tile", "occupiedTile")


def extract_wait_condition(result: Any) -> dict[str, Any] | None:
    """Read an explicit, supported waiting condition out of a native result.

    Only conditions the runtime can actually evaluate from a fresh native
    snapshot are accepted; an invented or unsupported marker is ignored so the
    step stays a normal deviation instead of silently parking forever.
    """
    if not isinstance(result, dict):
        return None
    candidate = result.get("waitCondition")
    if not isinstance(candidate, dict):
        details = result.get("details")
        candidate = details.get("waitCondition") if isinstance(details, dict) else None
    if isinstance(candidate, dict) and candidate.get("type") in SUPPORTED_WAIT_TYPES:
        return {
            "type": candidate["type"],
            "params": candidate.get("params") if isinstance(candidate.get("params"), dict) else {},
            "reasonCode": candidate.get("reasonCode") or result.get("reasonCode"),
        }
    return None


def derive_command_id(save_id: str, task_id: str, step_id: str, attempt: int) -> str:
    """Deterministic stable command id for one plan-step attempt.

    Keeping the id stable across a retry of the same attempt is what makes
    reconciliation and native de-duplication real instead of cosmetic.
    """
    save = save_id or "unknown-save"
    return f"plan:{save}:{task_id}:{step_id}:attempt-{int(attempt)}"


@dataclass
class StepExecution:
    """One worker turn: idle, executed, or deferred for a decision."""

    status: str  # idle | executed | deferred
    task_id: str | None = None
    goal_id: str | None = None
    step_id: str | None = None
    operation: str | None = None
    command_id: str | None = None
    attempt: int | None = None
    outcome: str | None = None
    task_status: str | None = None
    reason_code: str | None = None
    effects: list[dict[str, Any]] = field(default_factory=list)
    snapshot_revision: int | None = None
    result: Any = None
    message: str | None = None
    recovery_decisions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def executed(self) -> bool:
        return self.status == "executed"

    @property
    def needs_model(self) -> bool:
        """True only for meaningful deviations that require a new decision."""
        if self.status == "deferred":
            return True
        if self.status != "executed":
            return False
        return self.outcome in {"partial", "unknown"}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "taskId": self.task_id,
            "goalId": self.goal_id,
            "stepId": self.step_id,
            "operation": self.operation,
            "commandId": self.command_id,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "taskStatus": self.task_status,
            "reasonCode": self.reason_code,
        }
        if self.message:
            payload["message"] = self.message
        if self.result is not None:
            payload["result"] = self.result
        return payload


def classify_step_outcome(result: Any) -> tuple[str, str | None]:
    """Map a real operation result to the unified outcome enum."""
    if not isinstance(result, dict) or not result:
        return "unknown", "INVALID_TOOL_RESULT"
    reason_raw = result.get("reasonCode")
    if not reason_raw:
        err = result.get("error")
        if isinstance(err, dict):
            reason_raw = err.get("code") or err.get("message")
        else:
            reason_raw = err or result.get("message")
    reason = str(reason_raw) if reason_raw else None
    terminal = str(result.get("terminalState") or "").lower()
    status = str(result.get("status") or "").lower()
    outcome = result.get("outcome")
    if result.get("isError") or terminal in {"failed", "rejected"} or status in {"failed", "blocked", "rejected"} or outcome in {"failed", "rejected"}:
        return "partial", reason or "STEP_FAILED"
    if terminal in {"cancelled", "canceled"} or outcome == "cancelled":
        return "cancelled", reason or "CANCELLED"
    if status in {"executing", "running"} or terminal == "running":
        return "unknown", "STEP_IN_PROGRESS"
    if outcome in {"completed", "partial", "unknown"}:
        return outcome, reason or ("STEP_OUTCOME_UNKNOWN" if outcome == "unknown" else None)
    if status == "no-work":
        return "completed", "NO_WORK"
    if terminal == "succeeded" or status == "completed":
        return "completed", reason
    return "unknown", reason or "UNRECOGNIZED_TOOL_RESULT"


def is_transient_transport_error(ex: Exception) -> bool:
    # Only typed transport failures or known wrapping from the MCP transport.
    if isinstance(ex, (ConnectionError, TimeoutError)):
        return True
    text = str(ex).lower()
    return any(marker in text for marker in (
        "connection refused", "connection reset", "connectionclosed", "connection closed",
        "winerror 1225", "winerror 10061", "winerror 10054", "broken pipe",
        "transport disconnected", "endofstream",
    ))


def normalise_native_result(
    native: Any,
) -> tuple[str | None, str | None, list[dict[str, Any]], int | None]:
    """Extract (outcome, reason, effects, revision) from a reconciled native result."""
    if not isinstance(native, dict):
        return None, None, [], None
    terminal = native.get("terminalState")
    if not terminal:
        return None, None, [], None
    if terminal == "succeeded":
        outcome, reason = "completed", None
    elif terminal in {"partially-succeeded", "partial"}:
        outcome, reason = "partial", "RECONCILED_PARTIAL"
    elif terminal in {"failed", "rejected"}:
        outcome, reason = "partial", "RECONCILED_FAILED"
    elif terminal in {"cancelled", "canceled"}:
        outcome, reason = "cancelled", "RECONCILED_CANCELLED"
    else:
        return None, None, [], None
    effects = native.get("effects")
    if not isinstance(effects, list):
        effects = []
    revision = native.get("worldRevision")
    if not isinstance(revision, int):
        revision = None
    return outcome, reason, effects, revision


class PlanExecutor:
    """Claims and executes ready plan steps for one save (single worker)."""

    def __init__(
        self,
        store: WorkStore,
        dispatch: Dispatch,
        *,
        reconcile: Reconcile | None = None,
        lease_seconds: float = 180.0,
        snapshot_provider: SnapshotProvider | None = None,
        max_transport_retries: int = 2,
    ):
        self.store = store
        self.dispatch = dispatch
        self.reconcile = reconcile
        self.lease_seconds = lease_seconds
        self.snapshot_provider = snapshot_provider
        self.max_transport_retries = max(1, max_transport_retries)

    def _fresh_state(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self.snapshot_provider is None:
            return None, None
        try:
            snapshot, game_date = self.snapshot_provider()
        except Exception:
            logger.debug("Snapshot provider failed", exc_info=True)
            return None, None
        return snapshot, game_date

    def has_ready_work(self, save_id: str) -> bool:
        """Cheap read-only check used to avoid touching the store when idle.

        Waiting steps are gated by their explicit condition, so this accepts the
        fresh native snapshot: an unchanged snapshot never makes a waiting step
        ready.
        """
        snapshot, game_date = self._fresh_state()
        try:
            return self.store.has_ready_step(
                save_id, snapshot=snapshot, game_date=game_date
            )
        except WorkStateError:
            return False

    async def run_once(
        self,
        save_id: str,
        worker_id: str,
        *,
        recover: bool = False,
    ) -> StepExecution:
        """Claim and execute at most one ready step. Returns idle when none is ready."""
        decisions: list[dict[str, Any]] = []
        if recover:
            try:
                decisions = self.store.recover(save_id)
            except WorkStateError as ex:
                logger.debug("recover failed for %s: %s", save_id, ex)

        snapshot, game_date = self._fresh_state()
        claim = self.store.claim_next_step(
            save_id,
            worker_id,
            lease_seconds=self.lease_seconds,
            snapshot=snapshot,
            game_date=game_date,
        )
        if claim is None:
            return StepExecution(status="idle", recovery_decisions=decisions)

        step = StepExecution(
            status="executed",
            task_id=claim["taskId"],
            goal_id=claim.get("goalId"),
            step_id=claim["stepId"],
            operation=claim["operation"],
            attempt=claim["attempt"],
            recovery_decisions=decisions,
        )

        # A re-claimed step whose previous attempt is still unconfirmed may already
        # have a terminal native result. Reconcile that persisted id before
        # dispatching anything new; a disconnected id is not de-duplication proof.
        previous_id = claim.get("previousCommandId")
        if previous_id and self.reconcile is not None:
            try:
                native = await self.reconcile(previous_id)
            except Exception as ex:
                logger.debug("Reconcile of %s failed: %s", previous_id, ex)
                native = None
            outcome, reason, effects, revision = normalise_native_result(native)
            if outcome is not None:
                committed = self.store.commit_step_result(
                    save_id,
                    task_id=claim["taskId"],
                    step_id=claim["stepId"],
                    outcome=outcome,
                    effects=effects,
                    reason_code=reason,
                    snapshot_revision=revision,
                    command_id=previous_id,
                )
                step.command_id = previous_id
                step.outcome = outcome
                step.reason_code = reason
                step.effects = effects
                step.task_status = committed.get("taskStatus")
                step.result = {"reconciled": True, "native": native}
                return step

        command_id = derive_command_id(save_id, claim["taskId"], claim["stepId"], claim["attempt"])
        step.command_id = command_id
        # Persist the stable id BEFORE dispatch so a crash mid-flight is recoverable.
        self.store.assign_command_id(save_id, claim["taskId"], claim["stepId"], command_id)

        try:
            result = await self.dispatch(claim["operation"], claim.get("params") or {}, command_id)
        except Exception as ex:
            return self._commit_failure(save_id, step, command_id, ex)

        # A native result may name an explicit, checkable precondition (e.g. the
        # shop is closed, the player is standing on the tile, seeds are missing).
        # Park the step behind that condition instead of reporting a deviation for
        # every model turn; only supported native conditions are accepted.
        wait_condition = extract_wait_condition(result)
        if wait_condition is not None:
            return self._park_for_wait(save_id, step, command_id, wait_condition, result)

        outcome, reason = classify_step_outcome(result)
        effects = result.get("effects") if isinstance(result, dict) else None
        revision = result.get("snapshotRevision") if isinstance(result, dict) else None
        if revision is None and isinstance(result, dict) and isinstance(result.get("worldRevision"), int):
            revision = result.get("worldRevision")
        native_command_id = result.get("commandId") if isinstance(result, dict) else None

        committed = self.store.commit_step_result(
            save_id,
            task_id=claim["taskId"],
            step_id=claim["stepId"],
            outcome=outcome,
            effects=effects,
            reason_code=reason,
            snapshot_revision=revision,
            command_id=native_command_id or command_id,
        )
        step.outcome = outcome
        step.reason_code = reason
        step.effects = list(effects or [])
        step.snapshot_revision = revision
        step.task_status = committed.get("taskStatus")
        step.result = result
        return step

    def _park_for_wait(
        self,
        save_id: str,
        step: StepExecution,
        command_id: str,
        wait_condition: dict[str, Any],
        result: Any,
    ) -> StepExecution:
        """Park a step behind an explicit native condition (never a success)."""
        try:
            parked = self.store.mark_task_waiting(
                save_id,
                step.task_id or "",
                step_id=step.step_id,
                condition=wait_condition,
                reason_code=wait_condition.get("reasonCode"),
            )
        except WorkStateError as ex:
            logger.warning("Could not park %s behind a wait condition: %s", step.step_id, ex)
            return self._commit_failure(save_id, step, command_id, ex)
        step.status = "executed"
        step.outcome = "waiting"
        step.reason_code = wait_condition.get("reasonCode")
        step.task_status = parked.get("status")
        step.result = result
        step.message = parked.get("waitDescription")
        return step

    def _commit_failure(
        self, save_id: str, step: StepExecution, command_id: str, ex: Exception
    ) -> StepExecution:
        transient = is_transient_transport_error(ex)
        reason = "TRANSPORT_RETRY_EXHAUSTED" if transient else "STEP_DISPATCH_REJECTED"
        step.result = {"status": "unknown" if transient else "rejected", "reasonCode": reason, "message": str(ex)}
        # A transport/dispatch fault is the one transient case: park it behind a
        # bounded backoff so the worker cannot spin, then it is claimable once the
        # backoff window elapses. The bound is what makes it explicit, not endless.
        try:
            attempts = int(step.attempt or 1)
            # The bound is the total number of dispatch attempts allowed for one
            # step. Until it is reached a transient transport fault is parked
            # behind a bounded backoff; afterwards it becomes unknown and asks for
            # one decision instead of retrying forever.
            if transient and attempts < self.max_transport_retries:
                reason = "TRANSPORT_RETRY_PENDING"
                self.store.mark_task_waiting(
                    save_id,
                    step.task_id or "",
                    step_id=step.step_id,
                    condition={
                        "type": "transportRetry",
                        "params": {"attemptsUsed": attempts, "backoffSeconds": 30.0},
                        "reasonCode": reason,
                        "attemptsAllowed": self.max_transport_retries,
                    },
                )
                step.status = "executed"
                step.outcome = "waiting"
                step.reason_code = reason
                step.message = str(ex)
                step.result = {"message": str(ex), "waitCondition": {"type": "transportRetry"}}
                return step
            self.store.commit_step_result(
                save_id,
                task_id=step.task_id or "",
                step_id=step.step_id or "",
                outcome="unknown" if transient else "partial",
                reason_code=reason,
                command_id=command_id,
            )
        except WorkStateError as commit_ex:
            logger.warning("Could not commit failed dispatch for %s: %s", step.step_id, commit_ex)
        step.outcome = "unknown" if transient else "partial"
        step.reason_code = reason
        step.status = "deferred"
        step.message = str(ex)
        return step
