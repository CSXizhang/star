from stardew_ai_runtime.work_state import WorkStore


def test_supplies_preparation_and_plant_water_all_keep_confirmed_counts():
    effects = [{"location": "Farm", "pathLength": 5}, {"state": "refilled"},
               {"state": "ate-food", "stack": 1}, {"state": "withdrawn", "stack": 20},
               *[{"state": "hoed"}] * 2, *[{"state": "planted"}] * 2,
               *[{"state": "watered"}] * 2,
               {"state": "skipped"}, {"state": "unknown"}, {"state": "failed"}]
    summary = WorkStore.effect_summary(effects)
    for actual in ("已记录 1 段行程", "水壶已补满", "已吃 1 份食物恢复体力",
                   "已取出 20 件", "已开垦 2 格", "已种下 2 格", "已浇水 2 格"):
        assert actual in summary
    assert "记录 3 项实际变化" not in summary
    assert summary.index("水壶已补满") < summary.index("已种下")


def test_animal_trip_reports_feed_pet_collection_and_door():
    summary = WorkStore.effect_summary([{"state": "fed", "stack": 12},
        {"state": "petted"}, {"state": "collected", "stack": 3}, {"state": "door-opened"}])
    for actual in ("已放入 12 份饲料", "已抚摸 1 只动物", "已收取 3 件", "动物门已打开"):
        assert actual in summary


def test_single_category_and_plant_water_wording_remains_compatible():
    assert WorkStore.effect_summary([{"state": "refilled"}]) == "水壶已补满"
    assert WorkStore.effect_summary([{"state": "planted"}] * 12 + [{"state": "watered"}] * 12) == "已种下 12 格，已浇水 12 格"
    assert WorkStore.effect_summary([{"state": "unknown"}, {"state": "skipped"}]) == "未确认实际变化"
