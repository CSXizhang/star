import pytest

from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def test_same_batch_preparation_planting_and_water_form_one_business():
    tiles = [{"x": x, "y": 5} for x in range(5, 8)]
    steps = [{"operation": "hoe_tiles", "params": {"location_id": "Farm", "tiles": tiles}},
             {"operation": "plant_seeds", "params": {"location_id": "Farm", "tiles": tiles}},
             {"operation": "water_zone", "params": {"center_x": 6, "center_y": 5, "radius": 1}}]
    WorkStore.validate_short_job([{"steps": steps}])
    with pytest.raises(WorkStateError, match="PLANTING_BATCH_REQUIRED"):
        WorkStore.validate_short_job([{"steps": [steps[1], steps[0], steps[2]]}])
    steps[0]["params"]["tiles"] = [{"x": 10, "y": 10}]
    with pytest.raises(WorkStateError, match="PLANTING_BATCH_REQUIRED"):
        WorkStore.validate_short_job([{"steps": steps}])


def test_scythe_preparation_must_stay_in_the_declared_seed_batch():
    tiles = [{"x": 5, "y": 5}, {"x": 6, "y": 5}]
    clear = {"operation": "cut_grass", "params": {"location_id": "Farm", "tiles": tiles}}
    hoe = {"operation": "hoe_tiles", "params": {"location_id": "Farm", "tiles": tiles}}
    plant = {"operation": "plant_seeds", "params": {"location_id": "Farm", "tiles": tiles}}
    water = {"operation": "water_zone", "params": {"center_x": 5, "center_y": 5, "radius": 1}}
    WorkStore.validate_short_job([{"steps": [clear, hoe, plant, water]}])
    for wrong in ([hoe, clear, plant, water], [plant, clear, water]):
        with pytest.raises(WorkStateError, match="PLANTING_BATCH_REQUIRED"):
            WorkStore.validate_short_job([{"steps": wrong}])
    clear["params"]["tiles"] = [{"x": 10, "y": 10}]
    with pytest.raises(WorkStateError, match="PLANTING_BATCH_REQUIRED"):
        WorkStore.validate_short_job([{"steps": [clear, plant, water]}])
