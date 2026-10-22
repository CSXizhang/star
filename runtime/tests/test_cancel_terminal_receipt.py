import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.work_state import ExecutionEntry, WorkStore


def setup(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._work_store = WorkStore(tmp_path / "work.json")
    client = SimpleNamespace(reconcile=AsyncMock(side_effect=RuntimeError("IPC closed")))
    bridge._plan_worker = SimpleNamespace(client=client)
    return bridge, client


def test_recorded_cancelled_native_effects_confirm_without_live_ipc(tmp_path):
    bridge, client = setup(tmp_path)
    entry = ExecutionEntry("owned", "task", "step", "pickup_items", "cancelled",
                           [{"state": "picked-up", "stack": 7}, {"state": "skipped", "reason": "inventory-full"}],
                           reason_code="CANCELLED")
    bridge._work_store._mutate("save", lambda state: state.executions.append(entry))
    assert asyncio.run(bridge._confirm_native_terminal("owned", "save"))
    client.reconcile.assert_not_awaited()
    assert bridge._work_store.state("save").executions[0].effects == entry.effects


def test_unknown_other_save_or_other_command_cannot_confirm(tmp_path):
    bridge, client = setup(tmp_path)
    bridge._work_store._mutate("other", lambda state: state.executions.append(
        ExecutionEntry("owned", "task", "step", "pickup_items", "cancelled", [])))
    bridge._work_store._mutate("save", lambda state: state.executions.extend([
        ExecutionEntry("different", "task", "step", "pickup_items", "completed", []),
        ExecutionEntry("owned", "task", "step", "pickup_items", "unknown", [], reason_code="NATIVE_TERMINAL_UNCONFIRMED"),
    ]))
    assert not asyncio.run(bridge._confirm_native_terminal("owned", "save"))
    assert not asyncio.run(bridge._confirm_native_terminal("missing", "save"))
    assert client.reconcile.await_count == 2


def test_receipt_arriving_during_failed_reconcile_still_confirms(tmp_path):
    bridge, client = setup(tmp_path)

    async def closes_after_callback(command_id):
        bridge._work_store._mutate("save", lambda state: state.executions.append(
            ExecutionEntry(command_id, "task", "step", "pickup_items", "cancelled", [], reason_code="CANCELLED")))
        raise RuntimeError("IPC closed after native callback")

    client.reconcile.side_effect = closes_after_callback
    assert asyncio.run(bridge._confirm_native_terminal("owned", "save"))
    client.reconcile.assert_awaited_once_with("owned")
