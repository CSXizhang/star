"""Observations complete the read step, without claiming a native mutation."""

import asyncio

import pytest

from stardew_ai_runtime.plan_executor import PlanExecutor, classify_step_outcome
from stardew_ai_runtime.work_state import WorkStore


@pytest.mark.parametrize("operation,result", [
    ("query_shop", {"status": "ok", "isOpen": False, "items": []}),
    ("observe_production", {"groundItems": []}),
    ("observe_livestock", {"buildings": []}),
    ("observe_machines", {"machines": []}),
    ("observe_farm_space", {"rows": []}),
    ("observe_farming_helpers", {"groundItems": []}),
    ("observe_crafting", {"recipes": []}),
    ("observe_building_services", {"animalService": {"available": False}}),
    ("query_planting_options", {"regions": []}),
    ("query_planting_options", {"candidateTiles": []}),
    ("query_inventory", {"inventory": {"slots": []}}),
    ("query_chests", {"chests": []}),
    ("query_farm_work", {"farmWork": {"matureCropCount": 0}}),
    ("get_status", {"companion": {"activity": "idle"}}),
    ("get_work_overview", {"tasks": []}),
    ("query_wiki", {"results": []}),
])
def test_available_read_contract(operation, result):
    assert classify_step_outcome(result, operation) == ("completed", None)


@pytest.mark.parametrize("result", [
    {}, {"status": "unknown"}, {"status": "missing"}, {"status": "error"},
    {"status": "ok", "error": "missing snapshot"}, {"isError": True},
    {"status": "unknown", "items": []}, {"unexpected": True},
])
def test_unavailable_read_does_not_complete(result):
    assert classify_step_outcome(result, "query_shop")[0] != "completed"


def test_write_ok_is_not_native_completion():
    assert classify_step_outcome({"status": "ok"}, "purchase_items")[0] == "unknown"


def test_shop_read_completes_but_preserves_facts_for_native_purchase(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal = store.add_goal("save", "采购", source="user")
    store.begin_decision("save", "decision")
    store.submit_plan("save", goal_id=goal.id, decision_token="decision", tasks=[{
        "title": "复核并采购", "steps": [
            {"operation": "query_shop", "params": {"shop_id": "AnimalShop"}},
            {"operation": "purchase_items", "params": {
                "shop_id": "AnimalShop", "items": [{"item_id": "(O)178", "quantity": 1}],
                "budget_limit": 50,
            }},
        ],
    }])
    facts = {"status": "ok", "isOpen": False, "ownerPresent": False, "items": []}
    calls = []

    async def dispatch(operation, params, command_id):
        calls.append(operation)
        return facts if operation == "query_shop" else {
            "terminalState": "rejected", "reasonCode": "SHOP_CLOSED",
        }

    async def run():
        executor = PlanExecutor(store, dispatch=dispatch)
        read = await executor.run_once("save", "worker")
        assert read.outcome == "completed" and read.result == facts and read.effects == []
        purchase = await executor.run_once("save", "worker")
        assert purchase.outcome == "partial" and purchase.reason_code == "SHOP_CLOSED"

    asyncio.run(run())
    assert calls == ["query_shop", "purchase_items"]
