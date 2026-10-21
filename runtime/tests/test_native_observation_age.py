import asyncio
import json
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server


def test_cache_rewrite_keeps_native_capture_age_and_rejects_stale_input(tmp_path, monkeypatch):
    monkeypatch.delenv('STARDEW_LIFE_TURN_ID', raising=False)
    monkeypatch.delenv('STARDEW_LIFE_SAVE_ID', raising=False)
    bridge = ChatBridge(run_dir=tmp_path, backend="mcode", enable_plan_worker=False)
    bridge._latest_snapshot_payload = {"observedAt": 100.0, "world": {}, "companion": {}}
    bridge._write_life_snapshot("save")
    monkeypatch.setattr("time.time", lambda: 500.0)
    bridge._write_life_snapshot("save")
    cached = json.loads((tmp_path / 'data/life-snapshot.json').read_text(encoding='utf-8'))
    assert cached['capturedAt'] == 100.0
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface="life")
    with pytest.raises(ToolError, match='游戏近况已过期'):
        asyncio.run(server.call_tool("get_status", {}))
    scheduler.get_status.assert_not_called()
    assert json.loads((tmp_path / 'data/life-snapshot.json').read_text(encoding='utf-8')) == cached


@pytest.mark.parametrize('age_seconds,expired', [(120.0, False), (120.001, True)])
def test_life_snapshot_expiry_boundary_does_not_query_native_or_rewrite_cache(
    tmp_path, monkeypatch, age_seconds, expired,
):
    monkeypatch.delenv('STARDEW_LIFE_TURN_ID', raising=False)
    monkeypatch.delenv('STARDEW_LIFE_SAVE_ID', raising=False)
    bridge = ChatBridge(run_dir=tmp_path, backend='mcode', enable_plan_worker=False)
    bridge._latest_snapshot_payload = {'observedAt': 100.0, 'world': {}, 'companion': {}}
    bridge._write_life_snapshot('save')
    path = tmp_path / 'data/life-snapshot.json'
    before = path.read_bytes()
    monkeypatch.setattr('time.time', lambda: 100.0 + age_seconds)
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface='life')

    if expired:
        with pytest.raises(ToolError, match='游戏近况已过期'):
            asyncio.run(server.call_tool('get_status', {}))
    else:
        _, result = asyncio.run(server.call_tool('get_status', {}))
        assert result['capturedAt'] == 100.0
    scheduler.get_status.assert_not_called()
    assert path.read_bytes() == before


def test_owned_life_turn_rejects_expired_cache_when_no_observation_arrives(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, backend='mcode', enable_plan_worker=False)
    bridge._latest_snapshot_payload = {'observedAt': 100.0, 'world': {}, 'companion': {}}
    bridge._write_life_snapshot('save')
    bridge._life_turn_lease('turn', 'save')
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'turn')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', 'save')
    monkeypatch.setattr('time.time', lambda: 500.0)
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface='life')
    path = tmp_path / 'data/life-snapshot.json'
    before = path.read_bytes()

    async def exercise():
        pending = asyncio.create_task(server.call_tool('get_status', {}))
        await asyncio.sleep(0.02)
        assert not pending.done()
        with pytest.raises(ToolError, match='游戏近况已过期'):
            await asyncio.wait_for(pending, timeout=6)
        scheduler.get_status.assert_not_called()
        assert path.read_bytes() == before

    asyncio.run(exercise())


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


def test_owned_life_tool_waits_for_a_real_observation_without_renewing_old_cache(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, backend='mcode', enable_plan_worker=False)
    bridge._latest_snapshot_payload = {'observedAt': 100.0, 'world': {}, 'companion': {}}
    bridge._write_life_snapshot('save')
    bridge._life_turn_lease('turn', 'save')
    monkeypatch.setattr('time.time', lambda: 500.0)
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'turn')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', 'save')
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface='life')

    async def run():
        pending = asyncio.create_task(server.call_tool('get_status', {}))
        await asyncio.sleep(0.02)
        assert not pending.done()
        cached = json.loads((tmp_path / 'data/life-snapshot.json').read_text())
        assert cached['capturedAt'] == 100.0
        bridge._latest_snapshot_payload = {'observedAt': 499.0, 'world': {}, 'companion': {}}
        bridge._write_life_snapshot('save')
        _, result = await pending
        assert result['capturedAt'] == 499.0
        scheduler.get_status.assert_not_called()
    asyncio.run(run())


def test_observation_wait_stops_when_a_newer_player_turn_takes_ownership(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, backend='mcode', enable_plan_worker=False)
    bridge._latest_snapshot_payload = {'observedAt': 100.0, 'world': {}, 'companion': {}}
    bridge._write_life_snapshot('save')
    bridge._life_turn_lease('old-turn', 'save')
    monkeypatch.setattr('time.time', lambda: 500.0)
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'old-turn')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', 'save')
    scheduler = MagicMock()
    scheduler.run_dir = tmp_path
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface='life')

    async def run():
        pending = asyncio.create_task(server.call_tool('get_status', {}))
        await asyncio.sleep(0.02)
        bridge._life_turn_lease('new-turn', 'save')
        with pytest.raises(ToolError, match='STALE_CONVERSATION'):
            await pending
    asyncio.run(run())
