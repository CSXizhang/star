"""Free-mode daily purchase budget must be enforced on the real plan dispatch path.

The model-facing ``purchase_items`` tool only *selects* a short job
(``protect_job`` interception); the actual purchase runs through the shared
plan worker: ``run_next_step`` -> ``PlanExecutor`` -> ``execute_plan_operation``
-> ``CompanionScheduler.execute_purchase_items`` -> native client. These tests
drive that whole chain with a fake transport client and assert the daily
budget is a program guarantee, not a prompt hint.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.protocol import Envelope
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.work_state import WorkStore

SAVE = "save-a"
CHEAP = "seed-cheap"  # price 10 in the fake shop snapshot
MID = "seed-mid"  # price 40


def _payload(day: int = 1) -> dict:
    return {
        "capturedRevision": 5,
        "companion": {
            "locationId": "SeedShop",
            "tileX": 5,
            "tileY": 10,
            "facingDirection": 2,
            "stamina": 250.0,
            "maxStamina": 270.0,
            "waterCanLevel": 38,
            "maxWaterCanLevel": 40,
            "hasWateringCan": True,
            "activity": "idle",
        },
        "world": {
            "currentLocation": "SeedShop",
            "timeOfDay": 610,
            "season": "spring",
            "dayOfMonth": day,
            "isRaining": False,
            "year": 1,
        },
        "shop": {
            "shopId": "SeedShop",
            "status": "open",
            "isOpen": True,
            "ownerPresent": True,
            "closedMessage": None,
            "owners": ["Pierre"],
            "currency": 0,
            "availableMoney": 1000,
            "moneyStatus": "ok",
            "errorMessage": None,
            "items": [
                {"itemId": CHEAP, "name": "Cheap Seed", "price": 10, "stock": -1},
                {"itemId": MID, "name": "Mid Seed", "price": 40, "stock": -1},
            ],
        },
    }


def _snapshot_env(payload: dict, revision: int = 5) -> Envelope:
    return Envelope.from_mapping({
        "protocolVersion": "0.1",
        "messageType": "world.snapshot",
        "messageId": "snap-1",
        "saveId": SAVE,
        "gameSessionId": "session-a",
        "senderInstanceId": "mod-instance-001",
        "sequenceNumber": 1,
        "worldRevision": revision,
        "sentAt": "2026-09-12T00:00:00Z",
        "payload": payload,
    })


def _purchase_result(command_id: str, terminal: str, total_cost: int) -> dict:
    return {
        "commandId": command_id,
        "taskId": "task-native",
        "terminalState": terminal,
        "completedCount": 0 if terminal == "failed" else 1,
        "skippedCount": 0,
        "failedCount": 1 if terminal == "failed" else 0,
        "effects": [],
        "details": {
            "shopId": "SeedShop",
            "totalCost": total_cost,
            "purchasedItems": [],
            "skippedItems": [],
        },
        "worldRevision": 6,
    }


def _make_client(results: list, cached: dict | None = None) -> MagicMock:
    """Fake transport client; ``results`` are consumed per wait_for_result call.

    Any wait beyond the scripted results fails the test loudly instead of
    silently returning a fresh MagicMock.
    """
    client = MagicMock()
    client.is_connected = True
    client.save_id = SAVE
    client.game_session_id = "session-a"
    client.world_revision = 5
    client.connect = AsyncMock()
    client.handshake = AsyncMock()
    client.close = AsyncMock()
    client.latest_snapshot = _snapshot_env(_payload())
    client.wait_for_snapshot = AsyncMock(return_value=client.latest_snapshot)
    # The Mod echoes the command id it was sent; reconciliation keys on it.
    client.execute_purchase_items = AsyncMock(
        side_effect=lambda **kwargs: kwargs.get("command_id") or "anon-cmd"
    )
    scripted = list(results) + [AssertionError("unexpected extra wait_for_result call")]
    client.wait_for_result = AsyncMock(side_effect=scripted)
    cache: dict[str, object] = dict(cached or {})
    client.get_cached_result = MagicMock(side_effect=lambda command_id: cache.get(command_id))
    client.cache_result = lambda command_id, payload: cache.__setitem__(
        command_id, SimpleNamespace(payload=payload)
    )
    return client


def _autonomy(run_dir: Path) -> AutonomyController:
    return AutonomyController(Path(run_dir) / "data" / "autonomy-state.json")


def _work_store(run_dir: Path) -> WorkStore:
    return WorkStore(Path(run_dir) / "data" / "work-state.json")


def _free_mode(run_dir: Path, budget: int | None) -> None:
    ctl = _autonomy(run_dir)
    ctl.set_mode(SAVE, "free")
    if budget is not None:
        ctl.set_preferences(SAVE, budget_limit=budget)


async def _run_purchase_plan(
    server, store: WorkStore, items: list[dict], budget_limit: int, token: str
) -> dict:
    """Select one purchase short job under a fresh decision, then execute it."""
    store.begin_decision(SAVE, token)
    with patch.dict(os.environ, {"STARDEW_DECISION_TOKEN": token}):
        _, selected = await server.call_tool(
            "purchase_items", {"items": items, "budget_limit": budget_limit}
        )
    assert selected["status"] == "job-selected"
    _, execution = await server.call_tool("run_next_step", {})
    return execution


def test_single_purchase_cannot_exceed_remaining_daily_budget(native_compatible_run_dir) -> None:
    """One purchase whose native quote exceeds the daily budget is rejected
    before dispatch, even when the model authorized a far larger budget_limit."""
    _free_mode(native_compatible_run_dir, budget=100)
    client = _make_client([SimpleNamespace(payload=_purchase_result("x", "succeeded", 120))])
    scheduler = CompanionScheduler(client=client, run_dir=native_compatible_run_dir)
    server = create_mcp_server(scheduler=scheduler, full=True)
    store = _work_store(native_compatible_run_dir)

    execution = asyncio.run(
        _run_purchase_plan(server, store, [{"itemId": MID, "count": 3}], 500, "decision-1")
    )

    client.execute_purchase_items.assert_not_called()
    assert execution["outcome"] == "partial"
    assert execution["reasonCode"] == "AUTONOMY_BUDGET_EXHAUSTED"
    state = _autonomy(native_compatible_run_dir).state(SAVE)
    assert state.daily_spend == 0
    assert state.spend_reservations == {}


def test_cumulative_purchases_cannot_exceed_daily_budget(native_compatible_run_dir) -> None:
    """Two 40-cost purchases drain a budget of 100; the third is rejected by
    the program, not merely advised against."""
    _free_mode(native_compatible_run_dir, budget=100)
    client = _make_client([
        SimpleNamespace(payload=_purchase_result("x", "succeeded", 40)),
        SimpleNamespace(payload=_purchase_result("x", "succeeded", 40)),
    ])
    scheduler = CompanionScheduler(client=client, run_dir=native_compatible_run_dir)
    server = create_mcp_server(scheduler=scheduler, full=True)
    store = _work_store(native_compatible_run_dir)

    items = [{"itemId": MID, "count": 1}]
    first = asyncio.run(_run_purchase_plan(server, store, items, 40, "decision-1"))
    second = asyncio.run(_run_purchase_plan(server, store, items, 40, "decision-2"))
    third = asyncio.run(_run_purchase_plan(server, store, items, 40, "decision-3"))

    assert first["outcome"] == "completed"
    assert second["outcome"] == "completed"
    assert third["outcome"] == "partial"
    assert third["reasonCode"] == "AUTONOMY_BUDGET_EXHAUSTED"
    assert client.execute_purchase_items.await_count == 2
    state = _autonomy(native_compatible_run_dir).state(SAVE)
    assert state.daily_spend == 80
    assert state.spend_reservations == {}


def test_same_command_id_retry_is_idempotent_and_settles_once(native_compatible_run_dir) -> None:
    """A retry with the same command id reuses the in-flight native task (no
    second dispatch, no double charge); once settled, the same id replays."""
    _free_mode(native_compatible_run_dir, budget=100)
    client = _make_client([
        TimeoutError(),
        SimpleNamespace(payload=_purchase_result("native-cmd-x", "succeeded", 40)),
    ])
    scheduler = CompanionScheduler(client=client, run_dir=native_compatible_run_dir)

    async def run() -> None:
        first = await scheduler.execute_purchase_items(
            items=[{"itemId": MID, "count": 1}], budget_limit=40,
            command_id="cmd-x", timeout_seconds=0.01,
        )
        assert first["terminalState"] == "running"
        state = _autonomy(native_compatible_run_dir).state(SAVE)
        assert state.spend_reservations == {"cmd-x": 40}
        assert state.daily_spend == 0

        second = await scheduler.execute_purchase_items(
            items=[{"itemId": MID, "count": 1}], budget_limit=40,
            command_id="cmd-x", timeout_seconds=0.01,
        )
        assert second["terminalState"] == "succeeded"
        # The retry must not dispatch a second native purchase.
        assert client.execute_purchase_items.await_count == 1
        state = _autonomy(native_compatible_run_dir).state(SAVE)
        assert state.daily_spend == 40
        assert state.spend_reservations == {}

        third = await scheduler.execute_purchase_items(
            items=[{"itemId": MID, "count": 1}], budget_limit=40,
            command_id="cmd-x", timeout_seconds=0.01,
        )
        assert third["status"] == "replayed"
        assert client.execute_purchase_items.await_count == 1
        state = _autonomy(native_compatible_run_dir).state(SAVE)
        assert state.daily_spend == 40
        assert state.settled_spend_commands == ["cmd-x"]

    asyncio.run(run())


def test_unknown_result_keeps_reservation_and_settles_once_on_reconcile(
    native_compatible_run_dir,
) -> None:
    """A timed-out purchase keeps its reservation; the next purchase reconciles
    it and settles exactly once before its own dispatch."""
    _free_mode(native_compatible_run_dir, budget=100)
    client = _make_client([
        TimeoutError(),
        SimpleNamespace(payload=_purchase_result("native-2", "succeeded", 10)),
    ])
    scheduler = CompanionScheduler(client=client, run_dir=native_compatible_run_dir)
    server = create_mcp_server(scheduler=scheduler, full=True)
    store = _work_store(native_compatible_run_dir)

    first = asyncio.run(
        _run_purchase_plan(server, store, [{"itemId": MID, "count": 1}], 40, "decision-1")
    )
    assert first["outcome"] == "unknown"
    state = _autonomy(native_compatible_run_dir).state(SAVE)
    assert state.daily_spend == 0
    assert list(state.spend_reservations.values()) == [40]
    pending_id = next(iter(state.spend_reservations))

    # The Mod finishes the purchase in the background; its result is cached.
    client.cache_result(pending_id, _purchase_result(pending_id, "succeeded", 40))

    second = asyncio.run(
        _run_purchase_plan(server, store, [{"itemId": CHEAP, "count": 1}], 10, "decision-2")
    )
    assert second["outcome"] == "completed"
    assert client.execute_purchase_items.await_count == 2
    state = _autonomy(native_compatible_run_dir).state(SAVE)
    assert state.daily_spend == 50  # 40 settled once + 10
    assert state.spend_reservations == {}
    assert sorted(state.settled_spend_commands) == sorted([pending_id, second["commandId"]])


def test_terminal_results_settle_confirmed_cost_and_failure_refunds(
    native_compatible_run_dir,
) -> None:
    """Success, failure and partial fill all settle by the confirmed real cost;
    a failed purchase releases its reservation without charging."""
    _free_mode(native_compatible_run_dir, budget=100)
    client = _make_client([
        SimpleNamespace(payload=_purchase_result("x", "succeeded", 40)),
        SimpleNamespace(payload=_purchase_result("x", "failed", 0)),
        SimpleNamespace(payload=_purchase_result("x", "succeeded", 10)),
    ])
    scheduler = CompanionScheduler(client=client, run_dir=native_compatible_run_dir)
    server = create_mcp_server(scheduler=scheduler, full=True)
    store = _work_store(native_compatible_run_dir)

    ok = asyncio.run(_run_purchase_plan(server, store, [{"itemId": MID, "count": 1}], 40, "decision-1"))
    failed = asyncio.run(_run_purchase_plan(server, store, [{"itemId": MID, "count": 1}], 40, "decision-2"))
    partial = asyncio.run(_run_purchase_plan(server, store, [{"itemId": CHEAP, "count": 2}], 20, "decision-3"))

    assert ok["outcome"] == "completed"
    assert failed["outcome"] == "partial"  # native terminal failed -> step deviation, cost refunded
    assert partial["outcome"] == "completed"
    assert client.execute_purchase_items.await_count == 3
    state = _autonomy(native_compatible_run_dir).state(SAVE)
    assert state.daily_spend == 50  # 40 + 0 (failure) + 10 (partial fill)
    assert state.spend_reservations == {}
    assert sorted(state.settled_spend_commands) == sorted(
        [ok["commandId"], failed["commandId"], partial["commandId"]]
    )


def test_new_day_resets_budget_and_carries_unsettled_reservation(
    native_compatible_run_dir,
) -> None:
    """Day rollover (autonomy.on_day_started via next_candidate) zeroes the
    spend but keeps the unsettled reservation; it is settled once when the next
    purchase reconciles it, and the new day's budget is enforced on top."""
    _free_mode(native_compatible_run_dir, budget=100)
    client = _make_client([
        SimpleNamespace(payload=_purchase_result("native-x1", "succeeded", 40)),
        TimeoutError(),
        SimpleNamespace(payload=_purchase_result("native-z", "succeeded", 50)),
    ])
    scheduler = CompanionScheduler(client=client, run_dir=native_compatible_run_dir)
    server = create_mcp_server(scheduler=scheduler, full=True)
    store = _work_store(native_compatible_run_dir)

    # Day 1: one confirmed purchase (40) and one that times out (reservation 40).
    day1a = asyncio.run(_run_purchase_plan(server, store, [{"itemId": MID, "count": 1}], 40, "decision-1"))
    assert day1a["outcome"] == "completed"
    day1b = asyncio.run(_run_purchase_plan(server, store, [{"itemId": MID, "count": 1}], 40, "decision-2"))
    assert day1b["outcome"] == "unknown"
    ctl = _autonomy(native_compatible_run_dir)
    state = ctl.state(SAVE)
    assert state.daily_spend == 40
    pending_id = next(iter(state.spend_reservations))

    # The Mod cached the timed-out command's terminal result afterwards.
    client.cache_result(pending_id, _purchase_result(pending_id, "succeeded", 40))

    # Day rollover through the same path chat_bridge uses.
    ctl.next_candidate(SAVE, {"world": {"year": 1, "season": "spring", "dayOfMonth": 2}})
    state = ctl.state(SAVE)
    assert state.daily_spend == 0
    assert list(state.spend_reservations.values()) == [40]  # not lost across the day boundary

    # Without the reset only 100-40(spent)-40(reserved)=20 would remain, so a
    # 70-quote purchase is rejected on the fresh day after the pending one is
    # settled once (remaining 100-40=60).
    rejected = asyncio.run(
        _run_purchase_plan(server, store, [{"itemId": MID, "count": 1}, {"itemId": CHEAP, "count": 3}], 70, "decision-3")
    )
    assert rejected["outcome"] == "partial"
    assert rejected["reasonCode"] == "AUTONOMY_BUDGET_EXHAUSTED"
    assert client.execute_purchase_items.await_count == 2

    # A 50-quote purchase fits the remaining 60 and completes the day at 90.
    ok = asyncio.run(
        _run_purchase_plan(server, store, [{"itemId": MID, "count": 1}, {"itemId": CHEAP, "count": 1}], 50, "decision-4")
    )
    assert ok["outcome"] == "completed"
    assert client.execute_purchase_items.await_count == 3
    state = _autonomy(native_compatible_run_dir).state(SAVE)
    assert state.daily_spend == 90
    assert state.spend_reservations == {}
    assert sorted(state.settled_spend_commands) == sorted(
        [day1a["commandId"], pending_id, ok["commandId"]]
    )


