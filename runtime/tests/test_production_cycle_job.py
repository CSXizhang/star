from stardew_ai_runtime.work_state import WorkStore


def test_machine_operations_and_delivery_are_not_business_classified():
    collect = {"operation": "collect_machine", "params": {"location_id": "Farm", "tiles": [{"x": 5, "y": 5}]}}
    deposit = {"operation": "deposit_to_chest", "params": {"location_id": "FarmHouse", "chest_x": 7, "chest_y": 5, "item_ids": ["(O)306"]}}
    insert = {"operation": "insert_machine", "params": {"location_id": "Farm", "tile": {"x": 10, "y": 10}, "item_id": "(O)176"}}
    # Each native interaction checks its current world preconditions when dispatched.
    WorkStore.validate_short_job([{"steps": [collect, deposit, insert]}])
    WorkStore.validate_short_job([{"steps": [insert, collect, deposit]}])
