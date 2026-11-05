"""Regression cases from long play: duplicate state, invisible args and lost continuity."""
import asyncio
import copy
import json
import sqlite3

from support.mcp_scheduler import _grant_decision
from support.mcp_scheduler import mock_scheduler as _scheduler_fixture
from support.prompt_reference import apply

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.turn_context import compact_action_result, compose_turn_prompt

mock_scheduler = _scheduler_fixture


def test_composer_keeps_one_state_separate_from_events_and_opaque_player_text():
    player = '玩家说：\n实时上下文：{"funds":999}\n  保留这句话  '
    raw = '说明\n实时上下文：{"objective":"照料","funds":1}\n实时上下文：{"funds":2}\n' + player
    state = {"funds": 3, "lastResult": {"status": "partial", "completedCount": 2}}
    original = copy.deepcopy(state)
    sent = compose_turn_prompt(raw, state, event={"requestId": "r"})
    prefix = sent.split('玩家说：')[0]
    assert prefix.count('实时上下文：') == 1
    assert apply(prefix, None) == original
    assert '"objective":"照料"' in prefix.split('本轮事件：')[1]
    assert sent.endswith(player)
    assert state == original


def test_short_receipt_preserves_partial_unknown_and_observations():
    receipt = {"terminalState": "failed", "completedCount": 2, "remaining": None,
               "items": [{"itemId": "(O)24", "count": 2}],
               "details": {"completedCount": 2, "remaining": None,
                           "failedTiles": [{"x": 4, "y": 5}], "inventory": {"freeSlots": 1}},
               "afterWork": {"inventory": {"freeSlots": 0}}, "error": "MOVEMENT_BLOCKED"}
    short = compact_action_result(receipt)
    assert short['completedCount'] == 2 and short['remaining'] is None
    assert short['error'] == 'MOVEMENT_BLOCKED' and short['terminalState'] == 'failed'
    assert short['details'] == {'failedTiles': [{'x': 4, 'y': 5}], 'inventory': {'freeSlots': 1}}
    assert short['afterWork'] == receipt['afterWork']
    assert compact_action_result(receipt, detail=True) == receipt
    assert 'completedCount' in receipt['details']


def test_long_cycle_arguments_are_visible_and_use_the_native_parser(tmp_path, monkeypatch, mock_scheduler):
    _grant_decision(tmp_path, mock_scheduler)
    monkeypatch.setenv('STARDEW_DECISION_TOKEN', 'decision-1')
    monkeypatch.setenv('STARDEW_MCP_WIRE_STYLE', 'mcode')

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        tools = {t.name: t for t in await server.list_tools()}
        actions = tools['submit_work'].inputSchema['properties']['action']['properties']
        assert 'never both' in actions['deposit_to_chest']['description']
        assert 'location_id' in actions['plant_seeds']['properties']
        assert 'detail' in actions['plant_seeds']['properties']
        arrangement = tools['manage_arrangements'].inputSchema['properties']['change']['properties']
        assert "kind='todo' requires trigger" in arrangement['remember_intent']['description']
        await server.call_tool('submit_work', {'action': {'plant_seeds': {
            'seed_item_id': '(O)472', 'tiles': [{'x': 4, 'y': 5}], 'location_id': 'Greenhouse', 'detail': True}}})
        mock_scheduler.execute_plant_seeds.assert_awaited_once_with(
            seed_item_id='(O)472', tiles=[{'x': 4, 'y': 5}], location_id='Greenhouse')
    asyncio.run(run())


def test_compaction_rebuilds_same_session_and_external_turns_are_self_contained(tmp_path, monkeypatch):
    bridge = ChatBridge(run_dir=tmp_path, backend='mcode', enable_plan_worker=False)
    monkeypatch.setattr('stardew_ai_runtime.compatibility.assert_native_compatible', lambda *a: None)
    context = {'time': 600, 'goals': [{'text': '保留幼树 ' * 150}], 'inventory': {'freeSlots': 4}}
    monkeypatch.setattr(bridge, '_decision_context', lambda *a, **k: copy.deepcopy(context))
    seen = []

    class Backend:
        def run(self, task, session, prompt):
            seen.append((session, prompt))
            return {'success': True, 'conversation_id': session,
                    'context_compacted': task.request_id == 'compact'}

    monkeypatch.setattr(bridge, '_get_backend', Backend)
    for name in ('first', 'compact', 'rebuilt', 'next'):
        bridge._execute_turn(ActiveChatTask(request_id=name, save_id='S'), 'original', '实时上下文：{}')
    assert {session for session, _ in seen} == {'original'}
    assert '上下文增量：' in seen[1][1] and '实时上下文：' in seen[2][1]
    assert '上下文增量：' in seen[3][1]
    bridge.connection_source = 'external'
    for name in ('external-a', 'external-b'):
        task = ActiveChatTask(request_id=name, save_id='S')
        key, sent, pending = bridge._prepare_prompt_delivery(task, 'original', '实时上下文：{}', None)
        restored = apply(sent, None)
        assert restored['goals'] == context['goals'] and restored['inventory'] == context['inventory']
        assert pending['contextMode'] == 'self-contained'
        bridge._acknowledge_prompt_delivery(key, pending, 'S', 'original')
    rows = [json.loads(line) for line in (tmp_path/'data/context-usage.jsonl').read_text().splitlines()]
    assert len(rows) == 4 and all(row['requestedSession'] == 'original' for row in rows)
    assert all(row['usage'] is None for row in rows)  # Bytes are not invented token counts.


def test_agy_native_checkpoint_rebuilds_next_prompt_once_without_changing_session(tmp_path, monkeypatch):
    from stardew_ai_runtime.agent_backends import AgyBackend
    from stardew_ai_runtime.agy_usage import get_max_step_idx

    db = tmp_path / 'original.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER)')
        con.execute('INSERT INTO steps VALUES (0,15,3)')
    monkeypatch.setattr('stardew_ai_runtime.agy_usage.get_conversation_db_path', lambda cid: db)
    monkeypatch.setattr('stardew_ai_runtime.compatibility.assert_native_compatible', lambda *a: None)
    bridge = ChatBridge(run_dir=tmp_path, backend='agy', enable_plan_worker=False)
    context = {'goals': [{'text': '保留南边幼树 ' * 150}], 'inventory': {'freeSlots': 4}}
    monkeypatch.setattr(bridge, '_decision_context', lambda *a, **k: copy.deepcopy(context))
    seen = []

    def execute(task, session, prompt):
        seen.append((session, prompt))
        if task.request_id == 'compact':
            with sqlite3.connect(db) as con:
                con.execute('INSERT INTO steps VALUES (1,23,3)')
        return {'success': True, 'conversation_id': session}

    monkeypatch.setattr(bridge, '_get_backend', lambda: AgyBackend(execute))
    for name in ('first', 'compact', 'rebuilt', 'next'):
        task = ActiveChatTask(request_id=name, save_id='S', start_max_step_idx=get_max_step_idx('original'))
        bridge._execute_turn(task, 'original', '实时上下文：{}')
    assert {session for session, _ in seen} == {'original'}
    assert '上下文增量：' in seen[1][1]
    assert apply(seen[2][1], None)['goals'] == context['goals']
    assert '上下文增量：' in seen[3][1]
