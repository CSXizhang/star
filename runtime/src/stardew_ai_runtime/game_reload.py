"""Restore game-save facts while retaining player intent and immutable paid usage."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .work_state import WorkStore

RUNTIME_FILES = (
    "work-state.json", "autonomy-state.json", "companion-profile.json",
    "companion-memory.json", "companion-care.json", "companion-milestones.json",
)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Invalid game-owned state: {path.name}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def pending_game_load(run_dir: Path, save_id: str, session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    request = _read(run_dir / "data/game-load.json")
    if request.get("saveId") != save_id or request.get("gameSessionId") != session_id:
        return None
    if _read(run_dir / "data/game-load-applied.json").get(save_id) == session_id:
        return None  # A bridge restart or socket reconnect does not rewind a live day.
    partitions = request.get("runtimePartitions")
    if partitions is not None:
        if not isinstance(partitions, dict) or set(partitions) != set(RUNTIME_FILES):
            raise ValueError("Game checkpoint is incomplete")
        for raw in partitions.values():
            if raw is not None and not isinstance(json.loads(raw), dict):
                raise ValueError("Game checkpoint partition is invalid")
    return request


def _reconcile(name: str, record: dict[str, Any], committed: dict[str, Any], loaded_day: str) -> dict[str, Any]:
    """Keep current intent; only the real save can establish completed world facts."""
    record = copy.deepcopy(record)
    if name == "work-state.json":
        record.update(tasks=[], executions=committed.get("executions", []), decision={}, lastJob={},
                      archive=committed.get("archive", []), branchFailures=[],
                      lastSettledDay=loaded_day, schedulerEpoch=int(record.get("schedulerEpoch", 0)) + 1)
        saved_goals = {g["id"]: g for g in committed.get("goals", [])}
        for goal in record.get("goals", []):
            if goal.get("status") == "completed" and saved_goals.get(goal["id"], {}).get("status") != "completed":
                goal["status"] = "active"
                goal["project"] = {**goal.get("project", {}), "phase": "needs-revalidation",
                                   "completionAssessment": None, "completedTaskIds": []}
            goal["epoch"] = int(goal.get("epoch", 0)) + 1
            # Project notes can mix player plans and inferred completion. Keep
            # them for intent, but explicitly invalidate their world evidence.
            goal["project"] = {**goal.get("project", {}), "requiresWorldRevalidation": True}
        saved_todos = {t["id"]: t for t in committed.get("todos", [])}
        for todo in record.get("todos", []):
            if todo.get("status") not in {"pending", "cancelled"}:
                todo["status"] = saved_todos.get(todo["id"], {}).get("status", "pending")
        for notice in record.get("notices", []):
            if not notice.get("answeredAt"):
                notice["delivered"] = False
    elif name == "autonomy-state.json":
        record.update(lastDecisionFingerprint=None, gameDate=loaded_day,
                      dailySpend=committed.get("dailySpend", 0), spendReservations={},
                      settledSpendCommands=committed.get("settledSpendCommands", []),
                      pending=[], completed=[], last_job_decision_id=None, last_event_key=None,
                      last_action_fingerprint=None, last_attempt_revision=-1,
                      decisionEpoch=int(record.get("decisionEpoch", 0)) + 1,
                      failureCount=0, failure_count=0, breakerTripped=False,
                      breakerCooldownUntil=None, breakerReason=None)
    elif name == "companion-memory.json":
        # Preferences and agreements are player information. Action events from
        # the abandoned day cannot prove what happened in the loaded world.
        record["entries"] = ([entry for entry in record.get("entries", []) if entry.get("kind") != "event"]
                             + [entry for entry in committed.get("entries", []) if entry.get("kind") == "event"])
        record["memoryRevision"] = int(record.get("memoryRevision", 0)) + 1
        record.setdefault("instructionRevision", max(0, record["memoryRevision"] - 1))
    elif name == "companion-care.json":
        record = copy.deepcopy(committed)
    elif name == "companion-milestones.json":
        for key, node in record.get("nodes", {}).items():
            saved = committed.get("nodes", {}).get(key, {})
            node["verification"] = saved.get("verification")
            if node.get("status") in {"completed", "missed"}:
                node["status"] = saved.get("status", "adopted" if node.get("goalId") else "suggested")
            saved_preps = {p.get("key"): p for p in saved.get("prepItems", [])}
            for prep in node.get("prepItems", []):
                if prep.get("status") == "done":
                    prep["status"] = saved_preps.get(prep.get("key"), {}).get("status", "unknown")
        record["lastGameDate"] = committed.get("lastGameDate")
    return record


def restore_game_load(run_dir: Path, request: dict[str, Any]) -> None:
    """Called after authentication and after prior provider/worker tasks stop."""
    save_id, session_id = request["saveId"], request["gameSessionId"]
    partitions = request.get("runtimePartitions")
    key = hashlib.sha256((save_id + ":" + session_id).encode()).hexdigest()
    backup_path = run_dir / "data/reloads" / (key + ".json")
    if not backup_path.exists():
        backup = {name: _read(run_dir / "data" / name).get(save_id) for name in RUNTIME_FILES}
        _write(backup_path, {"saveId": save_id, "priorRuntimePartitions": backup})
    # An interrupted restore reuses its original before-image.
    backup = _read(backup_path)["priorRuntimePartitions"]
    for name in RUNTIME_FILES:
        path = run_dir / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
            WorkStore._lock(lock)
            try:
                document = _read(path)
                raw = partitions.get(name) if partitions else None
                committed = json.loads(raw) if raw is not None else {}
                document[save_id] = _reconcile(name, backup.get(name) or {}, committed, request["gameDate"])
                _write(path, document)
            finally:
                WorkStore._unlock(lock)
    summaries = _read(run_dir / "data/reload-context.json")
    summaries[save_id] = {**(request.get("reloadSummary") or {}),
                         "gameSessionId": session_id, "loadedGameDate": request["gameDate"],
                         "requiresWorldRevalidation": True,
                         "instruction": "已读档：保留玩家目标、偏好、约定和决定答案；旧行动结果不证明当前世界状态。先重新观察再规划。用 read_reload_history 分页查阅完整原文。"}
    _write(run_dir / "data/reload-context.json", summaries)
    for name in ("life-turn.json", "life-snapshot.json"):
        _write(run_dir / "data" / name, {})


def mark_game_load_applied(run_dir: Path, request: dict[str, Any]) -> None:
    path = run_dir / "data/game-load-applied.json"
    document = _read(path)
    document[request["saveId"]] = request["gameSessionId"]
    _write(path, document)


def reload_context(run_dir: Path | None, save_id: str | None) -> dict[str, Any] | None:
    if run_dir is None or not save_id:
        return None
    summary = _read(run_dir / "data/reload-context.json").get(save_id)
    if summary:
        work = _read(run_dir / "data/work-state.json").get(save_id, {})
        memory = _read(run_dir / "data/companion-memory.json").get(save_id, {})
        # Read live intent, never resurrect an agreement removed after reload.
        summary = {**summary, "goals": work.get("goals", []),
                   "agreementsAndPreferences": [e for e in memory.get("entries", []) if e.get("kind") != "event"],
                   "decisions": work.get("notices", [])}
    return summary


def read_reload_history(run_dir: Path, save_id: str, offset: int = 0, limit: int = 12) -> dict[str, Any]:
    summary = reload_context(run_dir, save_id) or {}
    archive = summary.get("fullConversationArchive")
    if not archive:
        return {"entries": [], "total": 0, "nextOffset": None}
    path = Path(archive).resolve()
    if not path.is_relative_to((run_dir / "data").resolve()):
        raise ValueError("Reload history must belong to this installation")
    document = _read(path)
    if document.get("SaveId") != save_id:
        raise ValueError("Reload history belongs to another save")
    entries = document.get("Entries", [])
    offset, limit = max(0, int(offset)), max(1, min(20, int(limit)))
    end = min(len(entries), offset + limit)
    return {"entries": entries[offset:end], "total": len(entries),
            "nextOffset": end if end < len(entries) else None, "executionEvidenceIsCurrent": False}
