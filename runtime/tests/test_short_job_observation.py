import pytest

from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def test_arrival_observation_can_accompany_one_purchase_business():
    steps = [{"operation": "navigate_to", "params": {"location_id": "AnimalShop"}},
             {"operation": "observe_building_services", "params": {"location_id": "AnimalShop"}},
             {"operation": "purchase_animal", "params": {"location_id": "Farm", "animal_type": "Chicken"}}]
    WorkStore.validate_short_job([{"steps": steps}])
    with pytest.raises(WorkStateError, match="MULTIPLE_BUSINESSES"):
        WorkStore.validate_short_job([{"steps": steps + [{"operation": "cut_grass", "params": {}}]}])