def test_command_mode_purchase_is_not_budget_gated(native_compatible_run_dir) -> None:
    """指令(命令)模式 keeps the pre-existing behavior: no daily-budget gate."""
    client = _make_client([SimpleNamespace(payload=_purchase_result("x", "succeeded", 10))])
    scheduler = CompanionScheduler(client=client, run_dir=native_compatible_run_dir)
    server = create_mcp_server(scheduler=scheduler, full=True)
    store = _work_store(native_compatible_run_dir)

    execution = asyncio.run(
        _run_purchase_plan(server, store, [{"itemId": CHEAP, "count": 1}], 999999, "decision-1")
    )

    assert execution["outcome"] == "completed"
    assert client.execute_purchase_items.await_count == 1
    state = _autonomy(native_compatible_run_dir).state(SAVE)
    assert state.daily_spend == 0
    assert state.spend_reservations == {}


def test_free_mode_without_configured_budget_rejects_purchases(native_compatible_run_dir) -> None:
    """Zero budget (free mode, no preference set) rejects every purchase."""
    _free_mode(native_compatible_run_dir, budget=None)
    client = _make_client([SimpleNamespace(payload=_purchase_result("x", "succeeded", 10))])
    scheduler = CompanionScheduler(client=client, run_dir=native_compatible_run_dir)
    server = create_mcp_server(scheduler=scheduler, full=True)
    store = _work_store(native_compatible_run_dir)

    execution = asyncio.run(
        _run_purchase_plan(server, store, [{"itemId": CHEAP, "count": 1}], 10, "decision-1")
    )

    client.execute_purchase_items.assert_not_called()
    assert execution["reasonCode"] == "AUTONOMY_BUDGET_EXHAUSTED"
