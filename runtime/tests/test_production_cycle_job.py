import pytest

from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def test_one_machine_cycle_delivers_and_reinserts_only_its_own_machines():
    collect = {"operation": "collect_machine", "params": {"tiles": [{"x": 5, "y": 5}]}}
    deposit = {"operation": "deposit_to_chest", "params": {"chest_x": 7, "chest_y": 5, "item_ids": ["(O)306"]}}
    insert = {"operation": "insert_machine", "params": {"tile": {"x": 5, "y": 5}, "item_id": "(O)176"}}
    WorkStore.validate_short_job([{"steps": [collect, deposit, insert]}])
    for wrong in ([collect, insert, deposit], [insert, collect]):
        with pytest.raises(WorkStateError, match="PRODUCTION_CYCLE_REQUIRED"):
            WorkStore.validate_short_job([{"steps": wrong}])
    insert["params"]["tile"] = {"x": 10, "y": 10}
    with pytest.raises(WorkStateError, match="PRODUCTION_CYCLE_REQUIRED"):
        WorkStore.validate_short_job([{"steps": [collect, insert]}])
