"""A canonical milestone must save before its prepared work becomes dispatchable."""

import json

import pytest

from stardew_ai_runtime.work_state import WorkStore


def _sync(store, *, text="old scope", before_write=None):
    return store.sync_milestone_work(
        "save", "node", text=text, constraints={"objectiveScope": {"summary": text}},
        todos=[{"key": "water", "intent": "water：" + text,
                "trigger": {"type": "calendar", "year": 1, "season": "spring", "day": 16},
                "expiry": None}], active=True, before_write=before_write,
    )


def _in_flight(store, goal_id):
    store.begin_decision("save", "chosen")
    store.submit_plan("save", goal_id=goal_id, decision_token="chosen", tasks=[{
        "id": "old", "title": "previous batch", "steps": [
            {"id": "running", "operation": "water_auto"},
            {"id": "later", "operation": "water_auto"},
        ],
    }])
    assert store.claim_next_step("save", "worker")
    store.assign_command_id("save", "old", "running", "native-in-flight")


def test_node_write_failure_keeps_work_disk_cache_and_in_flight_command(tmp_path):
    store = WorkStore(tmp_path / "work.json")
    goal_id, old_ids = _sync(store)
    _in_flight(store, goal_id)
    before = store.state_path.read_bytes()

    def fail_node_write(new_goal_id, todo_ids):
        assert new_goal_id == goal_id and todo_ids != old_ids
        assert store.state_path.read_bytes() == before
        raise OSError("node file refused")

    with pytest.raises(OSError, match="node file refused"):
        _sync(store, text="new planting scope", before_write=fail_node_write)

    assert store.state_path.read_bytes() == before
    cached = store._states["save"]
    assert cached.goals[0].text == "old scope"
    assert cached.tasks[0].status == "running"
    assert cached.tasks[0].steps[0].command_id == "native-in-flight"
    assert cached.tasks[0].steps[0].status == "running"
    assert cached.tasks[0].steps[1].status == "pending"
    assert {todo.id for todo in cached.todos} == set(old_ids.values())


def test_work_write_failure_after_node_save_keeps_old_work_and_canonical_links(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "work.json")
    goal_id, _ = _sync(store)
    _in_flight(store, goal_id)
    before = store.state_path.read_bytes()
    node_file = tmp_path / "node.json"

    def save_node(new_goal_id, todo_ids):
        assert store.state_path.read_bytes() == before
        node_file.write_text(json.dumps({"summary": "new planting scope", "goalId": new_goal_id,
                                        "todoIds": todo_ids}), encoding="utf-8")

    def fail_work_write():
        raise OSError("work file refused")

    monkeypatch.setattr(store, "_write_unlocked", fail_work_write)
    with pytest.raises(OSError, match="work file refused"):
        _sync(store, text="new planting scope", before_write=save_node)

    assert node_file.exists()
    assert json.loads(node_file.read_text(encoding="utf-8"))["goalId"] == goal_id
    assert store.state_path.read_bytes() == before
    cached = store._states["save"]
    assert cached.goals[0].text == "old scope"
    assert cached.tasks[0].steps[0].command_id == "native-in-flight"
    assert cached.tasks[0].steps[0].status == "running"
    assert cached.tasks[0].steps[1].status == "pending"


def test_unchanged_retry_still_persists_node_links_before_work_write(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "work.json")
    goal_id, todo_ids = _sync(store)
    order = []
    write = store._write_unlocked

    def node_write(current_goal_id, current_todo_ids):
        assert current_goal_id == goal_id and current_todo_ids == todo_ids
        order.append("node")

    def work_write():
        order.append("work")
        write()

    monkeypatch.setattr(store, "_write_unlocked", work_write)
    assert _sync(store, before_write=node_write) == (goal_id, todo_ids)
    assert order == ["node", "work"]
