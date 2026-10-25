import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server


@pytest.mark.parametrize("age", [120.0, 120.001, 400.0])
def test_live_status_ignores_old_cache_without_renewing_its_age(tmp_path, monkeypatch, age):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._latest_snapshot_payload = {"observedAt": 100.0, "world": {}, "companion": {}}
    bridge._write_life_snapshot("save")
    monkeypatch.setattr("time.time", lambda: 100.0 + age)
    bridge._write_life_snapshot("save")
    path = tmp_path / "data/life-snapshot.json"
    before = path.read_bytes()
    scheduler = MagicMock(run_dir=tmp_path)
    scheduler.get_status = AsyncMock(return_value={"saveId": "save", "worldRevision": 42,
        "companion": {"stamina": 80}, "world": {}})
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="life")
    _, result = asyncio.run(server.call_tool("get_status", {}))
    assert result["worldRevision"] == 42 and result["companion"]["stamina"] == 80
    scheduler.get_status.assert_awaited_once()
    assert path.read_bytes() == before
    assert json.loads(before)["capturedAt"] == 100.0


def test_unchanged_game_revision_can_have_a_new_real_observation(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._latest_snapshot_revision = 7
    bridge._latest_snapshot_payload = {"observedAt": 100.0}
    bridge._write_life_snapshot("save")
    bridge._latest_snapshot_payload = {"observedAt": 499.0}
    bridge._write_life_snapshot("save")
    cached = json.loads((tmp_path / 'data/life-snapshot.json').read_text(encoding='utf-8'))
    assert cached['capturedAt'] == 499.0 and cached['worldRevision'] == 7


def test_legacy_observation_first_seen_time_is_not_renewed_by_dialogue(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._latest_snapshot_payload = {"world": {}}
    monkeypatch.setattr("time.time", lambda: 100.0)
    bridge._write_life_snapshot("save")
    monkeypatch.setattr("time.time", lambda: 500.0)
    bridge._write_life_snapshot("save")
    cached = json.loads((tmp_path / 'data/life-snapshot.json').read_text(encoding='utf-8'))
    assert cached['capturedAt'] == 100.0
