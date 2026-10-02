"""Independent acceptance of current work identity in the visible plan projection."""
from stardew_ai_runtime.chat_bridge import ChatBridge


def test_current_scope_is_visible_even_when_older_unfinished_goals_remain(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    old = bridge._work_store.add_goal("farm", "旧任务：取水壶", source="agent")
    current = bridge._work_store.add_goal("farm", "当前安排：种植并养鸡", source="user")
    bridge._autonomy.set_preferences("farm", goal="以前商量的方向")
    bridge._autonomy.set_mode("farm", "command", goal_scope=current.id)
    work = bridge._life_work_projection("farm")
    assert work["goal"] == current.text
    assert work["activeGoals"][0]["id"] == current.id
    assert any(g.id == old.id and g.status == "active" for g in bridge._work_store.state("farm").goals)


def test_cancelled_goal_is_never_presented_as_active_work(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", enable_plan_worker=False)
    cancelled = bridge._work_store.add_goal("farm", "已取消：继续浇水", source="user")
    bridge._work_store.cancel_goal("farm", cancelled.id)
    active = bridge._work_store.add_goal("farm", "当前安排：取种子", source="user")
    bridge._autonomy.set_mode("farm", "command", goal_scope=active.id)
    work = bridge._life_work_projection("farm")
    assert work["goal"] == active.text
    assert cancelled.id not in {g["id"] for g in work["activeGoals"]}
