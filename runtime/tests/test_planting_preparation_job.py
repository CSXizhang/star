from stardew_ai_runtime.work_state import WorkStore


def test_preparation_planting_and_exact_water_are_one_serial_job():
    tiles = [{"x": x, "y": 5} for x in range(100)]
    steps = [{"operation": "navigate_to", "params": {"location_id": "FarmHouse", "tile": {"x": 1, "y": 1}}},
             {"operation": "withdraw_from_chest", "params": {"location_id": "FarmHouse", "chest_x": 1, "chest_y": 1,
                                                              "items": [{"itemId": "(O)475", "count": 100}]}},
             {"operation": "navigate_to", "params": {"location_id": "Farm", "tile": {"x": 5, "y": 5}}},
             {"operation": "hoe_tiles", "params": {"location_id": "Farm", "tiles": tiles}},
             {"operation": "plant_seeds", "params": {"location_id": "Farm", "seed_item_id": "(O)475", "tiles": tiles}},
             {"operation": "water_tiles", "params": {"location_id": "Farm", "tiles": tiles}}]
    WorkStore.validate_short_job([{"steps": steps}])


def test_operations_are_not_reclassified_as_different_businesses():
    WorkStore.validate_short_job([{"steps": [
        {"operation": "plant_seeds", "params": {"tiles": [{"x": 69, "y": 18}, {"x": 64, "y": 23}]}},
        {"operation": "water_auto", "params": {"max_tiles": 100}},
        {"operation": "refill_watering_can", "params": {}},
    ]}])
