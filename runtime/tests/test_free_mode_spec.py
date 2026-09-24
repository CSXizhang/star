"""Focused free-mode acceptance tests; these do not require Stardew Valley or an LLM."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from stardew_ai_runtime.agent_backends import BackendProgress
from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.protocol import Envelope
from stardew_ai_runtime.scheduler import CompanionScheduler, requested_task_id


def _snapshot(day: int = 1, *, mature: int = 0) -> dict:
    return {"world": {"year": 1, "season": "spring", "dayOfMonth": day},
            "farmWork": {"matureCropCount": mature, "cropUnwateredTiles": []}}


def test_enable_requires_no_goal_text_and_control_protocol_round_trips() -> None:
    msg = Envelope.create_autonomy_control("mod", "req-1", "save-a", "set_mode", mode="free")
    assert msg.message_type == "autonomy.control"
    assert msg.payload["parameters"] == {"mode": "free"}
    state = Envelope.create_autonomy_state("bridge", "req-1", "save-a", {
        "mode": "free", "paused": False, "preferences": {},
        "preferencesRevision": 0, "decisionEpoch": 1, "gameDate": None, "dailySpend": 0,
    })
    assert state.message_type == "autonomy.state" and state.payload["requestId"] == "req-1"


def test_old_enabled_save_migrates_to_free_without_spend_permission(tmp_path: Path) -> None:
    path = tmp_path / "autonomy.json"
    path.write_text(json.dumps({"save-a": {"enabled": True, "budget_limit": None}}), encoding="utf-8")
    state = AutonomyController(path).state("save-a")
    assert state.mode == "free" and state.daily_spend == 0 and state.budget_limit is None


def test_first_day_and_new_day_each_allow_one_planning_wakeup(tmp_path: Path) -> None:
    ctl = AutonomyController(tmp_path / "state.json")
    ctl.set_mode("save-a", "free")
    first = ctl.next_candidate("save-a", _snapshot(1))
    assert first and first["kind"] == "decision"
    fp = ctl.fingerprint("save-a", _snapshot(1), first, ctl.state("save-a"))
    assert ctl.record_world_event("save-a", fp, 1)
    assert ctl.next_candidate("save-a", _snapshot(1)) is None
    ctl.on_day_started("save-a", 2)
    second = ctl.next_candidate("save-a", _snapshot(2))
    assert second and second["kind"] == "decision"


def test_identical_snapshots_have_zero_additional_wakeups_and_terminal_is_single_shot(tmp_path: Path) -> None:
    ctl = AutonomyController(tmp_path / "state.json")
    ctl.set_mode("save-a", "free")
    snap = _snapshot(mature=1)
    candidate = ctl.next_candidate("save-a", snap)
    fp = ctl.fingerprint("save-a", snap, candidate)
    assert ctl.record_world_event("save-a", fp, 2)
    ctl.record_action_result("save-a", fp, True)
    assert not ctl.record_world_event("save-a", fp, 3)


def test_fingerprint_uses_real_world_snapshot_fixture_and_tolerates_missing_shapes(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[2] / "protocol" / "examples" / "world-snapshot.json"
    snapshot = json.loads(fixture.read_text(encoding="utf-8"))["payload"]
    ctl = AutonomyController(tmp_path / "state.json")
    ctl.set_mode("save-a", "free")
    candidate = ctl.next_candidate("save-a", snapshot)
    assert candidate is not None
    fingerprint = ctl.fingerprint("save-a", snapshot, candidate, ctl.state("save-a"))
    assert "spring:1" in fingerprint and "(O)24" in fingerprint
    safe = dict(snapshot, chests="invalid", inventory=None, shop=None, farmWork=None)
    assert isinstance(ctl.fingerprint("save-a", safe, candidate), str)


def test_pause_cancel_and_save_switch_do_not_resume_old_autonomy(tmp_path: Path) -> None:
    ctl = AutonomyController(tmp_path / "state.json")
    ctl.set_mode("save-a", "free")
    ctl.control("save-a", "pause")
    assert ctl.next_candidate("save-a", _snapshot()) is None
    ctl.control("save-a", "cancel")
    assert ctl.state("save-a").mode == "command"
    assert ctl.state("save-b").mode == "command"


def test_player_request_preempts_autonomy_generation(tmp_path: Path) -> None:
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._active_task = ActiveChatTask("autonomy-old", "save-a", "autonomous work")
    old_generation = bridge._autonomy_generation
    assert bridge._preempt_autonomy_for_player("player-1") is True
    assert bridge._active_task is None and bridge._autonomy_generation == old_generation + 1


def test_quota_failure_pauses_free_mode_and_explains_reason(tmp_path: Path) -> None:
    async def run() -> None:
        bridge = ChatBridge(run_dir=tmp_path)
        bridge._autonomy.set_mode("save-a", "free")
        ws = AsyncMock()
        with patch.object(bridge, "_execute_turn", return_value={
            "success": False, "response": "", "error": "RESOURCE_EXHAUSTED",
            "conversation_id": None, "duration": 0.1,
        }):
            await bridge.handle_chat_submit(ws, "req-quota", "只读", "save-a")
        assert bridge._autonomy.state("save-a").paused is True
        final = json.loads(ws.send_text.call_args_list[-1].args[0])["payload"]
        assert final["status"] == "failed"
        assert "自由模式已暂停" in final["replyText"]

    asyncio.run(run())


def test_backend_tool_progress_is_forwarded_as_processing_reply() -> None:
    async def run() -> None:
        bridge = ChatBridge(backend="kimi")
        ws = AsyncMock()
        bridge._configure_backend_progress(ws, "req-tool", "save-a")
        bridge._backend_progress_callback(BackendProgress("kimi", "tool_started", "ignored", "mcp__stardew-companion__get_status"))
        await asyncio.sleep(0.01)
        payload = json.loads(ws.send_text.call_args.args[0])["payload"]
        assert payload["status"] == "processing"
        assert "get_status" in payload["replyText"]

    asyncio.run(run())


def test_budget_and_box_defaults_preserve_free_labor(tmp_path: Path) -> None:
    ctl = AutonomyController(tmp_path / "state.json")
    state = ctl.state("save-a")
    assert state.budget_limit is None or state.budget_limit == 0
    assert state.box_preference == "none"
    ctl.set_preferences("save-a", box_preference="any")
    assert ctl.state("save-a").box_preference == "any"  # explicit legacy preference remains valid
    assert ctl.next_candidate("save-a", {"farmWork": {"cropUnwateredTiles": [{"x": 1, "y": 1}]}}) is None


def test_purchase_accounting_covers_cumulative_replay_and_unknown_result(tmp_path: Path) -> None:
    ctl = AutonomyController(tmp_path / "state.json")
    ctl.set_mode("save-a", "free")
    ctl.set_preferences("save-a", budget_limit=10)
    ctl.reserve_spend("save-a", "a", 4)
    ctl.settle_spend("save-a", "a", 3)
    ctl.reserve_spend("save-a", "b", 4)
    ctl.settle_spend("save-a", "b", None, unknown=True)
    assert ctl.state("save-a").daily_spend == 3
    assert ctl.reserve_spend("save-a", "a", 4) == 0
    assert ctl.state("save-a").spend_reservations["b"] == 4


def test_free_purchase_entry_uses_native_quote_min_budget_and_command_replay(tmp_path: Path, provider_decision_context) -> None:
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.latest_world_revision = 1
    scheduler.get_status = AsyncMock(return_value={"saveId": "save-a"})
    scheduler.get_status = AsyncMock(return_value={"saveId": "save-a"})
    scheduler.wait_for_fresh_snapshot = AsyncMock(return_value=(None, False))
    scheduler.query_shop = AsyncMock(return_value={"items": [{"itemId": "seed", "price": 2}]})
    scheduler.execute_purchase_items = AsyncMock(return_value={
        "terminalState": "succeeded", "completedCount": 2, "skippedCount": 0, "failedCount": 0,
        "details": {"totalCost": 4, "remainingBudget": 6, "purchasedItems": [], "skippedItems": []},
    })
    ctl = AutonomyController(tmp_path / "data" / "autonomy-state.json")
    ctl.set_mode("save-a", "free")
    ctl.set_preferences("save-a", budget_limit=6)

    async def run() -> None:
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, full=True)
        _, result = await server.call_tool("purchase_items", {
            "items": [{"itemId": "seed", "count": 2}], "budget_limit": 99, "command_id": "cmd-1"
        })
        assert result["totalCost"] == 4
        scheduler.execute_purchase_items.assert_awaited_once_with(
            items=[{"itemId": "seed", "count": 2}], budget_limit=6, shop_id="SeedShop", command_id="cmd-1"
        )
        _, replay = await server.call_tool("purchase_items", {
            "items": [{"itemId": "seed", "count": 2}], "budget_limit": 99, "command_id": "cmd-1"
        })
        assert replay["status"] == "replayed"
        assert scheduler.execute_purchase_items.await_count == 1

    asyncio.run(run())
    assert AutonomyController(tmp_path / "data" / "autonomy-state.json").state("save-a").daily_spend == 4


def test_free_purchase_daily_limit_reservations_are_not_double_counted(tmp_path: Path, provider_decision_context) -> None:
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.latest_world_revision = 1
    scheduler.get_status = AsyncMock(return_value={"saveId": "save-a"})
    scheduler.wait_for_fresh_snapshot = AsyncMock(return_value=(None, False))
    scheduler.query_shop = AsyncMock(return_value={"items": [{"itemId": "seed", "price": 40}]})
    scheduler.execute_purchase_items = AsyncMock(side_effect=[
        {"terminalState": "succeeded", "details": {"totalCost": 40}},
        {"terminalState": "succeeded", "details": {"totalCost": 40}},
    ])
    ctl = AutonomyController(tmp_path / "data" / "autonomy-state.json")
    ctl.set_mode("save-a", "free")
    ctl.set_preferences("save-a", budget_limit=100)
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, full=True)

    async def run() -> None:
        for command_id in ("cmd-a", "cmd-b"):
            _, result = await server.call_tool("purchase_items", {
                "items": [{"itemId": "seed", "count": 1}],
                "budget_limit": 40,
                "command_id": command_id,
            })
            assert result["totalCost"] == 40
        _, rejected = await server.call_tool("purchase_items", {
            "items": [{"itemId": "seed", "count": 1}],
            "budget_limit": 40,
            "command_id": "cmd-c",
        })
        assert rejected["status"] == "rejected"
        assert rejected["error"] == "AUTONOMY_BUDGET_EXHAUSTED"

    asyncio.run(run())
    state = AutonomyController(tmp_path / "data" / "autonomy-state.json").state("save-a")
    assert state.daily_spend == 80
    assert scheduler.execute_purchase_items.await_count == 2


def test_free_purchase_unknown_terminal_keeps_reservation(tmp_path: Path, provider_decision_context) -> None:
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.latest_world_revision = 1
    scheduler.get_status = AsyncMock(return_value={"saveId": "save-a"})
    scheduler.wait_for_fresh_snapshot = AsyncMock(return_value=(None, False))
    scheduler.query_shop = AsyncMock(return_value={"items": [{"itemId": "seed", "price": 10}]})
    scheduler.execute_purchase_items = AsyncMock(return_value={"terminalState": "unknown", "details": {}})
    ctl = AutonomyController(tmp_path / "data" / "autonomy-state.json")
    ctl.set_mode("save-a", "free")
    ctl.set_preferences("save-a", budget_limit=100)

    async def run() -> None:
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, full=True)
        _, result = await server.call_tool("purchase_items", {
            "items": [{"itemId": "seed", "count": 1}], "budget_limit": 10, "command_id": "cmd-unknown"
        })
        assert result["terminalState"] == "unknown"

    asyncio.run(run())
    state = AutonomyController(tmp_path / "data" / "autonomy-state.json").state("save-a")
    assert state.daily_spend == 0
    assert state.spend_reservations == {"cmd-unknown": 10}


def test_pending_purchase_reconnect_does_not_blind_redispatch(tmp_path: Path, provider_decision_context) -> None:
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    scheduler.latest_world_revision = 1
    scheduler.get_status = AsyncMock(return_value={"saveId": "save-a"})
    scheduler.active_task = None
    scheduler.reconcile_command = AsyncMock(return_value=None)
    scheduler.execute_purchase_items = AsyncMock()
    ctl = AutonomyController(tmp_path / "data" / "autonomy-state.json")
    ctl.set_mode("save-a", "free")
    ctl.set_preferences("save-a", budget_limit=100)
    ctl.reserve_spend("save-a", "cmd-pending", 40)

    async def run() -> None:
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, full=True)
        _, result = await server.call_tool("purchase_items", {
            "items": [{"itemId": "seed", "count": 1}], "budget_limit": 40, "command_id": "cmd-pending"
        })
        assert result["status"] == "executing"
        assert result["terminalState"] == "running"
        _, blocked = await server.call_tool("purchase_items", {
            "items": [{"itemId": "seed", "count": 1}], "budget_limit": 40, "command_id": "cmd-next"
        })
        assert blocked["status"] == "rejected"
        assert blocked["error"] == "AUTONOMY_PENDING_RECONCILIATION"

    asyncio.run(run())
    assert scheduler.reconcile_command.await_count == 2
    scheduler.execute_purchase_items.assert_not_awaited()


def test_purchase_command_retry_reuses_task_and_idempotency_without_dispatch(tmp_path: Path, bound_native_game) -> None:
    client = MagicMock()
    client.is_connected = True
    client.save_id = "save-a"
    client.game_session_id = "session-a"
    client.world_revision = 7
    client.execute_purchase_items = AsyncMock(return_value="cmd-real")
    client.wait_for_result = AsyncMock(side_effect=[
        TimeoutError(),
        SimpleNamespace(payload={"terminalState": "succeeded", "details": {"totalCost": 0}}),
    ])
    scheduler = CompanionScheduler(client=client, run_dir=bound_native_game)

    async def run() -> None:
        first = await scheduler.execute_purchase_items(
            items=[{"itemId": "seed", "count": 1}], budget_limit=40,
            command_id="cmd-requested", timeout_seconds=0.01,
        )
        assert first["terminalState"] == "running"
        second = await scheduler.execute_purchase_items(
            items=[{"itemId": "seed", "count": 1}], budget_limit=40,
            command_id="cmd-requested", timeout_seconds=0.01,
        )
        assert second["terminalState"] == "succeeded"

    asyncio.run(run())
    assert client.execute_purchase_items.await_count == 1
    first_dispatch = client.execute_purchase_items.await_args.kwargs
    assert first_dispatch["command_id"] == "cmd-requested"
    # A stable plan command id derives a deterministic task id, so the native
    # idempotency key is stable across a retry of the same attempt.
    assert first_dispatch["task_id"] == requested_task_id("cmd-requested")
    assert first_dispatch["idempotency_key"] == f"save-a:{first_dispatch['task_id']}:attempt-1"
