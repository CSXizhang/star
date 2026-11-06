"""Model Context Protocol (MCP) Server for Stardew AI Companion (Farming V1).

Exposes streamlined farming tools (work overview, status, queries, water, harvest,
deposit, organize, task control) to MCP clients via stdio transport.
All tool requests pass through the thin policy/scheduler layer, strictly
enforcing single-task execution, input validation, and loopback security.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, BeforeValidator, Field

from stardew_ai_runtime.agent_instructions import read_guidance as load_guidance
from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.capability_consent import learning_provenance
from stardew_ai_runtime.chat_backend_config import load_chat_backend_config
from stardew_ai_runtime.companion_memory import CompanionMemoryStore, resolve_companion_id
from stardew_ai_runtime.companion_milestones import (
    CompanionMilestoneStore,
    MilestoneError,
    wire_node,
)
from stardew_ai_runtime.companion_profile import CompanionProfileStore
from stardew_ai_runtime.decision_context import water_can_resources
from stardew_ai_runtime.game_tools import GAME_TOOLS, operation_description, register_game_tools
from stardew_ai_runtime.mcode_wire_schema import mcode_tool_schema
from stardew_ai_runtime.mcp_contract import (
    _CAPABILITY_EXAMPLES as _CAPABILITY_EXAMPLES,
)
from stardew_ai_runtime.mcp_contract import (
    _EXECUTION_SCOPE_SCHEMA as _EXECUTION_SCOPE_SCHEMA,
)
from stardew_ai_runtime.mcp_contract import (
    _ITEM_COUNT_SHAPE as _ITEM_COUNT_SHAPE,
)
from stardew_ai_runtime.mcp_contract import (
    _PLAN_OPERATION_CALLS as _PLAN_OPERATION_CALLS,
)
from stardew_ai_runtime.mcp_contract import (
    _TILE_SHAPE as _TILE_SHAPE,
)
from stardew_ai_runtime.mcp_contract import (
    BASE_TOOLS as BASE_TOOLS,
)
from stardew_ai_runtime.mcp_contract import (
    CAPABILITY_GROUPS as CAPABILITY_GROUPS,
)
from stardew_ai_runtime.mcp_contract import (
    DEFAULT_MCP_INSTRUCTIONS as DEFAULT_MCP_INSTRUCTIONS,
)
from stardew_ai_runtime.mcp_contract import (
    INTERNAL_TOOLS as INTERNAL_TOOLS,
)
from stardew_ai_runtime.mcp_contract import (
    LEGACY_GAME_TOOLS as LEGACY_GAME_TOOLS,
)
from stardew_ai_runtime.mcp_contract import (
    LIFE_TOOLS as LIFE_TOOLS,
)
from stardew_ai_runtime.mcp_contract import (
    LIGHT_TOOLS as LIGHT_TOOLS,
)
from stardew_ai_runtime.mcp_contract import (
    MEMORY_WRITE_SCHEMA as MEMORY_WRITE_SCHEMA,
)
from stardew_ai_runtime.mcp_contract import (
    ChestCoordinate as ChestCoordinate,
)
from stardew_ai_runtime.mcp_contract import (
    InteractionTileXY as InteractionTileXY,
)
from stardew_ai_runtime.mcp_contract import (
    ItemCount as ItemCount,
)
from stardew_ai_runtime.mcp_contract import (
    ItemQuantity as ItemQuantity,
)
from stardew_ai_runtime.mcp_contract import (
    ShortJobStep as ShortJobStep,
)
from stardew_ai_runtime.mcp_contract import (
    ShortJobTask as ShortJobTask,
)
from stardew_ai_runtime.mcp_contract import (
    SpatialRegion as SpatialRegion,
)
from stardew_ai_runtime.mcp_contract import (
    TileXY as TileXY,
)
from stardew_ai_runtime.mcp_contract import (
    _accepts_command_id as _accepts_command_id,
)
from stardew_ai_runtime.mcp_contract import (
    _compact_plan_task as _compact_plan_task,
)
from stardew_ai_runtime.mcp_contract import (
    _is_satisfied_skip as _is_satisfied_skip,
)
from stardew_ai_runtime.mcp_contract import (
    _item_dicts as _item_dicts,
)
from stardew_ai_runtime.mcp_contract import (
    _McodeFastMCP as _McodeFastMCP,
)
from stardew_ai_runtime.mcp_contract import (
    _native_action_response as _native_action_response,
)
from stardew_ai_runtime.mcp_contract import (
    _normalize_wire_arrays as _normalize_wire_arrays,
)
from stardew_ai_runtime.mcp_contract import (
    _reject_boolean_integer as _reject_boolean_integer,
)
from stardew_ai_runtime.mcp_contract import (
    _reject_integer_booleans as _reject_integer_booleans,
)
from stardew_ai_runtime.mcp_contract import (
    _safe_wait_for_fresh_snapshot as _safe_wait_for_fresh_snapshot,
)
from stardew_ai_runtime.mcp_contract import (
    _tile_dicts as _tile_dicts,
)
from stardew_ai_runtime.mcp_contract import (
    _unwrap_item_array as _unwrap_item_array,
)
from stardew_ai_runtime.mcp_contract import (
    capability_example as capability_example,
)
from stardew_ai_runtime.mcp_contract import (
    capability_parameter_shape as capability_parameter_shape,
)
from stardew_ai_runtime.mcp_contract import (
    compact_work_overview as compact_work_overview,
)
from stardew_ai_runtime.mcp_contract import (
    resolve_surface as resolve_surface,
)
from stardew_ai_runtime.mcp_native_tools import register_native_tools
from stardew_ai_runtime.native_capabilities import CapabilityError, NativeCapabilityStore
from stardew_ai_runtime.plan_executor import (
    PlanExecutor,
)
from stardew_ai_runtime.plan_executor import (
    classify_step_outcome as _classify_step_outcome,
)
from stardew_ai_runtime.scheduler import (
    CompanionScheduler,
    NoActiveTaskError,
    PolicyViolationError,
    SchedulerError,
    extract_chest_summary,
    extract_non_tool_items,
    validate_action_tiles,
)
from stardew_ai_runtime.snapshot_cache import cache_lock, read_cache
from stardew_ai_runtime.wiki import WikiLookup
from stardew_ai_runtime.work_state import (
    PLANT_WORKFLOW_PLAN_ERROR,
    WorkStateError,
    WorkStore,
    bind_execution_scope_params,
)

logger = logging.getLogger("stardew_ai_runtime.mcp_server")


def create_mcp_server(
    run_dir: Path | str | None = None,
    scheduler: CompanionScheduler | None = None,
    server_name: str = "stardew-companion",
    instructions: str | None = None,
    full: bool | None = None,
    surface: str | None = None,
) -> FastMCP:
    """Create the one scheduler-backed server with explicit compatibility surfaces.

    full is the generic legacy default; internal retains worker dispatch and
    reconciliation. light exposes the eight game tools with immediate execution;
    life is an alias for light, including native access when authority permits.
    The environment overrides the CLI surface except for internal workers.
    """
    resolved_surface = resolve_surface(full, surface)
    if resolved_surface == "life":  # Legacy CLI/config name; same live game tools.
        resolved_surface = "light"
    inst = instructions if instructions is not None else DEFAULT_MCP_INSTRUCTIONS
    if instructions is None and resolved_surface != "light":
        inst += "持续生产授权后可用set_autonomy(goal_id=...)绑定目标并接续，不改变空闲主动帮忙开关。规模工作分批，由模型选择补水/补给/食物恢复及下一业务。持续生产goal保持active，等待用manage_todo的gameTime/machineReady/resource条件，不轮询倒计时；处理完待办再按需记录下一周期。observe_production提供指定地图地面鸡蛋/水源/食物，鸡蛋用pickup_items；building_services能力组提供原生建造升级购动物目录与服务操作。"
    server_type = _McodeFastMCP if os.environ.get("STARDEW_MCP_WIRE_STYLE") == "mcode" else FastMCP
    mcp = server_type(server_name, instructions=inst)
    external_codex = os.environ.get("STARDEW_EXTERNAL_CODEX") == "1" and not os.environ.get("STARDEW_DECISION_TOKEN")
    external_token = uuid.uuid4().hex
    external_save_id: str | None = None
    external_selected = False
    external_handed_off = False

    def decision_token() -> str | None:
        return external_token if external_codex else os.environ.get("STARDEW_DECISION_TOKEN")

    if external_codex:
        @mcp.tool()
        async def begin_game_turn() -> dict[str, Any]:
            """Start one external Codex decision after prior work has reached a terminal result.

            Requires idle, unpaused work and disabled free mode. Does not execute
            anything; then observe as needed and select ONE job with submit_plan.
            Never invoke concurrently with in-game chat. Repeated calls before
            selection are idempotent; active/uncertain work blocks a new turn.
            """
            nonlocal external_token, external_save_id, external_selected, external_handed_off
            if external_handed_off and external_save_id and autonomy_for_run().state(external_save_id).enabled:
                raise ToolError("GAME_BUSY: autonomy owns ongoing work; disable it before a new external turn")
            if external_selected and external_save_id:
                state = work_for_run().state(external_save_id)
                if state.paused or any(task.status in {"pending", "running", "waiting", "unknown"} for task in state.tasks):
                    raise ToolError("GAME_BUSY: resolve existing work before starting another external turn")
                if state.decision.get("selected") and not state.decision.get("finished"):
                    raise ToolError("GAME_BUSY: native owner is still settling the previous job")
                # The previous task has settled. Reconnect to verify which save is
                # currently loaded before granting a fresh decision.
                external_selected = False
                external_handed_off = False
            sid = await current_save_id()
            if autonomy_for_run().state(sid).enabled:
                raise ToolError("GAME_BUSY: disable free mode before using external Codex")
            try:
                store = work_for_run()
                current = store.state(sid).decision
                retry = (current.get("token") == external_token and not current.get("selected")
                         and not current.get("finished") and current.get("expires", 0) > time.time())
                token = external_token if retry else uuid.uuid4().hex
                store.begin_decision(sid, token, require_idle=True)
                external_token = token
                external_save_id = sid
                external_selected = False
            except WorkStateError as ex:
                raise ToolError(str(ex)) from None
            return {"saveId": sid, "status": "ready", "executionScope": "one_short_job"}
    sched = scheduler or CompanionScheduler(run_dir=run_dir)
    # Populated at the end from the real registered tools; shared with the
    # discovery/call closures so both surfaces expose every base op.
    base_tools: dict[str, Any] = {}
    job_request_owner: ContextVar[str | None] = ContextVar("job_request_owner", default=None)
    base_schemas: dict[str, dict[str, Any]] = {}
    base_metadata: dict[str, Any] = {}

    def validate_base_arguments(name: str, args: dict[str, Any], *, for_plan: bool = False) -> dict[str, Any]:
        """Share the real FastMCP parser without injecting defaults into jobs."""
        metadata = base_metadata[name]
        try:
            pre_parsed = metadata.pre_parse_json(args)
            for key, value in pre_parsed.items():
                if field := metadata.arg_model.model_fields.get(key):
                    value = pre_parsed[key] = _normalize_wire_arrays(value, field.annotation)
                    _reject_integer_booleans(value, field.annotation)
            if name == "withdraw_from_chest" and pre_parsed.get("items") is not None:
                items = pre_parsed["items"]
                if isinstance(items, list):
                    items = [
                        {**item, "count": int(item["count"])}
                        if isinstance(item, dict) and isinstance(item.get("count"), str)
                        and re.fullmatch(r"[0-9]+", item["count"]) else item
                        for item in items
                    ]
                # This field's dict[str, Any] otherwise leaves XML numeric strings
                # unparsed until dispatch. Validate the whole job before moving.
                pre_parsed["items"] = CompanionScheduler._validate_withdraw_items(items=items)
            parsed = metadata.arg_model.model_validate(pre_parsed)
            for key in ("tiles", "target_tiles"):
                if pre_parsed.get(key) is not None:
                    value = getattr(parsed, key)
                    if isinstance(value, list):
                        # FastMCP keeps validated sub-models as model instances;
                        # the scheduler-facing contract stays plain coordinate dicts.
                        value = [entry.model_dump() if isinstance(entry, BaseModel) else entry
                                 for entry in value]
                    coordinates = [] if key == "target_tiles" and value == [] else validate_action_tiles(value, preserve_order=True)
                    setattr(parsed, key, coordinates)
        except (ValueError, PolicyViolationError) as ex:
            raise ToolError(f"Invalid parameters for '{name}': {ex}") from None
        if for_plan:
            # Durable params must contain plain JSON values, including nested models.
            # Preserve allowed plan-only keys (e.g. location_id for hoe_tiles).
            result = {**args, **parsed.model_dump(exclude_unset=True)}
        else:
            result = {key: value for key, value in parsed.model_dump_one_level().items()
                      if key in parsed.model_fields_set}
        if name in {"water_auto", "harvest_auto"}:
            # FastMCP passes function defaults to the guarded wrapper. Preserve
            # the old omitted-None contract while retaining an explicit [].
            result = {key: value for key, value in result.items()
                      if key not in {"target_tiles", "location_id"} or value is not None}
        return result

    def autonomy_for_run() -> AutonomyController:
        actual_run_dir = getattr(sched, "run_dir", None) or run_dir
        return AutonomyController(Path(actual_run_dir or ".") / "data" / "autonomy-state.json")

    def wiki_for_run() -> WikiLookup:
        actual_run_dir = getattr(sched, "run_dir", None) or run_dir
        return WikiLookup(Path(actual_run_dir or ".") / "data" / "wiki-cache.json")

    def work_for_run() -> WorkStore:
        actual_run_dir = getattr(sched, "run_dir", None) or run_dir
        return WorkStore(Path(actual_run_dir or ".") / "data" / "work-state.json")

    def milestones_for_run() -> CompanionMilestoneStore:
        actual_run_dir = getattr(sched, "run_dir", None) or run_dir
        return CompanionMilestoneStore(Path(actual_run_dir or ".") / "data" / "companion-milestones.json")

    def profile_for_run() -> CompanionProfileStore:
        actual_run_dir = getattr(sched, "run_dir", None) or run_dir
        return CompanionProfileStore(Path(actual_run_dir or ".") / "data" / "companion-profile.json")

    def memory_for_run() -> CompanionMemoryStore:
        return CompanionMemoryStore(Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "companion-memory.json")

    capability_store = None

    def capabilities_for_run() -> NativeCapabilityStore:
        nonlocal capability_store
        if capability_store is None:
            capability_store = NativeCapabilityStore(Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "abilities")
        return capability_store

    async def learning_permission(quote: str | None, notice_id: str | None = None, previous: dict | None = None) -> dict:
        assert_life_turn_current(allow_control=True)
        sid = await current_save_id()
        path = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "life-turn.json"
        lease = read_cache(path) if path.exists() else {}
        notice = next((n for n in work_for_run().state(sid).notices if n.get("id") == notice_id), None) if notice_id else None
        return learning_provenance(save_id=sid, request_id=os.environ.get("STARDEW_LIFE_TURN_ID"),
                                   lease=lease or {}, quote=quote, notice=notice, previous=previous)

    async def extension_host_call(fn, *args, **kwargs):
        previous = getattr(sched, "await_terminal_results", False)
        sched.await_terminal_results = True
        try:
            return await fn(*args, **kwargs)
        finally:
            sched.await_terminal_results = previous

    def memory_context(save_id: str) -> dict[str, str]:
        root = Path(getattr(sched, "run_dir", None) or run_dir or ".")
        lease_path = root / "data" / "life-turn.json"
        try:
            lease = read_cache(lease_path) or {}
        except FileNotFoundError:
            lease = {}
        if lease.get("requestId") != os.environ.get("STARDEW_LIFE_TURN_ID") or lease.get("saveId") != save_id:
            lease = {}
        try:
            config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            config = {}
        source = str(lease.get("memoryConnectionSource") or os.environ.get("STARDEW_CONNECTION_SOURCE") or config.get("ConnectionSource") or "builtin")
        provider = str(lease.get("memoryProvider") or ("mcode" if source == "external" else load_chat_backend_config().get("backend", "")))
        snapshot = getattr(sched, "latest_snapshot", None) or {}
        world = (snapshot.get("payload") or {}).get("world") or {}
        date = ":".join(str(world[k]) for k in ("year", "season", "dayOfMonth")) if all(world.get(k) is not None for k in ("year", "season", "dayOfMonth")) else ""
        return {"companion_id": resolve_companion_id(root, save_id, provider=provider, connection_source=source), "game_date": date}

    async def ready_memory_context(save_id: str) -> dict[str, str]:
        # A provider can send its first MCP call just before the bridge consumes
        # session.started. Let that existing identity arrive; never invent one.
        deadline = time.monotonic() + 2
        while True:
            assert_life_turn_current(allow_control=True)
            context = memory_context(save_id)
            if context["companion_id"] or not os.environ.get("STARDEW_LIFE_TURN_ID") or time.monotonic() >= deadline:
                return context
            await asyncio.sleep(.05)

    async def execute_plan_operation(
        operation: str,
        params: dict[str, Any],
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one validated plan step through the real scheduler/wiki implementation.

        ``command_id`` is the stable plan command id persisted before dispatch. It
        is forwarded to the real scheduler method (and from there to the native
        Mod command) so the persisted id is the idempotency identity, not a label.
        Composite operations derive deterministic sub-command ids from it.
        """
        if operation == "plant_crop_workflow":
            raise ToolError(PLANT_WORKFLOW_PLAN_ERROR)
        entry = _PLAN_OPERATION_CALLS.get(operation)
        allowed = entry[1] if entry else frozenset()
        goal_constraints: dict[str, Any] = {}

        def _params_match(step_p: dict[str, Any], call_p: dict[str, Any]) -> bool:
            step_p = bind_execution_scope_params(operation, step_p or {}, goal_constraints)
            call_p = bind_execution_scope_params(operation, call_p or {}, goal_constraints)
            if step_p == call_p:
                return True
            if allowed:
                return {k: v for k, v in (step_p or {}).items() if k in allowed} == {k: v for k, v in (call_p or {}).items() if k in allowed}
            return False

        if operation not in {"cancel_task", "pause_task", "resume_task"}:
            sid = await current_save_id()
            state = work_for_run().state(sid)
            d = state.decision
            task = next((t for t in state.tasks if t.id == d.get("taskId")), None)
            goal = next((goal for goal in state.goals if task and goal.id == task.goal_id), None)
            goal_constraints = goal.constraints if goal is not None else {}
            params = bind_execution_scope_params(operation, params, goal_constraints)
            if not task or task.status == "cancelled" or state.paused or d.get("expires", 0) <= time.time() or d.get("finished") or not any(
                step.command_id == command_id and step.operation == operation and _params_match(step.params, params) and step.status == "running"
                for step in task.steps
            ):
                raise ToolError("UNAUTHORIZED_JOB_COMMAND: requires claimed step of this model-selected job; "
                    + str({"taskFound": bool(task), "taskStatus": task.status if task else None,
                           "paused": state.paused, "expired": d.get("expires", 0) <= time.time(),
                           "finished": d.get("finished"), "operation": operation,
                           "claimedSteps": [{"commandId": s.command_id, "operation": s.operation,
                                             "status": s.status, "paramsMatch": _params_match(s.params, params)}
                                            for s in task.steps] if task else []}))
        if operation == "query_wiki":
            query = str(params.get("query") or "")
            return await asyncio.to_thread(wiki_for_run().lookup, query)
        if entry is None:
            raise PolicyViolationError(f"operation '{operation}' is not an allowed plan operation")
        method_name, allowed = entry
        unknown = set(params) - set(allowed)
        if semantic_unknown := unknown - {"detail", "command_id"}:
            raise PolicyViolationError(f"INVALID_JOB_PARAMETERS: {operation} does not accept {sorted(semantic_unknown)}; nothing dispatched")
        if unknown:
            # Presentation detail and the legacy caller id are not execution
            # quantities or targets. The persisted step supplies command_id.
            params = {k: v for k, v in params.items() if k in allowed}
        method = getattr(sched, method_name, None)
        if method is None:
            raise SchedulerError(f"scheduler does not implement '{operation}'")
        call_params = dict(params)
        if operation == "get_status":
            # detail controls the MCP observation view, not native scheduling.
            detail = call_params.pop("detail", False)
            result = await method(**call_params)
            return detailed_status(result) if detail else result
        if command_id and _accepts_command_id(method):
            call_params["command_id"] = command_id
        return await method(**call_params)

    async def reconcile_native_command(command_id: str) -> dict[str, Any] | None:
        """Reconcile a persisted command id against the Mod's cached result.

        Read-only: never dispatches. Returns None when the native side has no
        terminal result for this id (which keeps the step ``unknown`` instead of
        pretending it succeeded or re-sending it).
        """
        if not command_id:
            return None
        try:
            return await sched.reconcile_command(command_id, timeout=0.2)
        except Exception:
            logger.debug("Reconcile failed for %s", command_id, exc_info=True)
            return None

    def classify_step_outcome(result: Any) -> tuple[str, str | None]:
        return _classify_step_outcome(result)

    def assert_life_turn_current(*, allow_control: bool = False, lease_locked: bool = False) -> None:
        request_id = os.environ.get("STARDEW_LIFE_TURN_ID")
        if not request_id:
            return  # Standalone read/testing MCP clients have no bridge lease.
        path = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "life-turn.json"
        try:
            lease = json.loads(path.read_text(encoding="utf-8")) if lease_locked else read_cache(path)
        except (OSError, ValueError) as ex:
            raise ToolError("STALE_CONVERSATION") from ex
        if lease.get("requestId") != request_id or lease.get("saveId") != os.environ.get("STARDEW_LIFE_SAVE_ID"):
            raise ToolError("STALE_CONVERSATION: newer player control takes precedence")
        blocked = lease.get("workBlocked", False)
        if not allow_control:
            # A committed stop takes effect for further plan mutations even if
            # the bridge has not consumed its inbox yet.
            for pending in sorted((path.parent / "life-controls").glob(request_id + "--*.json")):
                try:
                    control = json.loads(pending.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if control.get("action") in {"pause", "cancel", "resume"}:
                    blocked = control["action"] != "resume"
        if blocked and not allow_control:
            raise ToolError("WORK_STOPPED: this conversation paused or cancelled work; only an explicit resume can allow new work")

    async def current_save_id() -> str:
        if external_codex and external_selected and external_save_id:
            return external_save_id
        current = await sched.get_status()
        sid = current.get("saveId")
        expected_save = os.environ.get("STARDEW_LIFE_SAVE_ID")
        if expected_save and sid != expected_save:
            raise ToolError("SAVE_CHANGED: the active game save differs from this conversation")
        if not sid or sid == "unknown":
            raise SchedulerError("The connected Mod did not publish a current saveId.")
        return str(sid)

    async def validate_actor_operation(operation: str) -> None:
        if operation != "open_shop":
            return
        snapshot = sched.latest_snapshot
        payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
        if payload.get("actorMode") == "companion":
            raise ToolError(
                "PLAYER_MODE_ONLY: open_shop is unavailable for the companion. "
                "Use query_shop, navigate_to its observed counter, then purchase_items. No job selected."
            )

    async def record_observation_recovery(result: dict[str, Any], record) -> None:
        """Best-effort bookkeeping cannot turn a successful native read into failure.

        Bind recovery to the native result envelope's save, then verify the
        connected save still matches. Missing/unknown identities provide no
        recovery evidence; never guess from whichever save is now loaded.
        """
        observed_save = result.get("saveId")
        if not isinstance(observed_save, str) or observed_save in {"", "unknown", "unknown-save"}:
            return
        try:
            current = await sched.get_status()
            if current.get("saveId") == observed_save:
                record(observed_save)
        except Exception:
            logger.debug("Could not record observation recovery", exc_info=True)

    async def disable_autonomy_after_task_control() -> None:
        """Make pause/cancel stop future autonomous submissions for this save.

        The existing MCP scheduler remains the owner of the execution socket;
        ChatBridge observes this shared state and cancels its active autonomous
        chat task, which closes the agent's MCP connection and reaches Mod's
        existing transport-disconnect cancellation path.
        """
        try:
            sid = await current_save_id()
            autonomy_for_run().set_enabled(sid, False)
        except Exception:
            logger.debug("Could not update autonomy state after task control", exc_info=True)

    @mcp.tool()
    async def read_reload_history(offset: int = 0, limit: int = 12) -> dict[str, Any]:
        """Read original pre-reload conversation in pages; old action claims need fresh observation."""
        from .game_reload import read_reload_history as read_history
        sid = await current_save_id()
        return read_history(work_for_run().state_path.parent.parent, sid, offset, limit)

    def model_water_observation(result: dict[str, Any]) -> dict[str, Any]:
        """Life prompts and execution tools share the same native resource meaning."""
        if resolved_surface == "internal":
            return result  # Machine consumers retain the native protocol vocabulary.
        output = dict(result)
        companion = output.get("companion")
        if not isinstance(companion, dict):
            return output
        cached = getattr(sched, "latest_snapshot", None)
        payload = cached.get("payload") if isinstance(cached, dict) else None
        # A scheduler result owns its observed values. Only supplement omitted
        # overview fields from the exact same observation, never a newer cache
        # or another save/session that arrived while the tool was awaiting.
        native = dict(companion)
        same_observation = isinstance(cached, dict) and all(
            result.get(key) is not None and result.get(key) == cached.get(key)
            for key in ("saveId", "worldRevision")
        ) and (not result.get("gameSessionId") or
               result.get("gameSessionId") == cached.get("gameSessionId"))
        cached_companion = payload.get("companion") if isinstance(payload, dict) else None
        if same_observation and isinstance(cached_companion, dict):
            for key in ("waterCanLevel", "maxWaterCanLevel"):
                if key not in native:
                    native[key] = cached_companion.get(key)
        water = water_can_resources(native)
        output["companion"] = {key: value for key, value in companion.items()
                               if key not in {"waterCanLevel", "maxWaterCanLevel"}}
        output["companion"]["waterCan"] = water
        return output

    @mcp.tool()
    async def get_work_overview(detail: bool = False) -> dict[str, Any]:
        """Get consolidated farm work, companion backpack, and candidate chests overview.

        Default returns counts, backpack/chest summaries and current-priority work.
        detail=True includes exact coordinates, full work records and native effects.
        """
        try:
            if external_codex and external_selected and external_save_id:
                overview = work_for_run().overview(external_save_id)
                return {**(overview if detail or resolved_surface == "internal" else compact_work_overview(overview)), "gameSnapshotStale": True}
            native = await sched.get_work_overview(detail=detail)
            native = model_water_observation(native)
            if not detail:
                native["farmWork"] = {key: value for key, value in (native.get("farmWork") or {}).items() if not key.endswith("Tiles") and key != "matureCrops"}
                if isinstance(native.get("chests"), list):
                    native["chestCount"] = len(native["chests"])
                    native["chests"] = native["chests"][:5]
                native["detailsAvailable"] = True
            sid = native.get("saveId")
            if isinstance(sid, str):
                cached = sched.latest_snapshot
                payload = cached.get("payload") if isinstance(cached, dict) else None
                world = (payload or {}).get("world") or {}
                work = work_for_run().overview(sid, snapshot=payload,
                    game_date={"year": world.get("year"), "season": world.get("season"), "day": world.get("dayOfMonth")})
                native["work"] = work if detail else compact_work_overview(work)
            return native
        except SchedulerError as ex:
            raise ToolError(f"Failed to get work overview: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error getting work overview: {ex}") from None

    def detailed_status(result: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(result)
        # Reuse only sections actually published in the latest native snapshot.
        cached = getattr(sched, "latest_snapshot", None)
        payload = cached.get("payload") if isinstance(cached, dict) else None
        raw = payload if isinstance(payload, dict) else {}
        for section in ("inventory", "chests", "planting", "shop"):
            value = raw.get(section)
            if isinstance(value, dict):
                enriched[section] = value
        return enriched

    @mcp.tool()
    async def get_status(detail: bool = False) -> dict[str, Any]:
        """Get companion status (position, stamina, water, task) and world time/weather.

        ``detail=True`` keeps the real native sections the compact decision context
        reads (weather icon, companion wallet, backpack inventory, chests, planting,
        shop) instead of dropping them in the condensed view. Never invents a
        missing field: anything the Mod did not publish stays absent/unknown.
        After external submission, returns persisted work without reconnecting;
        detail=True includes full project/layout, step targets and native effects.
        """
        try:
            if external_codex and external_selected and external_save_id:
                overview = work_for_run().overview(external_save_id)
                return {**(overview if detail or resolved_surface == "internal" else compact_work_overview(overview)), "gameSnapshotStale": True}
            res = model_water_observation(await sched.get_status())
            if detail:
                return detailed_status(res)
            comp = res.get("companion", {})
            world = res.get("world", {})
            return {
                "actorMode": res.get("actorMode"),
                "actorMultiplayerId": res.get("actorMultiplayerId"),
                "controlOwner": res.get("controlOwner"),
                "farmWork": res.get("farmWork"),
                "companion": {
                    "locationId": comp.get("locationId"),
                    "tileX": comp.get("tileX", 0),
                    "tileY": comp.get("tileY", 0),
                    "activity": comp.get("activity", "idle"),
                    "stamina": comp.get("stamina"),
                    "maxStamina": comp.get("maxStamina"),
                    "waterCan": comp.get("waterCan"),
                    "currentTask": comp.get("currentTask"),
                    # Real thin-observer wallet; absent stays absent/unknown.
                    "availableMoney": comp.get("availableMoney"),
                    "moneyStatus": comp.get("moneyStatus"),
                },
                "world": {
                    "timeOfDay": world.get("timeOfDay", 600),
                    "isRaining": world.get("isRaining", False),
                    # Real native weather icon (isRaining=false is not "clear").
                    "weatherIcon": world.get("weatherIcon"),
                },
                "saveId": res.get("saveId", "unknown"),
                "worldRevision": res.get("worldRevision", 0),
                "observedAt": res.get("observedAt"),
            }
        except SchedulerError as ex:
            raise ToolError(f"Failed to get status: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error getting status: {ex}") from None

    @mcp.tool()
    async def query_farm_work(detail: bool = False) -> dict[str, Any]:
        """Query observed Farm work, independent of companion location. Unobserved counts are unknown; detail=True includes tile lists."""
        try:
            res = await sched.query_farm_work()
            if detail:
                return res
            fw = res.get("farmWork", {})
            companion_location = (res.get("companion") or {}).get("locationId")
            return {
                "farmWork": {
                    "locationId": fw.get("locationId"),
                    "observationStatus": fw.get("observationStatus", "unknown"),
                    "matureCropCount": fw.get("matureCropCount"),
                    "tilledUnwateredCount": fw.get("tilledUnwateredCount"),
                    "cropUnwateredCount": fw.get("cropUnwateredCount"),
                    "isTruncated": fw.get("isTruncated", False),
                },
                "currentCompanionLocationId": companion_location,
                "needsNavigation": companion_location.casefold() != "farm" if isinstance(companion_location, str) else None,
                "worldRevision": res.get("worldRevision", 0),
            }
        except SchedulerError as ex:
            raise ToolError(f"Failed to query farm work: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error querying farm work: {ex}") from None

    @mcp.tool()
    async def autonomy_status(save_id: str | None = None) -> dict[str, Any]:
        """Read player-controlled autonomy state for one save; disabled by default."""
        sid = await current_save_id()
        if save_id is not None and save_id != sid:
            raise ToolError("save_id must match the currently connected save")
        state_dict = dict(autonomy_for_run().state(sid).__dict__)
        state_dict.update({
            "failureCount": state_dict.get("failure_count", 0),
            "breakerTripped": state_dict.get("breaker_tripped", False),
            "breakerCooldownUntil": state_dict.get("breaker_cooldown_until"),
            "breakerReason": state_dict.get("breaker_reason"),
        })
        return {"saveId": sid, **state_dict}

    @mcp.tool()
    async def set_autonomy(
        enabled: bool | None = None,
        save_id: str | None = None,
        goal: str | None = None,
        box_preference: str | None = None,
        goal_id: str | None = None,
        idle_preference: Literal["autonomous", "clear", "forage", "wood", "wait"] | None = None,
    ) -> dict[str, Any]:
        """Control idle initiative, or bind a player-authorized ongoing goal.

        Pass goal_id alone to continue that active assignment without changing
        idle initiative. enabled changes only the optional idle-help preference.
        Explicit jobs execute in either mode. Pause is independent and preserved;
        a direct player resume instruction can resume through manage_companion.
        Purchases follow the player goal and available funds; no daily allowance is required.
        idle_preference is an optional preference: autonomous, clear, forage, wood or wait.
        """
        nonlocal external_selected, external_save_id, external_handed_off
        sid = await current_save_id()
        if save_id is not None and save_id != sid:
            raise ToolError("save_id must match the currently connected save")
        try:
            autonomy = autonomy_for_run()
            store = work_for_run()
            if goal_id is not None and not any(g.id == goal_id and g.status == "active" for g in store.state(sid).goals):
                raise ToolError("goal_id must identify an active saved goal")
            was_paused = autonomy.state(sid).paused or store.state(sid).paused
            autonomy.set_preferences(sid, goal=goal,
                                     box_preference=box_preference, idle_preference=idle_preference)
            if (enabled or goal_id) and external_codex:
                # Explicitly transfer the sole native socket to the bridge before
                # enabling its next model decision. Keep already selected work.
                await sched.close()
                decision = store.state(sid).decision
                if decision.get("token") == external_token and not decision.get("selected"):
                    store.revoke_decision(sid)
                external_selected = True
                external_save_id = sid
                external_handed_off = True
            # Enable, scope and pause travel in one durable mutation: no snapshot
            # can observe enabled global work before the project scope is bound.
            state = autonomy.set_enabled(sid, autonomy.state(sid).enabled if enabled is None else enabled, goal_scope=goal_id, paused=was_paused)
            state_dict = dict(state.__dict__)
            state_dict.update({
                "failureCount": state_dict.get("failure_count", 0),
                "breakerTripped": state_dict.get("breaker_tripped", False),
                "breakerCooldownUntil": state_dict.get("breaker_cooldown_until"),
                "breakerReason": state_dict.get("breaker_reason"),
            })
            return {"saveId": sid, **state_dict}
        except ValueError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def work_plan_overview(detail: bool = False) -> dict[str, Any]:
        """Relevant-only work memory: active goals, current short tasks, next step, waiting todos, anomalies.

        Use this once instead of repeatedly listing tasks and re-querying status. It
        reads only the latest cached snapshot; it never issues a new farm query.
        Default omits full layout, step parameters and effect coordinates. Use
        detail=True for those exact facts, especially before recovering partial or
        unknown work; omission never means a target was completed or should retry.
        """
        # After an external Codex selects work, the bridge worker needs the
        # Mod's sole command socket. Read its persisted result without reopening it.
        sid = external_save_id if external_codex and external_save_id else await current_save_id()
        store = work_for_run()
        decisions = store.recover(sid)
        snapshot = sched.latest_snapshot
        payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
        world = payload.get("world", {}) if isinstance(payload, dict) else {}
        game_date = {
            "year": world.get("year"),
            "season": world.get("season"),
            "day": world.get("dayOfMonth"),
        }
        overview = store.overview(sid, snapshot=payload if payload else None, game_date=game_date)
        overview["dueTodos"] = store.evaluate_todos(sid, snapshot=snapshot, game_date=game_date)
        overview["recoveryDecisions"] = decisions
        return overview if detail or resolved_surface == "internal" else compact_work_overview(overview)

    @mcp.tool()
    async def request_player_decision(message: str, goal_id: str | None = None,
                                      choices: Annotated[list[str] | None, Field(min_length=2, max_length=6)] = None) -> dict[str, Any]:
        """Notify the player explicitly about a needed decision or significant discussion.

        A goal is optional; questions do not require creating an active goal.
        Routine progress belongs in the execution log. Identical notices deduplicate
        across restarts. This never grants permission or unpauses work.
        delivered=False on creation means queued, not failed; the bridge delivers
        it on a following game snapshot and records delivered=True.
        """
        sid = await current_save_id()
        try:
            return work_for_run().request_player_decision(sid, goal_id, message, choices)
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def manage_goal(
        action: Literal["create", "revise", "pause", "resume", "complete", "finish", "cancel", "list"],
        goal_id: str | None = None,
        text: str | None = None,
        priority: int | None = None,
        constraints: dict[str, Any] | None = None,
        source: Literal["agent"] = "agent",
        project: dict[str, Any] | None = None,
        kind: Literal["main", "stage", "daily"] | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """Manage projects; creation defaults to a completable stage, even for the first project.

        Model-created goals are recorded with source=agent; the model cannot label a
        goal as a user instruction. Player-authored goals come from the chat path.
        project is a partial update that preserves omitted fields. It stores the current plan: phase, nextAction, blocker, prerequisites, window,
        progress, reason and farmRegion (locationId, bounds, reservedPaths, expansionDirection).
        Keep summary concise; revise after actual results. It does not grant execution authority.
        It never grants authority or proves completion; assess completion from current game observations.

        ``kind`` classifies the goal: 'main' is the single long-running main goal
        (only one active main goal is allowed; preset-sourced goals are 'main'),
        'stage' is the default for model-created finite projects (with or without a parent), 'daily' marks
        day-scoped work; ``parent_id`` links a goal to its main goal. Speculative
        conclusions must NOT be written as goals or constraints — mark them as
        speculation with the memory tools instead.

        complete on a 'main' goal is refused with MAIN_GOAL_LOCKED (returned as a
        friendly blocked result, not an error): a long-term main goal ends only at
        a run boundary (preset boundary or an explicit player instruction); day to
        day, record progress and plan the next stage instead of closing it.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            if action == "list":
                return {"saveId": sid, "goals": store.list_goals(sid)}
            if action == "create":
                if source != "agent":
                    raise WorkStateError("model-created goals must use source='agent'")
                goal_kwargs: dict[str, Any] = {}
                if kind is not None or parent_id is not None:
                    # Forwarded to the work store's goal-kind contract; the store
                    # rejects a second active 'main' goal (MAIN_GOAL_EXISTS).
                    goal_kwargs = {"kind": kind, "parent_id": parent_id}
                goal = store.add_goal(
                    sid, text or "", source="agent", priority=priority or 0, constraints=constraints,
                    **goal_kwargs,
                )
                if project is not None:
                    goal = store.revise_goal(sid, goal.id, project=project)
                return {"saveId": sid, "goal": asdict(goal)}
            if action == "revise":
                current = next((g for g in store.state(sid).goals if g.id == goal_id), None)
                advisory = False
                if current is not None and current.kind == "main":
                    if constraints is not None and constraints != current.constraints:
                        raise WorkStateError("MAIN_CONSTRAINTS_LOCKED: model notes cannot replace the main authorization")
                    if text is not None and text.strip() != current.text:
                        project = {**(project or {}), "reason": text}
                        text = None
                        advisory = True
                goal = store.revise_goal(
                    sid, goal_id or "", text=text, priority=priority, constraints=constraints, project=project
                )
                return {"saveId": sid, "goal": asdict(goal),
                        "advisoryTextStoredAs": "project.reason" if advisory else None}
            if action in {"pause", "resume"}:
                goal = store.revise_goal(
                    sid, goal_id or "", status="paused" if action == "pause" else "active"
                )
                return {"saveId": sid, "goal": asdict(goal)}
            if action in {"complete", "finish"}:
                try:
                    observation = None
                    if resolved_surface == "light":
                        current = await sched.get_status()
                        if current.get("saveId") != sid or current.get("companion", {}).get("currentTask"):
                            raise ToolError("Observe the current game's remaining work after its action stops.")
                        observation = {"worldRevision": current.get("worldRevision"), "world": current.get("world", {})}
                    goal = store.complete_goal(sid, goal_id or "", completion_observation=observation)
                except Exception as ex:
                    if "MAIN_GOAL_LOCKED" in str(ex):
                        return {"saveId": sid, "goalId": goal_id, "blocked": True,
                                "reasonCode": "MAIN_GOAL_LOCKED",
                                "message": "长期主目标（kind='main'）只能由 run 边界终结"
                                           "（preset 边界或玩家明确指令）；日常请记录进度并规划下一阶段，"
                                           "不要 complete 主目标。"}
                    raise
                return {"saveId": sid, "goal": asdict(goal), "assessment": "model-reviewed; native log unchanged"}
            if action == "cancel":
                goal = store.cancel_goal(sid, goal_id or "")
                return {"saveId": sid, "goal": asdict(goal)}
            raise WorkStateError(f"unsupported goal action '{action}'")
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def manage_plan(
        goal_id: str,
        tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a structured short plan for a goal, or list its tasks.

        Each task has id/title/completionCondition/dependencies and steps; every step
        is an operation name plus structured params, validated against the allowed
        scheduler operations. Dependency cycles are rejected.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            if tasks:
                created = store.create_plan(sid, goal_id, tasks)
                return {"saveId": sid, "tasks": [asdict(t) for t in created]}
            return {"saveId": sid, "tasks": store.list_tasks(sid, goal_id)}
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def manage_todo(
        action: Literal["create", "list", "complete", "cancel", "due"],
        todo_id: str | None = None,
        intent: str | None = None,
        trigger: dict[str, Any] | None = None,
        goal_id: str | None = None,
        expiry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create/list/complete/cancel cross-day todos, or evaluate which are due now.

        Triggers: calendar (year/season/day), gameTime (year/season/day/timeOfDay HHMM),
        inventory (itemId/minCount), crop (cropId/state), resource (resource=stamina|water,
        minAmount), machineReady (locationId/tile={x,y}; observe_machines first).
        Due evaluation uses only the latest native snapshot and date; the
        stored goal memory is never treated as the latest inventory fact.
        Complete a handled todo and create its next cycle only when needed. Keep
        continuous production goals active between days; goal complete ends all their todos.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            if action == "list":
                return {"saveId": sid, "todos": store.list_todos(sid)}
            if action == "create":
                todo = store.add_todo(
                    sid,
                    intent=intent or "",
                    trigger=trigger or {},
                    goal_id=goal_id,
                    expiry=expiry,
                )
                return {"saveId": sid, "todo": asdict(todo)}
            if action in {"complete", "cancel"}:
                todo = store.complete_todo(
                    sid, todo_id or "", "done" if action == "complete" else "cancelled"
                )
                return {"saveId": sid, "todo": asdict(todo)}
            if action == "due":
                snapshot = sched.latest_snapshot
                payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
                world = payload.get("world", {}) if isinstance(payload, dict) else {}
                game_date = {
                    "year": world.get("year"),
                    "season": world.get("season"),
                    "day": world.get("dayOfMonth"),
                }
                return {
                    "saveId": sid,
                    "due": store.evaluate_todos(sid, snapshot=snapshot, game_date=game_date),
                }
            raise WorkStateError(f"unsupported todo action '{action}'")
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def manage_companion(
        action: Literal["pause", "resume", "cancel", "bedtime"], bedtime: int | None = None,
        goal_id: str | None = None, all_arrangements: bool = False,
    ) -> dict[str, Any]:
        """Apply an explicit player work control or remember the requested bedtime.

        Only call for the player's current instruction. cancel withdraws authorization
        for the arrangement and goal permanently; pause temporarily stops work and
        keeps it resumable. Never replace an explicit cancellation with pause.
        goal_id and all_arrangements apply ONLY to cancel. For pause/resume omit
        both: these controls affect the work scheduler globally, not one goal.
        Do not withdraw other arrangements just because a new task was assigned;
        only cancel the scope the player explicitly requested.
        To cancel a named arrangement read get_work_overview and bind its exact goal_id;
        do not cancel an unrelated current goal. all_arrangements is only for an
        explicit request to cancel all work. Omitted goal_id means current work.
        Negated, quoted or hypothetical cancellation is not a control instruction.
        resume cannot restore a cancelled arrangement; it requires a new assignment.
        resume includes 'start now'
        while paused. bedtime is the COMPANION IN-BED DEADLINE, HHMM 1800..2500 (midnight 2400), ten-minute steps. Finish work and walk home beforehand. The player chooses their own sleep time; never promise to send the player home. In player mode (actorMode=player) there is no companion bedtime at all: sleeping is managed by the sleep tool/native sleep flow, so bedtime is refused there.
        The bridge applies this intent without letting chat own native execution.
        """
        if not os.environ.get("STARDEW_LIFE_TURN_ID"):
            raise ToolError("This tool requires a current player message.")
        sid = await current_save_id()
        lease_path = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "life-turn.json"
        # Serialize authority validation and durable writes with bridge revocation.
        with cache_lock(lease_path):
            assert_life_turn_current(allow_control=True, lease_locked=True)
            if (goal_id or all_arrangements) and action != "cancel":
                raise ToolError("Cancellation scope belongs only to action=cancel.")
            if goal_id and all_arrangements:
                raise ToolError("Choose a specific goal_id or all_arrangements, not both.")
            from stardew_ai_runtime.work_controls import explicit_cancel_scope
            request_id = os.environ.get("STARDEW_LIFE_TURN_ID", "")
            if not request_id or not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
                raise ToolError("No active player conversation.")
            lease = json.loads(lease_path.read_text(encoding="utf-8"))
            if action == "pause" and explicit_cancel_scope(str(lease.get("playerText") or "")) is not None:
                raise ToolError("CANCEL_NOT_PAUSE: explicit withdrawal requires action=cancel.")
            if action == "bedtime":
                snapshot = getattr(sched, "latest_snapshot", None)
                payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
                actor_mode = payload.get("actorMode") if isinstance(payload, dict) else None
                if actor_mode == "player":
                    raise ToolError(
                        "玩家模式下没有伙伴作息：睡觉由 sleep 工具/原生睡眠流程管理，"
                        "bedtime 只适用于伙伴模式。"
                    )
                if isinstance(bedtime, bool) or not isinstance(bedtime, int):
                    raise ToolError("Bedtime must be HHMM.")
                bedtime = bedtime + 2400 if 0 <= bedtime <= 100 else bedtime
                if not 1800 <= bedtime <= 2500 or bedtime % 100 >= 60 or bedtime % 10:
                    raise ToolError("Bedtime must be 18:00..01:00 in ten-minute steps.")
            directory = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "life-controls"
            directory.mkdir(parents=True, exist_ok=True)
            # time_ns has finite clock resolution on Windows. A tied timestamp must
            # not let the random suffix reverse two sequential player controls.
            stamps = [int(p.stem[len(request_id) + 2:].split("-", 1)[0])
                      for p in directory.glob(request_id + "--*.json")]
            stamp = max(time.time_ns(), max(stamps, default=0) + 1)
            path = directory / (request_id + "--" + str(stamp) + "-" + uuid.uuid4().hex + ".json")
            intent = {"saveId": sid, "requestId": request_id, "action": action, "bedtime": bedtime,
                      "goalId": goal_id, "allArrangements": all_arrangements}
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
            return {"accepted": True, "action": action, "bedtime": bedtime,
                    "status": "queued", "nativeOutcomeConfirmed": False,
                    "feedbackInstruction": "This accepts a control intent, not a native result. Before reporting current counts after pause/cancel/resume, read get_work_overview for fresh observedFacts; if a native command is still unconfirmed, do not invent its final result."}

    @mcp.tool()
    async def manage_milestones(
        action: str,
        node_id: Annotated[str | None, Field(description="Current node ID for write actions; with action='list', returns this node's complete saved scope without list/coordinate truncation.")] = None,
        title: str | None = None,
        target_date: str | None = None,
        summary: str | None = None,
        source_url: str | None = None,
        reserved_funds: int | None = None,
        planned_count: int | None = None,
        terms_note: str | None = None,
        reason: str | None = None,
        preparation: Annotated[list[str] | None, Field(description="For custom propose/revise nodes, capability keys: travel, water, harvest, clear, plant, animals, machines, store, ship, pickup, layout, production. Use travel for a player-assigned trip to a specified map and arrival tile. On revise, omitted/null keeps current preparation; a list replaces it and [] clears it, synchronizing the adopted goal and todos. Fixed catalog nodes do not accept preparation changes.")] = None,
        execution_scope: Annotated[dict[str, Any] | None, Field(description="Optional exact observed candidate scope: {locationId:'Farm',tiles:[{x:69,y:18},{x:64,y:23}]} (integer [x,y] pairs also accepted). On revise, omitted/null keeps the saved scope; {} clears it; locationId plus tiles:[] saves an explicit empty candidate range. Candidate tile count is independent of planned_count/seed count; keep a fixed saved range instead of expanding it to consume more seeds. Read the full saved range with action='list', node_id='<node.id>'.", json_schema_extra=_EXECUTION_SCOPE_SCHEMA)] = None,
        id: str | None = None,
        nodeId: str | None = None,
    ) -> dict[str, Any]:
        """Discuss and save milestone nodes in chat or plan. propose saves a suggestion; adopt/revise require player agreement, neither mode grants approval. A clear instruction or "you decide" permits propose and adopt together; a question alone does not. Use the observed "node_id" for adopt/revise/defer/reopen; list with node_id reads its full scope. Saving does not execute actions.

        Facts come from the official English Wiki with sourceUrl on every node.
        Actions: list (always allowed), propose/adopt/revise/defer/reopen (write
        actions, allowed during the unified player conversation (chat or plan)).
        propose records an UNAPPROVED suggestion; adopt and changing accepted
        work require explicit player agreement this turn. Neither mode grants
        approval by itself. Only say the plan is saved after this tool confirms it.

        Use node_id for adopt/revise/defer/reopen, copied from the current node's
        id. Example: {"action":"adopt","node_id":"spring-egg-festival-strawberry:y1",
        "planned_count":5}. id and nodeId are compatibility aliases; conflicting
        values are rejected. Do not guess another parameter spelling.
        The successful response already contains the authoritative changed node,
        revision, and manual/capability preparation: do not list again just to
        confirm that write. Saving is not proof that physical work has run.

        For a custom proposal or revision, preparation lists existing capabilities:
        travel (only navigate to the player-specified destination and report arrival),
        water, harvest, clear, plant (existing seeds), animals, machines, store,
        ship (explicitly approved sale items only), pickup, layout (persistent multi-day design/construction) or production (ongoing farming/husbandry including necessary seed purchases, coop construction, animal acquisition and feed). A player assignment such as "you handle crops and animals" MUST use production, never downgrade it into today-only water/animals. Summary must retain
        location, scope and protected items. For today's ordinary work, omit
        target_date to use the observed game date; no festival node is required.
        A pure walking assignment MUST use travel so adoption creates executable work.
        Keep the destination map and arrival tile in summary and execution_scope.
        When changing accepted work, revise the existing node with its current
        scope: update title and terms_note if those old descriptions no longer
        apply, and preparation if the required capabilities change. Omitting
        preparation keeps the existing items; an explicit list replaces them,
        and [] clears them. These changes synchronize the adopted goal/todos.
        Existing arrangements may be revised or deferred while paused: keep
        work paused until the updated scope is saved and the player agrees to resume.
        Fixed catalog nodes keep their preparation facts. A failed second adopt
        returns recommendedAction=revise and acceptedFields, including preparation
        and execution_scope. execution_scope records the observed locationId and
        exact candidate tiles; integer [x,y] pairs are also accepted. These are
        candidate positions, not a requirement to consume one seed per position.
        planned_count records the agreed seed/work quantity separately. Retain a
        fixed candidate range rather than expanding it to satisfy planned_count.
        On revise, omitted/null keeps execution_scope and {} clears it. Use
        {"action":"list","node_id":"<node.id>"} to read the complete saved node
        and exact coordinates if the injected goal context is abbreviated.
        Infer a modest proposal from the live snapshot; budget/count are optional,
        never ask the player to fill internal parameters. A clear instruction or "you decide" authorizes propose and adopt in the SAME turn; a question or preference alone does not.
        adopt persists the node and wires each ``capability`` prep item into the
        work system as a user goal plus calendar todos (lead time before the
        target day, expiry on the target day); ``manual`` prep items stay with the
        player and must be stated as such. reserved_funds is a ONE-TIME
        planning reminder only (does not freeze money), recorded in goal constraints — never a per-day purchase
        budget. defer cancels those todos and pauses the goal. This tool never
        dispatches work: no submit_plan, no begin_decision, no scheduler actions.
        """
        sid = await current_save_id()
        store = milestones_for_run()
        snapshot = sched.latest_snapshot
        payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
        world = payload.get("world", {}) if isinstance(payload, dict) else {}
        game_date = None
        if world.get("year") is not None and world.get("season") and world.get("dayOfMonth") is not None:
            game_date = {
                "year": world.get("year"),
                "season": world.get("season"),
                "day": world.get("dayOfMonth"),
            }
        action_clean = (action or "").strip().lower()
        identifiers = {value.strip() for value in (node_id, id, nodeId) if value and value.strip()}
        if len(identifiers) > 1:
            raise ToolError("CONFLICTING_NODE_ID: node_id/id/nodeId disagree; pass only node_id from the current node snapshot.")
        node_id = next(iter(identifiers), None)
        if action_clean == "list":
            play_style = None
            try:
                profile_res = profile_for_run().get(sid)
            except Exception:
                profile_res = None
            if isinstance(profile_res, dict):
                profile = profile_res.get("profile")
                if isinstance(profile, dict):
                    play_style = profile.get("playStyle")
            if node_id:
                by_id = {node["id"]: node for node in store.list_nodes(sid) if node.get("id")}
                for node in store.suggest(game_date, play_style):
                    by_id.setdefault(node["id"], node)
                node = by_id.get(node_id)
                nodes = [node] if node is not None else []
            else:
                nodes = store.merged_nodes(sid, game_date, play_style)
            return {
                "saveId": sid,
                "revision": store.revision(sid),
                "nodes": [wire_node(n) for n in nodes],
            }
        lease_path = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "life-turn.json"
        # Serialize authority validation and durable writes with bridge revocation.
        with cache_lock(lease_path):
            # Editing or withdrawing an existing arrangement must not require
            # restarting the old native job. The turn lease still rejects stale
            # callers, and these writes do not clear the persisted pause state.
            assert_life_turn_current(allow_control=action_clean in {"revise", "defer"}, lease_locked=True)
            if action_clean not in {"propose", "adopt", "revise", "defer", "reopen"}:
                raise ToolError(
                    f"unsupported milestones action '{action}'; use list|propose|adopt|revise|defer|reopen"
                )
            if os.environ.get("STARDEW_LIFE_MODE") not in {"chat", "plan"}:
                raise ToolError(
                    "LIFE_CONVERSATION_REQUIRED: 修改节点需要当前伙伴对话（chat 或 plan）；"
                    "采纳或修改已接受的安排需玩家明确授权，模式本身不代表授权。"
                )
            # The conversation model judges player agreement; this gate only checks
            # the conversation context, alongside the current-turn/stop checks above.
            if action_clean != "propose" and not node_id:
                raise ToolError(
                    'NODE_ID_REQUIRED: use {"action":"' + action_clean + '","node_id":"<node.id>"}; '
                    "copy id from the injected node snapshot or manage_milestones(action='list')."
                )

            def saved(node: dict[str, Any]) -> dict[str, Any]:
                if node.get("status") == "adopted" and node.get("goalId"):
                    turn_id = os.environ.get("STARDEW_LIFE_TURN_ID")
                    if turn_id:
                        work_store = work_for_run()
                        known_request = any(r.get("id") == turn_id for r in work_store.state(sid).requests)
                        if not known_request:
                            lease = json.loads(lease_path.read_text(encoding="utf-8"))
                            player_text = lease.get("playerText")
                            if isinstance(player_text, str) and player_text.strip():
                                work_store.record_request(sid, turn_id, player_text, origin="player")
                                known_request = True
                        if known_request:
                            work_store.bind_goal_request(sid, node["goalId"], turn_id)
                        # Legacy leases omit playerText and carry no request
                        # ledger. Preserve that supported authorization path
                        # without fabricating a player instruction or binding.
                # One complete node is sufficient to acknowledge the write. Omit
                # absent values, not preparation ownership or observed status.
                compact = {key: value for key, value in wire_node(node).items() if value is not None}
                compact["prepItems"] = [
                    {key: value for key, value in prep.items() if value is not None}
                    for prep in compact.get("prepItems", [])
                ]
                return {"saveId": sid, "revision": store.revision(sid), "saved": True,
                        "execution": "not_started_by_this_tool", "node": compact}

            try:
                if action_clean == "propose":
                    node = store.propose(
                        sid,
                        title=title or "",
                        target_date=target_date or (f"{game_date['year']}:{game_date['season']}:{game_date['day']}" if game_date else ""),
                        summary=summary,
                        source_url=source_url,
                        preparation=preparation,
                        execution_scope=execution_scope,
                        reserved_funds=reserved_funds,
                        planned_count=planned_count,
                        terms_note=terms_note,
                        game_date=game_date,
                    )
                    return saved(node)
                if action_clean == "adopt":
                    node = store.adopt(
                        sid,
                        node_id or "",
                        reserved_funds=reserved_funds,
                        planned_count=planned_count,
                        terms_note=terms_note,
                        work_store=work_for_run(),
                        game_date=game_date,
                    )
                    return {
                        **saved(node),
                        "goalId": node.get("goalId"),
                        "todoIds": node.get("todoIds"),
                    }
                if action_clean == "revise":
                    node = store.revise(
                        sid,
                        node_id or "",
                        title=title,
                        summary=summary,
                        reserved_funds=reserved_funds,
                        planned_count=planned_count,
                        terms_note=terms_note,
                        preparation=preparation,
                        execution_scope=execution_scope,
                        game_date=game_date,
                        target_date=target_date,
                        work_store=work_for_run(),
                    )
                    return saved(node)
                if action_clean == "defer":
                    node = store.defer(
                        sid,
                        node_id or "",
                        reason=reason,
                        work_store=work_for_run(),
                        game_date=game_date,
                    )
                    return saved(node)
                node = store.reopen(
                    sid,
                    node_id or "",
                    reason=reason,
                    work_store=work_for_run(),
                    game_date=game_date,
                )
                return saved(node)
            except MilestoneError as ex:
                return {"saveId": sid, "error": str(ex), "execution": "not_started_by_this_tool",
                        "saved": False, **getattr(ex, "details", {})}

    @mcp.tool()
    async def submit_plan(
        tasks: Annotated[list[ShortJobTask], BeforeValidator(_unwrap_item_array), Field(min_length=1, max_length=1, description="Exactly one current job with sequential native operations; no task dependencies or future waits.")],
        goal_id: str | None = None,
        goal_text: str | None = None,
        replace: bool = False,
        detail: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue sequential game operations for the current model turn.

        Further calls in the same turn append to its pending job for the same
        goal and player request. Execution begins after the turn returns;
        a dispatched job cannot be edited through this tool.

        One task may combine sequential navigation, inventory transfers,
        preparation, planting and exact-tile watering across maps. Native map,
        resource and ownership checks apply to every operation; partial, failed
        or unknown results stop the job for a new decision. Future waits belong
        in remembered todos. Selection acknowledges work; it does not prove effects.
        Default acknowledges task ids/states without echoing full submitted targets;
        detail=True returns the full persisted task. It never means execution succeeded.

        Prefer this over the legacy ``manage_plan`` tool. Exact shape::

            tasks = [
              {"id": "t1", "title": "浇水", "completionCondition": "选定的两株作物已浇水",
               "dependencies": [], "steps": [
                 {"operation": "water_auto", "params": {"max_tiles": 2,
                  "target_tiles": [{"x": 62, "y": 27}, {"x": 66, "y": 23}]}}]}
            ]

        Example coordinates must be replaced with freshly observed candidates.
        For water_auto, max_tiles limits one batch; target_tiles optionally limits
        its location scope. A successful batch can be followed by refill/watering
        steps even when crops remain. goalSatisfied reports the wider scope.

        Every ``operation`` must be one of the discoverable plan operations; an
        unknown operation is rejected with the allowed list. ``params`` should
        contain only that operation's documented keys; unknown keys are rejected
        before any job is saved or executed. Pass an existing
        active ``goal_id`` from context or ``goal_text`` to resolve/create an agent goal.
        Both may be omitted when the decision has one bound active goal. With
        a queued player instruction, pass its request_id from the delivery facts
        to attach this job and its eventual results to that original instruction.
        Otherwise the current decision's original request owns the job. With
        ``replace=True`` the goal's still-pending tasks are superseded; work already
        running or unknown is preserved. A settled partial may be explicitly
        superseded with replace=True after inspecting its effects; this cancels
        its remaining work while retaining the partial native evidence. Call
        discover_capabilities("memory") for the same schema.
        """
        nonlocal external_selected
        sid = await current_save_id()
        store = work_for_run()
        try:
            normalized_tasks = [task.model_dump(exclude_none=True) if isinstance(task, ShortJobTask)
                                else ShortJobTask.model_validate(task).model_dump(exclude_none=True)
                                for task in tasks]
            for task in normalized_tasks:
                for step in task.get("steps", []):
                    await validate_actor_operation(step["operation"])
                    entry = _PLAN_OPERATION_CALLS.get(step["operation"])
                    if entry and (unknown := set(step.get("params") or {}) - entry[1]):
                        raise WorkStateError(
                            f"INVALID_JOB_PARAMETERS: {step['operation']} does not accept {sorted(unknown)}; "
                            f"use its documented keys {sorted(entry[1])}. No job selected."
                        )
                    if step["operation"] == "navigate_to" and (step.get("params") or {}).get("tile") is None:
                        params = step.get("params") or {}
                        snapshot = sched.latest_snapshot
                        payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
                        shop = payload.get("shop") if isinstance(payload, dict) else None
                        if (isinstance(shop, dict) and isinstance(shop.get("interactionTile"), dict)
                                and str(shop.get("locationId") or "SeedShop").casefold()
                                == str(params.get("location_id") or "").casefold()):
                            params["tile"] = shop["interactionTile"]
                        else:
                            raise WorkStateError(
                                "INVALID_JOB_PARAMETERS: navigate_to needs an observed destination tile. "
                                "Omitting it only works for the shop with a current native interactionTile; "
                                "a map name or guessed landmark alone is insufficient. No job selected."
                            )
                    if step["operation"] in {"build_building", "upgrade_building", "purchase_animal"} and (step.get("params") or {}).get("location_id", "Farm") != "Farm":
                        raise WorkStateError(
                            "INVALID_JOB_PARAMETERS: building and animal services use location_id='Farm' for the target building, "
                            "not ScienceHouse/AnimalShop. Navigate to the service counter separately. No job selected."
                        )
                    if entry:
                        step["params"] = validate_base_arguments(step["operation"], step.get("params") or {}, for_plan=True)
            result = store.submit_plan(
                sid,
                tasks=normalized_tasks,
                goal_id=goal_id,
                goal_text=goal_text,
                replace=replace,
                decision_token=decision_token(),
                request_id=request_id,
            )
            if external_codex:
                external_selected = True
                await sched.close()
            if not detail and resolved_surface != "internal":
                result = {**result, "tasks": [_compact_plan_task(task) for task in result.get("tasks", [])],
                          "detailsAvailable": True,
                          "detailHint": "Read work_plan_overview(detail=True) for stored targets and effects; do not resubmit to retrieve details."}
            handoff = ({"effectStatus": "not_executed_yet", "canAppendBeforeDispatch": True,
                        "executionStarts": "after_current_turn", "handoff": "ack_async"}
                       if not external_codex else {})
            return {"saveId": sid, "executionScope": "one_short_job", **result, **handoff}
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def answer_player_decision(notice_id: str) -> dict[str, Any]:
        """Resolve the linked question only when the current player's message answers it.

        No extra confirmation is needed. Unrelated chat or a clarifying question does not resolve it.
        The bridge stores the original player text; do not invent or paraphrase their answer.
        """
        directory = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data"
        with cache_lock(directory / "life-turn.json"):
            assert_life_turn_current(allow_control=True, lease_locked=True)
            lease = json.loads((directory / "life-turn.json").read_text(encoding="utf-8"))
            if (not os.environ.get("STARDEW_LIFE_TURN_ID") or lease.get("noticeId") != notice_id
                    or not lease.get("playerText")):
                raise ToolError("DECISION_REPLY_NOT_LINKED: this turn is not an answer to that question")
            work_for_run().answer_player_notice(lease["saveId"], notice_id, lease["playerText"], lease["requestId"])
        return {"answeredNoticeId": notice_id}

    @mcp.tool()
    async def record_decision_note(goal_id: str, note: str, category: Literal["observation", "hypothesis", "intent", "failure"], location_id: str,
                                   basis_command_id: str | None = None) -> dict[str, Any]:
        """Keep a dated note on an active goal. An optional basis_command_id links
        native evidence; notes without that evidence remain unverified model
        interpretation. Intent grants no extra work authorization. Current game
        observations override old notes. Use 1..800 characters.
        """
        sid = await current_save_id()
        directory=Path(getattr(sched,"run_dir",None) or run_dir or ".")/"data"
        from .snapshot_cache import cache_lock
        with cache_lock(directory/"life-turn.json"):
            assert_life_turn_current(lease_locked=True)
            try:
                return work_for_run().record_decision_note(sid,goal_id,note,category,location_id,basis_command_id)
            except WorkStateError as ex:
                raise ToolError(str(ex)) from None

    async def save_companion_note(text: str, *, kind: str, scope: str | None, title: str = "",
                                  note_id: str | None = None, supersedes: list[str] | None = None,
                                  valid_on: str | None = None) -> dict[str, Any]:
        sid = await current_save_id()
        context = await ready_memory_context(sid)
        directory = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data"
        with cache_lock(directory / "life-turn.json"):
            assert_life_turn_current(allow_control=True, lease_locked=True)
            try:
                status, result = memory_for_run().save_note(
                    sid, text, **context, source="companion", kind=kind,
                    scope=scope, title=title, note_id=note_id, supersedes=supersedes, valid_on=valid_on)
            except ValueError as ex:
                raise ToolError(str(ex)) from None
        return {"status": status, "saveId": sid, **result,
                "recordingMeaning": "confirmed confirms the note was saved, not that its contents were verified in the game."}

    @mcp.tool()
    async def save_memory(text: str, scope: str | None = None, note_id: str | None = None,
                          supersedes: list[str] | None = None, valid_on: str | None = None) -> dict[str, Any]:
        """Remember a preference, agreement, experience or current understanding in natural language.

        Preserve context: '雨天想一起去海边，今天先休息' is a note, not an action trigger.
        scope describes where/when it applies, e.g. 长期约定 or 昨日整理后的箱子观察.
        Omitted scope leaves applicability unspecified. Inventory/positions are dated
        observations or handoff context; keep uncertain explanations as guesses.
        For a single game day, valid_on is year:season:day (e.g. 1:spring:3).
        Without valid_on the note remains available for recall; that is not a claim
        that the recorded world state is still current. Reusable approaches use save_method.
        To correct a note, pass its note_id and the updated text; supersedes replaces
        a list of notes while retaining history. A temporary exception can be its own
        note. Notes support reasoning and do not create work or prove world effects.
        Updates preserve omitted scope/valid_on; valid_on='' removes a date restriction.
        """
        return await save_companion_note(text, kind="memory", scope=scope, note_id=note_id,
                                         supersedes=supersedes, valid_on=valid_on)

    @mcp.tool()
    async def save_method(title: str, text: str, scope: str | None = None,
                          note_id: str | None = None, supersedes: list[str] | None = None) -> dict[str, Any]:
        """Save a reusable learned method as a flexible skill note, not a scheduled script.

        Describe when it helps, the useful approach and any actual experience.
        Adapt it to the current world when reused. Player-specific destinations or
        preferences belong in save_memory. Saving a method does not install code or
        grant a missing SMAPI capability. Use note_id to improve an existing method.
        """
        return await save_companion_note(text, kind="method", scope=scope, title=title,
                                         note_id=note_id, supersedes=supersedes)

    @mcp.tool()
    async def recall_memory(query: str = "", kind: Literal["memory", "method"] | None = None,
                            note_id: str | None = None, offset: int = 0, limit: int = 12) -> dict[str, Any]:
        """Recall this companion's relevant notes, learned methods and shared save memories.

        Use a topic such as 雨天 海边 or 收获整理; empty query returns recent notes.
        note_id reads one complete note, including an older dated note when explicitly
        requested. Notes carry their source/date/scope; current observations and the
        player's latest words help interpret them. kind='method' finds learned skills.
        Follow nextOffset for further results; a direct note_id reads the complete text.
        """
        sid = await current_save_id()
        context = await ready_memory_context(sid)
        return {"saveId": sid, **memory_for_run().recall(
            sid, query, **context, kind=kind, note_id=note_id, offset=offset, limit=limit)}

    @mcp.tool()
    async def forget_memory(note_id: str, reason: str = "") -> dict[str, Any]:
        """Withdraw a remembered note or learned method from future recall; keep its revision history."""
        sid = await current_save_id()
        context = await ready_memory_context(sid)
        directory = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data"
        with cache_lock(directory / "life-turn.json"):
            assert_life_turn_current(allow_control=True, lease_locked=True)
            try:
                status, result = memory_for_run().forget_note(
                    sid, note_id, companion_id=context["companion_id"], reason=reason)
            except ValueError as ex:
                raise ToolError(str(ex)) from None
        return {"status": status, "saveId": sid, **result}

    @mcp.tool()
    async def remember_intent(
        intent: str,
        kind: Literal["goal", "todo"] = "goal",
        goal_id: str | None = None,
        priority: int = 0,
        trigger: dict[str, Any] | None = None,
        expiry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Remember a goal or future todo. kind='todo' requires trigger, e.g. {"type":"calendar","year":2,"season":"spring","day":3}; kind='goal' ignores trigger. Inventory and crop triggers are also supported.

        Model-created goals are always recorded with source=agent, so the model can
        never label its own idea as a player instruction.

        Only record what you treat as durable intent: a confirmed observation or
        agreed plan is a fact and may become a goal/todo constraint; a hypothesis or
        inference is speculation — never write it as a goal or constraint, mark it
        as speculation with the companion-memory tools instead; a standing rule the
        player or system set is a constraint and must not be reworded as your own goal.

        Exact ``trigger`` shapes (call discover_capabilities("memory") for the same
        schema)::

            {"type": "calendar", "year": 2, "season": "spring", "day": 3}
            {"type": "inventory", "itemId": "(O)368", "minCount": 5}
            {"type": "crop", "cropId": "(O)24", "state": "harvestable"}

        Invalid (rejected with the allowed list): {"type": "daily"},
        {"type": "date", "date": "spring 3"}, {"type": "next_day"}.
        A trigger is required for kind='todo' and ignored for kind='goal'.
        """
        sid = await current_save_id()
        store = work_for_run()
        try:
            result = store.remember_intent(
                sid,
                intent=intent,
                kind=kind,
                goal_id=goal_id,
                priority=priority,
                trigger=trigger,
                expiry=expiry,
            )
            return {"saveId": sid, **result}
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def run_next_step() -> dict[str, Any]:
        """Harness worker: claim one ready plan step, execute it, and commit the result.

        This is the same execution path the ChatBridge internal plan worker uses
        (shared ``PlanExecutor``): the stable command id is persisted before
        dispatch and forwarded to the real scheduler/native command, and
        completion is committed only from the real execution result. Returns
        status=idle without any LLM wake when the plan has no executable work.
        """
        sid = await current_save_id()
        store = work_for_run()

        def _snapshot_state() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            # Explicit waiting conditions are evaluated against the latest cached
            # native snapshot only; this never issues a new farm query.
            cached = sched.latest_snapshot
            payload = cached.get("payload") if isinstance(cached, dict) else None
            if not isinstance(payload, dict):
                return None, None
            world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
            game_date = {
                "year": world.get("year"),
                "season": world.get("season"),
                "day": world.get("dayOfMonth"),
            }
            return payload, game_date

        executor = PlanExecutor(
            store,
            dispatch=execute_plan_operation,
            reconcile=reconcile_native_command,
            snapshot_provider=_snapshot_state,
        )
        execution = await executor.run_once(sid, f"mcp-{uuid.uuid4().hex[:8]}", recover=True)
        result = execution.as_dict()
        snapshot, game_date = _snapshot_state()
        if execution.status == "idle":
            return {
                **result,
                "hasExecutableWork": False,
                "overview": store.overview(sid, snapshot=snapshot, game_date=game_date),
            }
        return {
            **result,
            "hasExecutableWork": store.has_ready_step(
                sid, snapshot=snapshot, game_date=game_date
            ),
            "overview": store.overview(sid, snapshot=snapshot, game_date=game_date),
        }

    @mcp.tool()
    async def harvest_and_store(
        chest_x: ChestCoordinate,
        chest_y: ChestCoordinate,
        max_tiles: int = 16,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Harvest mature crops and deposit the harvested non-tool items into the named chest.

        Only the chest coordinates and item ids supplied by the caller are touched;
        no chest is chosen automatically.
        """
        harvest = await sched.harvest_auto(max_tiles=max_tiles)
        harvest_effects = harvest.get("effects") or []
        if harvest.get("status") == "executing" or harvest.get("terminalState") == "running":
            return {
                "outcome": "unknown",
                "goalSatisfied": False,
                "effects": harvest_effects,
                "remaining": {"stage": "harvest"},
                "reasonCode": "HARVEST_IN_PROGRESS",
                "snapshotRevision": harvest.get("worldRevision"),
                "status": "executing",
                "terminalState": "running",
                "stage": "harvest",
                "harvest": harvest,
            }
        harvest_ok = harvest.get("terminalState") == "succeeded"
        if harvest.get("status") == "no-work" or harvest.get("terminalState") == "none":
            return {
                "outcome": "completed",
                "goalSatisfied": True,
                "effects": [],
                "remaining": {"stage": None},
                "reasonCode": "NO_WORK",
                "snapshotRevision": harvest.get("worldRevision"),
                "status": "succeeded",
                "stage": "done",
                "harvest": harvest,
                "message": "No mature crops to harvest; nothing to store.",
            }
        if not harvest_ok and not harvest.get("completedCount"):
            return {
                "outcome": "partial",
                "goalSatisfied": False,
                "effects": harvest_effects,
                "remaining": {"stage": "harvest"},
                "reasonCode": "HARVEST_FAILED",
                "snapshotRevision": harvest.get("worldRevision"),
                "status": "partial",
                "stage": "harvest",
                "harvest": harvest,
            }

        deposit = await sched.deposit_to_chest(chest_x=chest_x, chest_y=chest_y, item_ids=item_ids)
        deposit_effects = deposit.get("effects") or []
        if deposit.get("status") == "executing" or deposit.get("terminalState") == "running":
            return {
                "outcome": "unknown",
                "goalSatisfied": False,
                "effects": harvest_effects + deposit_effects,
                "remaining": {"stage": "deposit"},
                "reasonCode": "DEPOSIT_IN_PROGRESS",
                "snapshotRevision": deposit.get("worldRevision"),
                "status": "executing",
                "terminalState": "running",
                "stage": "deposit",
                "harvest": harvest,
                "deposit": deposit,
            }
        deposit_ok = deposit.get("terminalState") == "succeeded"
        goal_satisfied = harvest_ok and deposit_ok
        return {
            "outcome": "completed" if goal_satisfied else "partial",
            "goalSatisfied": goal_satisfied,
            "effects": harvest_effects + deposit_effects,
            "remaining": {"stage": None if goal_satisfied else "deposit"},
            "reasonCode": "OK" if goal_satisfied else "STORE_INCOMPLETE",
            "snapshotRevision": deposit.get("worldRevision") or harvest.get("worldRevision"),
            "status": "succeeded" if goal_satisfied else "partial",
            "stage": "done",
            "harvest": harvest,
            "deposit": deposit,
        }

    @mcp.tool()
    async def discover_capabilities(group: str | None = None) -> dict[str, Any]:
        """Discover base tool groups and their short parameter schema on demand.

        Returns one group at a time (farm/shopping/chest/movement/knowledge) so the
        full base schemas are not carried in every request. directCall describes
        call_capability(tool, params); planStep describes submit_plan steps and
        accepts only its listed keys. Direct-call aliases are not plan-step keys.
        """
        if group is not None and group not in CAPABILITY_GROUPS:
            raise ToolError(
                f"unknown capability group '{group}'; available: {', '.join(sorted(CAPABILITY_GROUPS))}"
            )
        selected = {group: CAPABILITY_GROUPS[group]} if group else {}
        groups: dict[str, list[dict[str, Any]]] = {}
        for group_name, names in selected.items():
            entries: list[dict[str, Any]] = []
            for name in names:
                schema = base_schemas.get(name, {})
                parameters = schema.get("parameters") or {}
                properties = parameters.get("properties") or {}
                description = str(schema.get("description") or "").split("\n")[0]
                direct_parameters = {
                    key: capability_parameter_shape(key, value, parameters)
                    for key, value in properties.items()
                }
                allowed_values = {}
                for key, value in properties.items():
                    if isinstance(value, dict):
                        variants = [value, *value.get("anyOf", [])]
                        values = [item for variant in variants for item in variant.get("enum", [])]
                        if values:
                            allowed_values[key] = values
                plan_spec = _PLAN_OPERATION_CALLS.get(name)
                entries.append(
                    {
                        "name": name,
                        "description": description[:140],
                        **({"supportedActorModes": ["player"],
                            "companionAlternative": "query_shop -> navigate_to observed counter -> purchase_items"}
                           if name == "open_shop" else {}),
                        "directCall": {"required": list(parameters.get("required") or []),
                                       "parameters": direct_parameters,
                                       "allowedValues": allowed_values,
                                       "example": capability_example(name, parameters, direct_parameters)},
                        "planStep": ({"operation": name,
                                      "required": [key for key in parameters.get("required", []) if key in plan_spec[1]],
                                      "parameters": {key: direct_parameters.get(key, {"type": "string", "default": "Farm"} if key == "location_id" else {})
                                                     for key in sorted(plan_spec[1])},
                                      "example": {"operation": name, "params": {key: value for key, value in capability_example(name, parameters, direct_parameters).items() if key in plan_spec[1]}},
                                      "unknownKeys": "rejected"} if plan_spec else None),
                    }
                )
            groups[group_name] = entries

        response: dict[str, Any] = {
            "groups": groups,
            "availableGroups": sorted(CAPABILITY_GROUPS),
            "groupCounts": {name: len(tools) for name, tools in CAPABILITY_GROUPS.items()},
            # A name-only group list made the model open unrelated groups just
            # to locate an operation (e.g. shipping under shopping). Publish a
            # small index, not all parameter schemas, to allow one targeted read.
            "groupContents": {name: list(tools) for name, tools in CAPABILITY_GROUPS.items()},
            "discoveryHint": "Use groupContents to locate the operation, then read only its group for parameters. Reuse schemas already read in this session unless a validation error says they changed.",
            "callWith": "call_capability(tool, params)",
            "planWith": "Use planStep.operation and only planStep.parameters keys in submit_plan steps; null means direct-call-only. Direct-call aliases are not accepted in steps.",
            "fullToolListExposed": resolved_surface == "full",
        }
        if group == "memory":
            # The exact nested write schema, so the model never has to guess
            # trigger kinds such as "daily"/"date"/"next_day".
            response["memoryWriteSchema"] = MEMORY_WRITE_SCHEMA
        return response

    @mcp.tool()
    async def call_capability(tool: str, params: dict[str, Any] | None = None,
                              request_id: str | None = None) -> dict[str, Any]:
        """Invoke a base capability by name with structured params.

        This runs the exact same implementation, validation and result shape as the
        corresponding base tool; it is not a parallel code path.
        When a result delivery offers another queued player instruction, pass its
        request_id to bind a selected job and its eventual results to that instruction.
        """
        if tool == "observe_map_image":
            raise ToolError("Call observe_map_image directly to receive native image content")
        if tool == "plant_crop_workflow":
            raise ToolError(PLANT_WORKFLOW_PLAN_ERROR)
        if tool not in base_tools:
            raise ToolError(f"unknown capability '{tool}'; call discover_capabilities first")
        schema = base_schemas.get(tool, {})
        parameters = schema.get("parameters") or {}
        properties = parameters.get("properties") or {}
        required = parameters.get("required") or []
        args = dict(params or {})
        if properties:
            unknown = sorted(key for key in args if key not in properties)
            if unknown:
                raise ToolError(f"unsupported parameter(s) for '{tool}': {', '.join(unknown)}")
        missing = sorted(key for key in required if key not in args)
        if missing:
            raise ToolError(f"missing required parameter(s) for '{tool}': {', '.join(missing)}")
        args = validate_base_arguments(tool, args)
        owner_token = job_request_owner.set(request_id)
        try:
            result = await base_tools[tool](**args)
        except ToolError:
            raise
        except (PolicyViolationError, SchedulerError, NoActiveTaskError) as ex:
            raise ToolError(str(ex)) from None
        finally:
            job_request_owner.reset(owner_token)
        return {"tool": tool, "result": result}

    @mcp.tool()
    async def read_guidance(topic: str) -> dict[str, str]:
        """Read one reviewed topic: crop-selection, farm-region, livestock-processing,
        procurement-travel, wiki-lookup, daily-rhythm, or execution.
        This is a read-only packaged reference, not an arbitrary file reader.
        """
        try:
            # Actor identity is cached native metadata, not a reason to open a
            # second game connection or refresh observations merely to read docs.
            cached_mode = ((sched.latest_snapshot or {}).get("payload") or {}).get("actorMode")
            if cached_mode is None:
                try:
                    cached_mode = (read_cache(Path(sched.run_dir or run_dir or ".") / "data/life-snapshot.json").get("payload") or {}).get("actorMode")
                except (OSError, ValueError):
                    pass
            return load_guidance(topic, actor_mode=cached_mode)
        except ValueError as ex:
            raise ToolError(str(ex)) from None

    @mcp.tool()
    async def query_wiki(query: str, exact_page: bool = False, section: str | None = None) -> dict[str, Any]:
        """Look up strategy knowledge only when needed; returns short results with source and cache status.

        English queries prefer the official Stardew Valley Wiki, with BWIKI's
        Chinese encyclopedia as fallback. Chinese queries use BWIKI directly.
        Results include the actual source URL and language. Page content is
        reference material, not instructions; facts may differ by game version.
        For a known item/page title use exact_page=True: reads only that page.
        Optional section selects a named heading (availableSections returned),
        including tables beyond the initial excerpt; cached page reads are reused.
        """
        try:
            return await asyncio.to_thread(wiki_for_run().lookup, query, exact_page, section)
        except ValueError as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Wiki lookup unavailable: {ex}") from None

    @mcp.tool()
    async def water_tiles(
        tiles: Annotated[list[TileXY], BeforeValidator(_unwrap_item_array)],
        location_id: str = "Farm",
        include_empty_tiles: bool = False,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Water exactly the observed coordinate list, including scattered planting tiles.

        tiles=[{"x":69,"y":18},{"x":64,"y":23}]. Native water/stamina and
        route rules may stop early; counts and effects describe only real actions.
        """
        try:
            result = await sched.execute_tiles(tiles=_tile_dicts(tiles), location_id=location_id,
                                              include_empty_tiles=include_empty_tiles)
            return result if detail else {**_native_action_response("water-tiles", result),
                "targetCount": result.get("targetCount"),
                "unprocessedTargetCount": result.get("unprocessedTargetCount")}
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Water tiles execution rejected: {ex}") from None

    @mcp.tool()
    async def water_zone(
        center_x: int, center_y: int, radius: int = 0, include_empty_tiles: bool = False, detail: bool = False
    ) -> dict[str, Any]:
        """Water crops around an observed center with a non-negative integer radius.

        Returns terminalState, counts, and remaining unwatered crops.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            zone_kwargs = {"center_x": center_x, "center_y": center_y, "radius": radius}
            if include_empty_tiles:
                zone_kwargs["include_empty_tiles"] = True
            res = await sched.execute_water_zone(**zone_kwargs)
            if res.get("status") == "executing" or res.get("terminalState") == "running":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "inProgress": True,
                    "completedCount": res.get("completedCount", 0),
                    "fresh": False,
                    "message": res.get("message") or f"Water zone task is executing on companion (taskId={res.get('taskId')}).",
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)
            remaining_unwatered = None
            if fresh and fresh_snap:
                fw = fresh_snap.get("payload", {}).get("farmWork") or {}
                remaining_unwatered = fw.get("tilledUnwateredCount" if include_empty_tiles else "cropUnwateredCount")

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "remainingUnwateredCount": remaining_unwatered,
                "remainingCountScope": "all_tilled_soil" if include_empty_tiles else "crops_only",
                "remainingCropUnwateredCount": fw.get("cropUnwateredCount") if fresh and fresh_snap else None,
                "remainingEmptySoilUnwateredCount": (max(0, fw["tilledUnwateredCount"] - fw["cropUnwateredCount"])
                    if fresh and fresh_snap and type(fw.get("tilledUnwateredCount")) is int and type(fw.get("cropUnwateredCount")) is int else None),
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if detail:
                response["effects"] = res.get("effects", [])
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Water zone execution rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Water zone execution failed: {ex}") from None

    @mcp.tool()
    async def water_auto(
        max_tiles: Annotated[int, Field(description="Maximum tiles in one batch, not the goal scope. For only N crops, also pass an explicit observed target_tiles subset; omitted targets may leave the overall task partial.")] = 25, include_empty_tiles: bool = False, detail: bool = False,
        target_tiles: Annotated[list[TileXY] | None, BeforeValidator(_unwrap_item_array), Field(description="Optional exact candidate coordinates [{x:62,y:27},{x:66,y:23}]. Intersect with freshly observed unwatered crops; omitted/null uses observed work, [] selects no work. Coordinates outside this list are never selected.")] = None,
        location_id: str | None = None,
    ) -> dict[str, Any]:
        """Water freshly observed crops in target_tiles when provided; [] means no work.

        Omitted target_tiles keeps ordinary auto selection. location_id, if given,
        must match the observed work map. Include empty soil only when enabled.
        max_tiles is a batch cap; it does not exclude other crops from the goal.
        For only N crops, select up to N freshly observed target_tiles explicitly.
        Auto selection does not guarantee nearest-first ordering; choose the
        observed subset according to the player's requested area or proximity.
        """
        pre_rev = getattr(sched, "latest_world_revision", 0)
        try:
            auto_kwargs = {"max_tiles": max_tiles}
            if include_empty_tiles:
                auto_kwargs["include_empty_tiles"] = True
            if target_tiles is not None:
                auto_kwargs["target_tiles"] = _tile_dicts(target_tiles)
            if location_id is not None:
                auto_kwargs["location_id"] = location_id
            res = await sched.water_auto(**auto_kwargs)
            if res.get("status") == "no-work":
                return {
                    "status": "no-work",
                    "terminalState": "none",
                    "targetCount": 0,
                    "remainingUnwateredCount": 0,
                    "fresh": True,
                    "message": res.get("message", "No unwatered tilled tiles found."),
                }

            if res.get("status") == "executing":
                return {
                    "status": "executing",
                    "taskId": res.get("taskId"),
                    "terminalState": "running",
                    "targetCount": res.get("targetCount", 0),
                    "completedCount": res.get("completedCount", 0),
                    "remainingUnwateredCount": res.get("remainingUnwateredCount"),
                    "fresh": False,
                    "message": res.get("message", "Task is executing on companion in background."),
                    "details": res.get("details"),
                }

            fresh_snap, fresh = await _safe_wait_for_fresh_snapshot(sched, pre_rev, timeout=1.0)
            remaining_unwatered = None
            if fresh and fresh_snap:
                fw = fresh_snap.get("payload", {}).get("farmWork") or {}
                remaining_unwatered = fw.get("tilledUnwateredCount" if include_empty_tiles else "cropUnwateredCount")

            details = res.get("details") or {}
            failed_tiles = details.get("failedTiles") or []
            skipped_tiles = details.get("skippedTiles") or []
            blocked_targets = []
            for ft in failed_tiles:
                if isinstance(ft, dict):
                    reason = str(ft.get("reason", ""))
                    if any(k in reason.lower() for k in ("obstacle", "replan", "unreachable", "blocked", "obstruction")):
                        t = ft.get("tile") or {}
                        blocked_targets.append({"x": t.get("x"), "y": t.get("y"), "reason": reason, "retryable": False})
            for st in skipped_tiles:
                if isinstance(st, dict):
                    reason = str(st.get("reason", ""))
                    if any(k in reason.lower() for k in ("obstruction", "unreachable", "blocked", "obstacle")):
                        t = st.get("tile") or {}
                        blocked_targets.append({"x": t.get("x"), "y": t.get("y"), "reason": reason, "retryable": False})

            watered_tiles = [
                {"x": eff["tile"]["x"], "y": eff["tile"]["y"]}
                for eff in res.get("effects", [])
                if isinstance(eff, dict) and eff.get("state") == "watered" and isinstance(eff.get("tile"), dict)
            ]

            response = {
                "status": "executed",
                "taskId": res.get("taskId"),
                "terminalState": res.get("terminalState", "unknown"),
                "completedCount": res.get("completedCount", 0),
                "skippedCount": res.get("skippedCount", 0),
                "failedCount": res.get("failedCount", 0),
                "targetCount": res.get("targetCount", 0),
                "remainingUnwateredCount": remaining_unwatered,
                "remainingCountScope": "all_tilled_soil" if include_empty_tiles else "crops_only",
                "remainingCropUnwateredCount": fw.get("cropUnwateredCount") if fresh and fresh_snap else None,
                "remainingEmptySoilUnwateredCount": (max(0, fw["tilledUnwateredCount"] - fw["cropUnwateredCount"])
                    if fresh and fresh_snap and type(fw.get("tilledUnwateredCount")) is int and type(fw.get("cropUnwateredCount")) is int else None),
                "fresh": fresh,
                "details": res.get("details"),
                "error": res.get("error"),
            }
            if watered_tiles:
                response["wateredTiles"] = watered_tiles
            if blocked_targets:
                response["blockedTargets"] = blocked_targets
                response["actionableSummary"] = (
                    f"Non-retryable blocked targets: {len(blocked_targets)} tiles blocked by dynamic obstacles or actors "
                    f"(maximum replans exceeded). Do not immediately retry identical water_auto until the blocking actor/obstacle moves. "
                    f"You may use water_zone on other reachable areas."
                )
            if detail:
                response["targetTiles"] = res.get("targetTiles", [])
                response["effects"] = res.get("effects", [])
                response["isTruncated"] = res.get("isTruncated", False)
            return response
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(f"Auto watering rejected: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Auto watering failed: {ex}") from None


    @mcp.tool()
    async def query_inventory(detail: bool = False) -> dict[str, Any]:
        """Query companion backpack: freeSlots, capacity, non-tool items. Full if detail=True."""
        try:
            res = await sched.query_inventory()
            if detail:
                return res
            inv = res.get("inventory", {})
            return {
                "inventory": {
                    "capacity": inv.get("capacity", 0),
                    "freeSlots": inv.get("freeSlots", 0),
                    "nonToolItems": extract_non_tool_items(inv.get("slots", [])),
                },
                "worldRevision": res.get("worldRevision", 0),
            }
        except SchedulerError as ex:
            raise ToolError(f"Failed to query inventory: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error querying inventory: {ex}") from None

    @mcp.tool()
    async def query_chests(
        item_id: str | None = None,
        name: str | None = None,
        is_seed: bool | None = None,
        page: int = 1,
        page_size: int = 20,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Query farm chests: coordinates, capacity, freeSlots, items.
        Supports filtering by item_id, name, is_seed, and pagination (default 20/page).
        Concise summary by default (items truncated per chest); full if detail=True.
        Positions identify currently observed chests, not persistent chest identities
        or a history of who moved a chest or removed its contents.
        """
        try:
            res = await sched.query_chests()
            all_chests = res.get("chests", [])

            def matches_filter(item: dict[str, Any]) -> bool:
                if item_id is not None:
                    iid = str(item.get("itemId", "")).lower()
                    target_id = item_id.strip().lower()
                    if iid != target_id and not iid.endswith(target_id):
                        return False
                if name is not None:
                    iname = str(item.get("name", "")).lower()
                    if name.strip().lower() not in iname:
                        return False
                if is_seed is not None:
                    cat = item.get("category")
                    iid = str(item.get("itemId", "")).lower()
                    iname = str(item.get("name", "")).lower()
                    is_it_seed = (
                        cat == -74
                        or "seed" in iname
                        or "starter" in iname
                        or "spores" in iname
                        or "seed" in iid
                    )
                    if is_it_seed != is_seed:
                        return False
                return True

            has_filter = (item_id is not None) or (name is not None) or (is_seed is not None)

            filtered_chests = []
            for c in all_chests:
                tile = c.get("tile", {})
                cx = tile.get("x", 0)
                cy = tile.get("y", 0)
                contents = c.get("contents", [])

                if has_filter:
                    matched_items = [it for it in contents if matches_filter(it)]
                    if not matched_items:
                        continue
                    if detail:
                        c_copy = dict(c)
                        c_copy["contents"] = matched_items
                        filtered_chests.append(c_copy)
                    else:
                        summary = extract_chest_summary(c, cx, cy, None, detail=False)
                        matched_in_summary = [it for it in summary["items"] if matches_filter(it)]
                        filtered_chests.append({
                            "tile": summary["tile"],
                            "capacity": summary["capacity"],
                            "freeSlots": summary["freeSlots"],
                            "itemCount": summary["itemCount"],
                            "matchingItemCount": len(matched_items),
                            "items": matched_in_summary[:6],
                            "itemsTruncated": len(matched_items) > 6,
                            "hasDuplicateStacks": summary["hasDuplicateStacks"],
                            "hasMergeableStacks": summary["hasMergeableStacks"],
                        })
                else:
                    if detail:
                        filtered_chests.append(c)
                    else:
                        summary = extract_chest_summary(c, cx, cy, None, detail=False)
                        filtered_chests.append({
                            "tile": summary["tile"],
                            "capacity": summary["capacity"],
                            "freeSlots": summary["freeSlots"],
                            "itemCount": summary["itemCount"],
                            "items": summary["items"],
                            "itemsTruncated": summary["itemsTruncated"],
                            "hasDuplicateStacks": summary["hasDuplicateStacks"],
                            "hasMergeableStacks": summary["hasMergeableStacks"],
                        })

            safe_page_size = max(1, min(page_size, 50))
            safe_page = max(1, page)
            start_idx = (safe_page - 1) * safe_page_size
            end_idx = start_idx + safe_page_size
            paged_chests = filtered_chests[start_idx:end_idx]
            total_pages = max(1, (len(filtered_chests) + safe_page_size - 1) // safe_page_size)

            return {
                "chests": paged_chests,
                "observationScope": "Current Farm chest positions and contents; movement and item-transfer history are not observed here.",
                "totalChests": len(all_chests),
                "matchingChests": len(filtered_chests),
                "page": safe_page,
                "pageSize": safe_page_size,
                "totalPages": total_pages,
                "isTruncated": bool(res.get("isTruncated", False)) or (len(filtered_chests) > safe_page_size),
                "worldRevision": res.get("worldRevision", 0),
            }
        except SchedulerError as ex:
            raise ToolError(f"Failed to query chests: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected error querying chests: {ex}") from None

    register_native_tools(mcp, sched, record_observation_recovery, work_for_run)

    @mcp.tool()
    async def player_control(action: str) -> dict[str, Any]:
        """Claim/release the sole native player control lease. Only 'claim'/'release'; a human takeover always revokes it. Does not move or change resources."""
        if action not in {"claim", "release"}:
            raise ToolError("action must be claim or release")
        assert_life_turn_current()
        client = await sched.ensure_connected()
        token = decision_token()
        if token and resolved_surface not in {"light", "life"}:
            store = work_for_run()
            store._assert_decision_valid(store.state(client.save_id), token)
        if client.actor_mode != "player":
            raise ToolError("PLAYER_MODE_REQUIRED")
        await client.send_player_control(action)
        await asyncio.sleep(0.2)
        return {"action":action, "requested":True, "actorMode":client.actor_mode,
                "actorMultiplayerId":str(client.actor_multiplayer_id) if client.actor_multiplayer_id is not None else None, "saveId":client.save_id,
                "note":"Read get_status for current authoritative ownership; sending a request is not proof of ownership."}

    @mcp.tool()
    async def observe_player_activities() -> dict[str, Any]:
        """Read the current actor's map NPCs, public actions and shore geometry in either mode. actorTile belongs to that actor; playerInteractionSupported marks player-only interactions, and companion observations do not expose the player's menu. Use native walking to approach; future loot or locks remain unknown."""
        result = await sched.observe_player_activities()
        if isinstance(result.get("activities"), dict):
            await record_observation_recovery(result, lambda sid:
                work_for_run().observe_interaction_recovery(sid, result["activities"]))
        return result

    @mcp.tool()
    async def talk_npc(npc_name: str, location_id: str) -> dict[str, Any]:
        """Initiate native conversation with an observed adjacent NPC. Do not guess their current schedule. Follow native UI with native_advance/native_answer; routine authorized dialogue can continue, significant permanent choices require player authorization."""
        return _native_action_response("talk_npc", await sched.talk_npc(npc_name,location_id))

    @mcp.tool()
    async def interact_tile(tile: InteractionTileXY, location_id: str) -> dict[str, Any]:
        """Activate one observed adjacent public map Action via native checkAction. Native doors, mine entrance/ladders, signs and mailbox retain their rules. A TouchAction is not a forced warp: navigate onto its observed tile. Never infer a failed interaction unlocked something."""
        return _native_action_response("interact_tile", await sched.interact_tile(tile.model_dump(),location_id))

    @mcp.tool()
    async def attack(facing: int, location_id: str) -> dict[str, Any]:
        """One native melee swing (facing 0 up,1 right,2 down,3 left). Requires real non-scythe weapon. Observe living nearby monsters, approach safely, respect health/time; do not assert damage, kill or loot until native receipts/inventory show it. Native weapon animation/hitbox/monster rules execute; no direct HP editing."""
        return _native_action_response("attack",await sched.attack(facing,location_id))

    @mcp.tool()
    async def native_menu_select(choice_index: int, location_id: str, text: str | None = None) -> dict[str, Any]:
        """Select an observed item slot, crafting cursor destination, unlocked mine elevator button, or native NamingMenu confirmation. For NamingMenu use observed index 0 and explicit text (1..64 characters); the native text box filters and bounds it. If NAME_TEXT_CHANGED, read the resulting text before a new decision: no confirmation was sent. Naming UI input is not proof of a completed world transaction; inspect the resulting native state. Read current indexed choices with observe_player_activities. Choices and names follow the player's existing authorized intent; no story, trash, purchase or item decision is implicit."""
        return _native_action_response("native_menu_select",await sched.native_menu_select(choice_index,location_id,text=text))

    @mcp.tool()
    async def fish(facing: int, location_id: str) -> dict[str, Any]:
        """One complete bounded native cast from a reached shore (0 up,1 right,2 down,3 left). Requires player's real FishingRod; obtain through normal Willy event/shop, never grant it. Native timing, minigame physics/random catch, stamina/rewards/inventory remain game-owned. A failed/no-gain cast is not a catch. Model decides location, duration by repeated deliberate casts and when to stop."""
        return _native_action_response("fish", await sched.fish(facing,location_id))

    @mcp.tool()
    async def open_shop(shop_id: str, location_id: str) -> dict[str, Any]:
        """Player mode only; companions use query_shop then navigate_to and purchase_items.

        After the player reaches an adjacent counter, open its native menu and
        read sale IDs, prices and stock. Does not buy; closed shops fail explicitly.
        """
        return _native_action_response("open_shop", await sched.open_shop(shop_id,location_id))

    @mcp.tool()
    async def open_chest(chest_x: int, chest_y: int, location_id: str = "Farm") -> dict[str, Any]:
        """Open an adjacent normal chest through native interaction to learn its contents. Navigate beside it first."""
        return _native_action_response("open_chest", await sched.open_chest(chest_x,chest_y,location_id))

    @mcp.tool()
    async def native_advance(allow_skip: bool = False) -> dict[str, Any]:
        """Advance the actor's current native UI one step (native-advance).

        Use for ordinary dialogue (advances one line), informational menus
        (dismissed), and — only with allow_skip=True — a legally skippable cutscene.
        A question dialogue is NEVER answered implicitly: it is rejected with
        CHOICE_REQUIRED and details.responses lists the options; answer it with
        native_answer(choice_index=...). The model must not pick on the player's
        behalf without authorized intent. Festival free-roam is not a cutscene:
        observe_player_activities exposes NPCs, native actions and boundary exits.
        Waiting does not end a festival. Rejection reasonCodes: EVENT_ACTIVE (scripted event),
        FESTIVAL_FREE_ROAM (observe and choose an available native action),
        CHOICE_REQUIRED (list options, then native_answer), MENU_OWNED_BY_SLEEP_FLOW
        (leave sleep-flow menus alone), NAME_INPUT_REQUIRED (NamingMenu needs
        explicit native_menu_select text; closing it does not complete naming),
        NOTHING_TO_ADVANCE (no native UI to act on).
        """
        try:
            return _native_action_response("native-advance", await sched.execute_native_advance(allow_skip=allow_skip))
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Native advance failed: {ex}") from None

    @mcp.tool()
    async def native_answer(choice_index: int) -> dict[str, Any]:
        """Answer the current question dialogue or confirmation with an explicit choice (native-answer).

        choice_index selects one of the responses a CHOICE_REQUIRED native-advance
        rejection listed in details.responses (0-based, in display order). Answering
        is never implicit: never choose for the player without their instruction. A
        non-question state is rejected NOT_A_QUESTION. Native ConfirmationDialog
        choices are 0=confirm, 1=cancel; read its text in get_status first.
        """
        try:
            return _native_action_response("native-answer", await sched.execute_native_answer(choice_index=choice_index))
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None
        except Exception as ex:
            raise ToolError(f"Native answer failed: {ex}") from None

    @mcp.tool()
    async def pause_task() -> dict[str, Any]:
        """Pause the currently active companion task."""
        await disable_autonomy_after_task_control()
        try:
            result = await sched.pause_task()
            return result
        except NoActiveTaskError as ex:
            raise ToolError(f"Cannot pause: {ex}") from None
        except SchedulerError as ex:
            raise ToolError(f"Pause failed: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected pause error: {ex}") from None

    @mcp.tool()
    async def resume_task() -> dict[str, Any]:
        """Resume the currently paused companion task."""
        try:
            return await sched.resume_task()
        except NoActiveTaskError as ex:
            raise ToolError(f"Cannot resume: {ex}") from None
        except SchedulerError as ex:
            raise ToolError(f"Resume failed: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected resume error: {ex}") from None

    @mcp.tool()
    async def cancel_task(
        reason: str = "User cancelled via MCP",
    ) -> dict[str, Any]:
        """Cancel the currently active companion task safely."""
        await disable_autonomy_after_task_control()
        try:
            result = await sched.cancel_task(reason=reason)
            return result
        except NoActiveTaskError as ex:
            raise ToolError(f"Cannot cancel: {ex}") from None
        except SchedulerError as ex:
            raise ToolError(f"Cancel failed: {ex}") from None
        except Exception as ex:
            raise ToolError(f"Unexpected cancel error: {ex}") from None

    @mcp.tool()
    async def cancel_plan_command(command_id: str) -> dict[str, Any]:
        """Retire only a persisted native command whose task authorization was revoked."""
        if resolved_surface != "internal":
            raise ToolError("HARNESS_ONLY: native command retirement is an internal control")
        sid = await current_save_id()
        state = work_for_run().state(sid)
        if not any(task.status == "cancelled" and any(
            step.command_id == command_id and step.status in {"running", "unknown", "waiting"}
            for step in task.steps
        ) for task in state.tasks):
            raise ToolError("COMMAND_NOT_REVOKED: no cancelled task owns this unresolved command")
        client = await sched.ensure_connected()
        await client.cancel_skill(command_id="retire-" + uuid.uuid4().hex,
                                  correlation_id=command_id, reason="Accepted work scope changed")
        return {"status": "cancellation-requested", "commandId": command_id}

    @mcp.tool()
    async def reconcile_plan_command(command_id: str) -> dict[str, Any]:
        """Read the Mod's cached terminal result for a persisted plan command id.

        Read-only: it never dispatches. The internal plan worker uses it to settle
        a step whose previous attempt is unconfirmed instead of re-dispatching it
        and risking a double charge/action. Returns ``found=False`` when the Mod
        has no terminal result for the id.
        """
        if not command_id:
            return {"found": False, "commandId": command_id, "reasonCode": "MISSING_COMMAND_ID"}
        payload = await reconcile_native_command(command_id)
        if not isinstance(payload, dict):
            return {"found": False, "commandId": command_id}
        return {"found": True, "commandId": command_id, "native": payload}

    @mcp.tool()
    async def dispatch_plan_operation(
        operation: str,
        params: dict[str, Any] | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Harness-only: execute one validated plan step with its stable command id.

        This is the internal plan worker's dispatch entry point. It runs the same
        real scheduler implementation, parameter validation and command-id
        forwarding as the ``run_next_step`` tool (both call
        ``execute_plan_operation``); it is not a parallel path and is never exposed
        on the model surface. The persisted plan command id is the idempotency
        identity the native Mod de-duplicates on.
        """
        try:
            raw_params = dict(params or {})
            entry = _PLAN_OPERATION_CALLS.get(operation)
            if entry is not None:
                _, allowed = entry
                unknown = set(raw_params) - set(allowed)
                if unknown:
                    logger.warning(
                        "Stripping unknown parameter(s) %s for operation '%s' before dispatch",
                        sorted(unknown),
                        operation,
                    )
                    filtered_params = {k: v for k, v in raw_params.items() if k in allowed}
                else:
                    filtered_params = raw_params
            else:
                filtered_params = raw_params
            return await execute_plan_operation(operation, filtered_params, command_id)
        except (PolicyViolationError, SchedulerError) as ex:
            raise ToolError(str(ex)) from None

    mcp.scheduler = sched  # type: ignore[attr-defined]
    # Capture every real tool implementation for discovery/call, then trim the
    # default list to the lightweight surface unless the caller wants the full list.
    def protect_job(name, fn):
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            nonlocal external_selected
            if name == "plant_crop_workflow":
                raise ToolError(PLANT_WORKFLOW_PLAN_ERROR)
            sid = await current_save_id()
            store = work_for_run()
            if name in {"pause_task", "resume_task"}:
                return await fn(*args, **kwargs)
            if name == "cancel_task":
                # A new turn may ask to stop the previous native action before
                # selecting its own short job.  Cancelling when that old action
                # is already gone must not revoke this turn's unused token.
                # Once this turn has selected a job, revoke it before native
                # cancellation so a pending step cannot be dispatched later.
                if store.state(sid).decision.get("selected"):
                    store.revoke_decision(sid)
                return await fn(*args, **kwargs)
            if name not in _PLAN_OPERATION_CALLS:
                raise ToolError("SHORT_JOB_UNSUPPORTED: choose a discoverable native plan operation")
            await validate_actor_operation(name)
            if args:
                raise ToolError("Short job parameters must be named")
            # Direct action calls become durable jobs too. Serialize nested
            # validated values (e.g. ItemQuantity) before persisting the plan.
            params = validate_base_arguments(name, dict(kwargs), for_plan=True)
            if name == "navigate_to":
                destinations = [params.get("tile")]
                for x_key, y_key in (("tile_x", "tile_y"), ("x", "y")):
                    x_value, y_value = params.pop(x_key, None), params.pop(y_key, None)
                    if x_value is not None or y_value is not None:
                        if x_value is None or y_value is None:
                            raise ToolError("Both x and y coordinates must be specified. No job selected.")
                        destinations.append({"x": x_value, "y": y_value})
                destinations = [tile for tile in destinations if tile is not None]
                if destinations and any(tile != destinations[0] for tile in destinations):
                    raise ToolError("Conflicting navigation coordinates. No job selected.")
                if destinations:
                    params["tile"] = destinations[0]
                if params.get("tile") is None:
                    snapshot = sched.latest_snapshot
                    payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
                    shop = payload.get("shop") if isinstance(payload, dict) else None
                    if (isinstance(shop, dict) and isinstance(shop.get("interactionTile"), dict)
                            and str(shop.get("locationId") or "SeedShop").casefold()
                            == str(params.get("location_id") or "").casefold()):
                        params["tile"] = shop["interactionTile"]
                    else:
                        raise ToolError("navigate_to needs an observed destination tile. No job selected.")
                if any(type(params["tile"].get(key)) is not int or params["tile"][key] < 0 for key in ("x", "y")):
                    raise ToolError("Destination must contain non-negative integer x and y. No job selected.")
            unknown = set(params) - _PLAN_OPERATION_CALLS[name][1] - {"detail", "command_id"}
            if unknown:
                raise ToolError(f"INVALID_JOB_PARAMETERS: {name} does not accept {sorted(unknown)}; no job selected")
            try:
                decision = store.state(sid).decision
                bound_goal_id = decision.get("goalScope") or decision.get("goalId")
                selected = store.submit_plan(sid, goal_id=bound_goal_id,
                    goal_text=None if bound_goal_id else "Current model-selected short job",
                    tasks=[{"title": name, "steps": [{"operation": name, "params": params}]}],
                    decision_token=decision_token(), request_id=job_request_owner.get())
            except WorkStateError as ex:
                raise ToolError(str(ex)) from None
            if external_codex:
                external_selected = True
                await sched.close()
            return {"status": "job-selected", "taskId": selected["tasks"][0]["id"], "goalId": selected["goalId"],
                    "effectStatus": "not_executed_yet", "canAppendBeforeDispatch": True,
                    **({"handoff": "ack_async", "executionStarts": "after_current_turn"} if not external_codex else {})}
        return guarded

    async def execute_observed_action(name, args):
        # Keep the native single-action controller and current turn authority,
        # without creating a persisted plan or waiting for the model to exit.
        assert_life_turn_current()
        if external_codex and resolved_surface == "light":
            from .external_connection import read_binding
            root = Path(getattr(sched, "run_dir", None) or run_dir or ".")
            binding = read_binding(root)
            if binding and binding.get("enabled", True):
                raise ToolError("ORIGINAL_PARTNER_CONNECTED: disconnect the original partner adapter before manual interaction")
            source = os.environ.get("STARDEW_CONNECTION_SOURCE")
            if source is None:
                try:
                    source = json.loads((root / "config.json").read_text(encoding="utf-8-sig")).get("ConnectionSource")
                except (OSError, ValueError):
                    source = None
            if source != "external":
                raise ToolError("MANUAL_CONNECTION_REQUIRED: select the external connection source in game first; tools alone do not wake a conversation")
            await begin_game_turn()
        sid = await current_save_id()
        store = work_for_run()
        try:
            state = store.state(sid)
            if state.paused:
                raise WorkStateError("WORK_STOPPED: work is paused")
            # Immediate execution belongs to the live conversation and native
            # actor, not the expiring token of the retired short-job queue.
        except WorkStateError as ex:
            raise ToolError(str(ex)) from None
        await validate_actor_operation(name)
        try:
            lease = read_cache(Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "life-turn.json")
        except FileNotFoundError:
            lease = {}
        scope = lease.get("goalScope") or state.decision.get("goalScope") or state.decision.get("goalId")
        goal = next((g for g in state.goals if g.id == scope), None)
        args = bind_execution_scope_params(name, args, goal.constraints if goal else {})
        validated = validate_base_arguments(name, args)
        parsed = base_metadata[name].arg_model.model_validate(validated)
        previous_wait = getattr(sched, "await_terminal_results", False)
        sched.await_terminal_results = True
        try:
            result = await native_tools[name](**{key: getattr(parsed, key) for key in type(parsed).model_fields})
            if (name in {"place_items", "hoe_tiles", "clear_debris", "cut_grass", "chop_tree"}
                    and isinstance(result, dict) and result.get("terminalState") in {"partially-succeeded", "failed", "rejected"}):
                # Put actual unfinished targets before long successful-effect lists:
                # providers may truncate the middle of a large native receipt.
                details = result.get("details") or {}
                candidates = [x for x in result.get("effects", []) if x.get("state") in {"failed", "skipped"}]
                for field in ("failedTiles", "skippedTiles", "failed", "skipped"):
                    candidates.extend(details.get(field) or [])
                remaining = {}
                for entry in candidates:
                    if not isinstance(entry, dict):
                        continue
                    tile = entry.get("tile") or entry
                    x, y = tile.get("x"), tile.get("y")
                    if type(x) is int and type(y) is int:
                        remaining[(x, y)] = {"x": x, "y": y, "reason": entry.get("reason")}
                if not remaining and not result.get("completedCount"):
                    remaining = {(tile["x"], tile["y"]): dict(tile) for tile in args.get("tiles", [])}
                review = {"unresolvedTargets": list(remaining.values()),
                    "nextStep": "This action did not finish all requested work. Reobserve remaining targets; decide whether to clear, preserve and redesign, or recover inventory. Update the actual project layout and finish every requested functional area before completing the project. No work was queued or retried."}
                if remaining:
                    try:
                        xs, ys = zip(*remaining, strict=True)
                        left, top = max(0, min(xs) - 1), max(0, min(ys) - 1)
                        region = {"x": left, "y": top, "width": max(xs) - left + 2, "height": max(ys) - top + 2}
                        snapshot = getattr(sched, "latest_snapshot", None) or {}
                        pose = (snapshot.get("payload") or {}).get("companion") or {}
                        space = await sched.query_farm_space(location_id=args.get("location_id") or pose.get("locationId") or "Farm", region=region)
                        occupants = [o for o in space.get("occupants", []) if any(
                            o.get("x", -1000) - 1 <= x < o.get("x", -1000) + o.get("width", 1) + 1
                            and o.get("y", -1000) - 1 <= y < o.get("y", -1000) + o.get("height", 1) + 1 for x, y in remaining)]
                        review["observation"] = {"region": region, "capturedRevision": space.get("capturedRevision"),
                            "occupants": occupants[:128], "occupantsTruncated": bool(space.get("occupantsTruncated")) or len(occupants) > 128}
                    except Exception as ex:
                        review["observation"] = {"status": "unavailable", "error": str(ex)}
                result = {"remainingWork": review, **result}
            if name in {"navigate_to", "ship_items"} and isinstance(result, dict) and result.get("terminalState") in {"failed", "rejected"}:
                try:
                    status = await sched.get_status()
                    pose = status.get("companion") or {}
                    space = await sched.query_farm_space(location_id=pose.get("locationId") or args.get("location_id") or "Farm")
                    x, y = pose.get("tileX"), pose.get("tileY")
                    nearby = [o for o in space.get("occupants", []) if type(x) is int and type(y) is int
                              and abs(o.get("x", -1000) - x) <= 4 and abs(o.get("y", -1000) - y) <= 4]
                    result["obstructionObservation"] = {"position": pose, "nearbyOccupants": nearby,
                        "occupantsTruncated": space.get("occupantsTruncated"),
                        "nextStep": "Decide whether this obstruction can and should be cleared under player preferences; do not repeat the same blocked route blindly."}
                except Exception as ex:
                    result["obstructionObservation"] = {"status": "unavailable", "error": str(ex)}
            if name in {"harvest_auto", "harvest_tiles", "chop_tree", "clear_debris", "cut_grass", "pickup_items", "remove_items"} and isinstance(result, dict):
                try:
                    snapshot = getattr(sched, "latest_snapshot", None) or {}
                    companion = (snapshot.get("payload") or {}).get("companion") or {}
                    location = args.get("location_id") or companion.get("locationId") or "Farm"
                    ground = await sched.query_production(location_id=location)
                    inventory = await sched.query_inventory()
                    result["afterWork"] = {
                        "ground": {"locationId": ground.get("locationId"), "scope": "map",
                                   "capturedRevision": ground.get("capturedRevision"),
                                   "groundItems": [item for item in ground.get("groundItems", [])
                                                   if item.get("canBeGrabbed") or item.get("isDropped")],
                                   "truncated": ground.get("groundItemsTruncated", True)},
                        "inventory": inventory,
                        "interpretation": "Current observations after the action, not an inventory-delta receipt. Automatic pickup, the player and other world changes may affect these items; choose remaining work from the current scene."}
                except Exception as ex:
                    result["afterWork"] = {"status": "unavailable", "error": str(ex)}
            return result
        finally:
            sched.await_terminal_results = previous_wait

    def protect_external_observation(fn):
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            if external_codex and external_handed_off:
                raise ToolError("AUTONOMY_OWNS_SESSION: ongoing work was handed to the bridge; read work_plan_overview, or disable autonomy and begin_game_turn before observing externally")
            if external_codex and external_selected and external_save_id:
                decision = work_for_run().state(external_save_id).decision
                if decision.get("selected") and not decision.get("finished"):
                    raise ToolError("JOB_IN_PROGRESS: use work_plan_overview for persisted progress; native observation is available after the job settles")
            return await fn(*args, **kwargs)
        return guarded

    @mcp.tool()
    async def inspect_capabilities(capability: str | None = None, version: str | None = None,
                                   native_type: str | None = None, mod_id: str | None = None,
                                   member: str | None = None, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """Discover learned native abilities, their source/guidance/schema, or actual game and SMAPI API signatures.

        With capability/version, read that persisted package; omit version for its saved active version.
        With native_type (e.g. Furniture) or mod_id, inspect the loaded native API, authoring interface
        and C# example. Prefer an exact type name; ambiguous matches return candidate names.
        Filter members by name with member, or continue with offset/limit when hasMore is true.
        Without filters, list loaded abilities and available versions.
        Discover an existing ability before developing a new one. API signatures describe available
        calls; choose native gameplay methods and use the program-bound actor.
        """
        assert_life_turn_current(allow_control=True)
        store = capabilities_for_run()
        if capability:
            result = store.inspect(capability, version)
            package = store.package_path(capability, result["manifest"]["version"])
            return {**result, "source": (package / "Adapter.cs").read_text(encoding="utf-8")}
        if native_type or mod_id:
            if offset < 0 or not 1 <= limit <= 40:
                raise ToolError("Use offset >= 0 and limit from 1 to 40 to read API pages.")
            discovery = await extension_host_call(sched.discover_extension_api, mod_id, native_type,
                                                  member=member, offset=offset, limit=limit)
            authoring = dict(discovery.get("authoring") or {})
            # Build references stay local to the builder; the model needs the API contract.
            authoring.pop("references", None)
            authoring.pop("hostAssembly", None)
            return {**discovery, "authoring": authoring}
        packages = []
        for package in store.list_packages():
            if package.get("error"):
                packages.append(package)
                continue
            manifest = package.get("manifest") or {}
            packages.append({**{key: manifest.get(key) for key in ("id", "version", "title", "kind")},
                             "status": (package.get("verification") or {}).get("status"),
                             "effectVerifiedByHost": False,
                             "details": "inspect_capabilities(capability=id, version=version)"})
        return {"host": await extension_host_call(sched.list_extensions), "packages": packages}

    @mcp.tool()
    async def develop_capability(capability: str, version: str, title: str, kind: Literal["query", "action"],
                                 source: str, entry_type: str, input_schema: dict[str, Any], guidance: str,
                                 dependencies: list[dict[str, str]] | None = None,
                                 permission_quote: str | None = None, notice_id: str | None = None) -> dict[str, Any]:
        """Compile a missing native ability from C# source after discussing it with the player.

        Use consult.inspect_capabilities to read the actual API and IGameExtension authoring example.
        Both query and action kinds implement the same IGameExtension interface; there is no separate query interface.
        input_schema is a JSON object schema. Use native interactions,
        current actor and observations. guidance explains purpose, conditions, inputs and actual effects.
        Quote the player's explicit permission in permission_quote; a linked decision answer also works.
        Existing permission covers revisions of the same ability and scope. Each build has a new version.
        Compilation is separate from gameplay; on success use manage_capability(action='try'), invoke
        learned_query/learned_action, then action='keep' to persist a successfully used version.
        Compiler diagnostics support correction; earlier installed versions stay available.
        """
        store = capabilities_for_run()
        previous = store.recent_learning_provenance(capability, save_id=await current_save_id(),
                                                    kind=kind, dependencies=dependencies)
        try:
            old = store.inspect(capability)
            if previous is None and old["manifest"]["kind"] == kind and old["manifest"].get("dependencies", []) == (dependencies or []):
                previous = old.get("provenance")
        except CapabilityError:
            pass
        provenance = await learning_permission(permission_quote, notice_id, previous)
        discovery = await extension_host_call(sched.discover_extension_api)
        authoring = discovery.get("authoring")
        if not isinstance(authoring, dict):
            raise ToolError("Native authoring information is unavailable; inspect the host result before retrying.")
        result = await store.build({"id": capability, "version": version, "title": title, "kind": kind,
            "source": source, "entryType": entry_type, "inputSchema": input_schema,
            "guidance": guidance, "dependencies": dependencies or []}, authoring, provenance=provenance)
        assert_life_turn_current(allow_control=True)
        return result

    @mcp.tool()
    async def prepare_capability_builder(permission_quote: str, notice_id: str | None = None) -> dict[str, Any]:
        """Prepare the local C# compiler once when develop_capability reports setup_required.

        Uses an existing compatible SDK when available; otherwise downloads the official SDK into
        the ability directory and checks its published checksum. Quote the player's agreement to
        set up the learning environment. Subsequent builds reuse it.
        """
        await learning_permission(permission_quote, notice_id)
        result = await capabilities_for_run().prepare_builder()
        assert_life_turn_current(allow_control=True)
        return result

    @mcp.tool()
    async def manage_capability(action: Literal["try", "keep", "rollback"], capability: str,
                                version: str | None = None) -> dict[str, Any]:
        """Load a compiled version for trial, keep its successful native use, or restore a previous version.

        try requires version and hot-loads the agreed package in the current game. Execute it through
        learned_query or learned_action and inspect the native result. keep saves that successfully
        used version for subsequent games. rollback restores the previous saved working version.
        Loading never completes the player's original goal; continue from current observations.
        Invocation success is the adapter's report. Use game observations to judge its effect and
        whether to keep it; other players may also change the world during the operation.
        """
        assert_life_turn_current()
        store = capabilities_for_run()
        saved_before = store.catalog()["capabilities"].get(capability)
        if action == "rollback":
            previous = (saved_before or {}).get("previousVersion")
            if not previous:
                raise ToolError("No previous saved version is available.")
            version = previous
        if not version:
            raise ToolError("Choose the version returned by develop_capability or inspect_capabilities.")
        package = store.inspect(capability, version)
        if not package.get("provenance", {}).get("requestId"):
            raise ToolError("This package has no recorded player discussion.")
        store.verify_artifact(capability, version)
        if action in {"keep", "rollback"} and (package.get("verification") or {}).get("status") != "used_successfully":
            raise ToolError("Try this version and inspect a successful native invocation before keeping it.")
        receipt = await extension_host_call(sched.load_extension, f"{capability}/{version}", action != "try")
        if action == "try":
            verification = store.record_native_load(capability, version, receipt)
        else:
            verification = store.record_native_persist(capability, version, receipt, saved_before=saved_before)
        return {"capability": capability, "version": version, "native": receipt, "verification": verification}

    async def invoke_learned(capability: str, version: str, arguments: dict[str, Any], kind: str) -> dict[str, Any]:
        import jsonschema

        # Both operations run native adapter code and honor the player's stop.
        assert_life_turn_current()
        store = capabilities_for_run()
        manifest = store.verify_artifact(capability, version)
        if manifest["kind"] != kind:
            raise ToolError(f"This capability is a {manifest['kind']}; use its corresponding learned operation.")
        try:
            jsonschema.validate(arguments, manifest["inputSchema"])
        except (jsonschema.ValidationError, jsonschema.SchemaError) as ex:
            raise ToolError(f"Invalid capability arguments: {ex.message}") from None
        result = await extension_host_call(sched.invoke_extension, capability, version, arguments, kind)
        receipt = result.get("details") or {}
        if receipt.get("succeeded") is True:
            result = {**result, "learning": store.record_native_validation(capability, version, receipt)}
        return result

    @mcp.tool()
    async def learned_query(capability: str, version: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a loaded native query adapter, honoring pause/control like other native executions.

        Get its version, input schema and guidance from consult.inspect_capabilities.
        Query describes its purpose; the adapter runs native code, not a read-only snapshot.
        """
        return await invoke_learned(capability, version, arguments, "query")

    @mcp.tool()
    async def learned_action(capability: str, version: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a loaded native ability as the current actor. Read its schema/guidance first; returns the native terminal result and observations."""
        return await invoke_learned(capability, version, arguments, "action")

    @mcp.tool()
    async def say_to_player(text: str) -> dict[str, Any]:
        """Speak naturally to the player now. Use for conversation or a needed question, not action progress; progress is already on the HUD."""
        assert_life_turn_current(allow_control=True)
        request_id = os.environ.get("STARDEW_LIFE_TURN_ID", "")
        sid = os.environ.get("STARDEW_LIFE_SAVE_ID", "")
        if not request_id or not sid or not text.strip():
            raise ToolError("A current game conversation and nonempty text are required")
        from .snapshot_cache import write_cache
        path = Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "life-controls" / f"{request_id}--{time.time_ns()}.json"
        write_cache(path, {"saveId": sid, "action": "speak", "text": text.strip()})
        return {"status": "queued"}

    def refresh_turn_context(fn):
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            path = os.environ.get("STARDEW_TURN_CONTEXT")
            if path:
                try:
                    context = json.loads(Path(path).read_text(encoding="utf-8"))
                except (OSError, ValueError) as ex:
                    raise ToolError("TURN_CONTEXT_UNAVAILABLE") from ex
                for key in ("STARDEW_LIFE_MODE", "STARDEW_LIFE_TURN_ID", "STARDEW_LIFE_SAVE_ID", "STARDEW_DECISION_TOKEN"):
                    if key in context:
                        os.environ[key] = str(context[key])
                    else:
                        os.environ.pop(key, None)
            turn_id = os.environ.get("STARDEW_LIFE_TURN_ID")
            sid = os.environ.get("STARDEW_LIFE_SAVE_ID")
            if turn_id and sid:
                from .turn_messages import exchange
                assert_life_turn_current(allow_control=True)
                exchange(Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "turn-messages.json",
                         turn_id, sid, ack_world=True)
            def player_updates():
                turn_id = os.environ.get("STARDEW_LIFE_TURN_ID")
                sid = os.environ.get("STARDEW_LIFE_SAVE_ID")
                if not turn_id or not sid:
                    return []
                assert_life_turn_current(allow_control=True)
                from .turn_messages import exchange
                return exchange(Path(getattr(sched, "run_dir", None) or run_dir or ".") / "data" / "turn-messages.json",
                                turn_id, sid, take=True)
            def update_fields(updates):
                return {key: items for key, items in {
                    "worldEvents": [m for m in updates if m.get("kind") == "world_changed"],
                    "playerMessages": [m for m in updates if m.get("kind") != "world_changed"],
                }.items() if items}
            if fn is execute_observed_action:
                updates = player_updates()
                if updates:
                    return {"status": "world_update" if any(m.get("kind") == "world_changed" for m in updates) else "player_update",
                            "actionExecuted": False, **update_fields(updates),
                            "nextStep": "Reconsider this action using the new world facts and player requests before dispatching."}
            result = await fn(*args, **kwargs)
            updates = player_updates() if isinstance(result, dict) else []
            if updates:
                result = {**result, **update_fields(updates),
                          "nextStep": "Use the updated world and player requests to choose the next action."}
            return result
        return guarded

    readonly = {"get_status", "query_inventory", "query_chests", "query_farm_work", "query_wiki", "read_guidance", "read_reload_history", "query_shop", "query_animals", "query_machines", "query_buildings", "query_debris", "query_location", "work_plan_overview"}
    declared_tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    readonly.update({"recall_memory", "inspect_capabilities", "learned_query"})
    native_tools = {name: tool.fn for name, tool in declared_tools.items()}
    wire_param_shapes = {}
    def wire_type(shape):
        # Generic operation fields need concrete converter types. Conditional
        # bounds/defaults belong to the chosen tool's canonical server validator,
        # not the first unrelated operation encountered by this projection.
        if not isinstance(shape, dict):
            return shape
        alternatives = shape.get("anyOf") or shape.get("oneOf")
        if alternatives:
            nonnull = next((part for part in alternatives if part.get("type") != "null"), {})
            return wire_type(nonnull)
        result = {k: v for k, v in shape.items() if k in
                  {"type", "properties", "items", "required", "additionalProperties"}}
        if "properties" in result:
            result["properties"] = {k: wire_type(v) for k, v in result["properties"].items()}
        if "items" in result:
            result["items"] = wire_type(result["items"])
        return result
    for name in sorted(set(_PLAN_OPERATION_CALLS) | {n for group in CAPABILITY_GROUPS.values() for n in group}):
        declaration = declared_tools.get(name)
        if declaration is None:
            continue
        for field, shape in declaration.parameters.get("properties", {}).items():
            canonical = capability_parameter_shape(field, shape, declaration.parameters)
            wire_param_shapes.setdefault(field, wire_type(canonical))
    for tool in mcp._tool_manager.list_tools():
        # These tools are bounded to the current Stardew Valley save. Wiki
        # lookup and its generic dispatcher can access public knowledge; keep
        # those open-world. Write/destructive defaults remain conservative.
        passive = tool.name in {"get_status", "query_inventory", "work_plan_overview",
                                "discover_capabilities", "read_guidance", "read_reload_history", "autonomy_status", "query_wiki"}
        passive = passive or tool.name == "recall_memory"
        tool.annotations = ToolAnnotations(readOnlyHint=passive, destructiveHint=not passive,
                                           openWorldHint=tool.name in {"query_wiki", "call_capability"})
        tool.description = "Stardew Valley (SMAPI), current saved game. " + operation_description(tool, full=True)
        exempt = {"say_to_player", "answer_player_decision", "player_control", "request_player_decision", "begin_game_turn", "submit_plan", "remember_intent", "record_decision_note", "manage_goal", "manage_goals", "manage_plan", "manage_todo", "manage_todos", "manage_milestones", "manage_companion", "call_capability", "set_autonomy", "autonomy_status", "run_next_step", "dispatch_plan_operation", "reconcile_plan_command", "cancel_plan_command", "work_plan_overview"}
        exempt.update({"save_memory", "save_method", "forget_memory", "develop_capability", "manage_capability", "prepare_capability_builder"})
        if tool.name not in exempt and tool.name not in readonly and not tool.name.startswith(("query_", "get_", "list_", "discover_", "observe_")):
            tool.fn = protect_job(tool.name, tool.fn)
        if tool.name.startswith(("query_", "observe_")) and tool.name != "query_wiki":
            tool.fn = protect_external_observation(tool.fn)
        tool.fn = refresh_turn_context(tool.fn)
        base_tools[tool.name] = tool.fn
        base_schemas[tool.name] = {
            "description": tool.description,
            "parameters": tool.parameters,
        }
        base_metadata[tool.name] = tool.fn_metadata
        if os.environ.get("STARDEW_MCP_WIRE_STYLE") == "mcode":
            tool.parameters = mcode_tool_schema(tool.name, tool.parameters,
                wire_param_shapes if tool.name in {"submit_plan", "call_capability", "manage_plan"} else None)
    if resolved_surface == "light":
        register_game_tools(mcp, base_tools, declared_tools, _PLAN_OPERATION_CALLS,
                            execute_action=refresh_turn_context(execute_observed_action))
        if external_codex:
            status_tool = mcp._tool_manager.get_tool("get_status")
            original_status = status_tool.fn

            @functools.wraps(original_status)
            async def manual_connection_status(*args, **kwargs):
                result = await original_status(*args, **kwargs)
                return {**result, "connection": {"mode": "manual", "automaticWake": False,
                    "instruction": "Tools alone do not receive game chat or wake this conversation. Select the external source before manual actions; disconnect any automatic partner first."}}

            status_tool.fn = manual_connection_status
        if os.environ.get("STARDEW_MCP_WIRE_STYLE") == "mcode":
            for tool in mcp._tool_manager.list_tools():
                if tool.name in GAME_TOOLS:
                    tool.parameters = mcode_tool_schema(tool.name, tool.parameters)
    if resolved_surface == "light":
        allowed = LIGHT_TOOLS
    elif resolved_surface == "internal":
        allowed = INTERNAL_TOOLS
    else:
        allowed = None
    if allowed is not None:
        for name in sorted(base_tools):
            if name not in allowed:
                mcp.remove_tool(name)
    mcp.exposure_surface = resolved_surface  # type: ignore[attr-defined]

    return mcp


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stardew-mcp-server",
        description="Model Context Protocol (MCP) Server for Stardew AI Companion",
    )
    parser.add_argument(
        "--run-dir",
        type=str,
        default=os.getenv("STARDEW_RUN_DIR"),
        help="Path to run directory containing transport-discovery.json (or set STARDEW_RUN_DIR)",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="stardew-companion",
        help="Server name reported in MCP initialize",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force the complete legacy base tool list (default surface)",
    )
    parser.add_argument(
        "--surface",
        choices=["full", "light", "internal", "life"],
        default=None,
        help="Tool exposure surface: 'full' (generic default), 'light' (game), 'internal' (plan worker) or 'life' (legacy alias for light)",
    )
    parser.add_argument(
        "--light",
        action="store_true",
        help="Alias for --surface light (game surface, base tools via discover/call)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)

    run_dir = Path(args.run_dir) if args.run_dir else None
    surface = "light" if args.light else args.surface
    server = create_mcp_server(
        run_dir=run_dir, server_name=args.name, full=args.full or None, surface=surface
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
