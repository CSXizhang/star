from stardew_ai_runtime.work_state import WorkStore


def test_arrival_observation_and_related_native_actions_share_one_serial_job():
    steps = [{"operation": "navigate_to", "params": {"location_id": "AnimalShop"}},
             {"operation": "observe_building_services", "params": {"location_id": "AnimalShop"}},
             {"operation": "purchase_animal", "params": {"location_id": "Farm", "animal_type": "Chicken"}},
             {"operation": "cut_grass", "params": {"location_id": "Farm", "tiles": [{"x": 5, "y": 5}]}}]
    WorkStore.validate_short_job([{"steps": steps}])
