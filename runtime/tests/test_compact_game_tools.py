import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from support.mcp_scheduler import _grant_decision
from support.mcp_scheduler import mock_scheduler as _scheduler_fixture

from stardew_ai_runtime.mcp_server import create_mcp_server

mock_scheduler = _scheduler_fixture


@pytest.mark.parametrize('instruction, owner', [(None, 'original-player'), ('浇水', 'another-player')])
def test_typed_actions_bind_goal_without_echoing_identifiers(tmp_path, monkeypatch, mock_scheduler, instruction, owner):
    store = _grant_decision(tmp_path, mock_scheduler)
    goal = store.add_goal('mock-save-123', '保留果树，只收拾石头')
    store.record_request('mock-save-123', 'original-player', '保留果树，只收拾石头')
    store.record_request('mock-save-123', 'another-player', '浇水')
    # Bind the decision to the exact persisted goal.
    store.begin_decision('mock-save-123', 'turn-2', goal_scope=goal.id,
                         request_id='original-player', request_ids=['another-player'])
    monkeypatch.setenv('STARDEW_DECISION_TOKEN', 'turn-2')
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        with pytest.raises(ToolError, match='exact text'):
            await server.call_tool('submit_work', {'instruction': 'delivery-wrong-id', 'steps': [{'water_auto': {}}]})
        assert not store.state('mock-save-123').tasks
        args = {'title': '清理石头', 'steps': [{'clear_debris': {'tiles': [{'x': 4, 'y': 5}]}}]}
        if instruction:
            args.update(title='浇水', instruction=instruction, steps=[{'water_tiles': {'tiles': [{'x': 4, 'y': 5}]}}])
        _, result = await server.call_tool('submit_work', args)
        task = store.state('mock-save-123').tasks[-1]
        assert task.goal_id == goal.id
        requests = {r['id']: r for r in store.state('mock-save-123').requests}
        assert requests[owner]['taskIds'] == [task.id]
        assert all(r['taskIds'] == [] for key, r in requests.items() if key != owner)
        assert task.steps[0].params['tiles'] == [{'x': 4, 'y': 5}]
        assert result['tasks'][0]['status'] == 'pending'
    asyncio.run(run())


@pytest.mark.parametrize('steps', [
    [{'water_auto': {}, 'harvest_auto': {}}],
    [{}],
    [{'water_auto': {'animal_name': 'cow'}}],
    [{'refill_watering_can': {'tiles': [{'x': ' ', 'y': 7}]}}],
    [{'invented_action': {}}],
])
def test_invalid_actions_never_select_a_job(tmp_path, mock_scheduler, steps):
    store = _grant_decision(tmp_path, mock_scheduler)
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        with pytest.raises(ToolError):
            await server.call_tool('submit_work', {'steps': steps})
        assert not store.state('mock-save-123').tasks
    asyncio.run(run())


def test_mcode_declares_fixed_typed_operations_without_generic_params(tmp_path, monkeypatch, mock_scheduler):
    monkeypatch.setenv('STARDEW_MCP_WIRE_STYLE', 'mcode')
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        tools = {t.name: t for t in await server.list_tools()}
        # Lock the public contract independently of the production registry.
        assert set(tools) == {
            'get_status', 'query_game', 'submit_work', 'manage_arrangements',
            'remember', 'request_player_decision', 'consult', 'read_reload_history',
        }
        args = tools['submit_work'].inputSchema['properties']
        assert 'goal_id' not in args
        assert 'request_id' not in args
        assert args['instruction']['type'] == 'string'
        actions = args['steps']['items']['properties']
        assert actions['refill_watering_can']['properties']['tiles']['type'] == 'array'
        assert actions['refill_watering_can']['properties']['tiles']['items']['properties']['x']['type'] == 'integer'
        assert actions['water_auto']['additionalProperties'] is False
        assert 'params' not in actions
        assert 'water-source tiles' in actions['refill_watering_can']['description']
        assert 'Does not refill' in actions['water_auto']['description']
        assert 'player' in actions['clear_debris']['description']
        memory = tools['remember'].inputSchema['properties']['entry']['properties']
        assert memory['record_decision_note']['properties']['category']['enum'] == ['observation', 'hypothesis', 'intent', 'failure']
        # Typed arguments plus a short semantic description for every operation.
        assert len(json.dumps([t.model_dump() for t in tools.values()])) < 56000
    asyncio.run(run())


def test_natural_answer_requires_matching_live_notice_and_original_player_text(tmp_path, monkeypatch, mock_scheduler):
    store = _grant_decision(tmp_path, mock_scheduler)
    goal = store.add_goal('mock-save-123', '建鸡舍')
    notice = store.request_player_decision('mock-save-123', goal.id, '建在哪里？', ['左侧', '右侧'])
    notice_id = notice['id']
    lease = {'requestId': 'answer', 'saveId': 'mock-save-123', 'noticeId': notice_id, 'playerText': '放右边吧'}
    (tmp_path/'data'/'life-turn.json').write_text(json.dumps(lease), encoding='utf-8')
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'answer')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', 'mock-save-123')
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        with pytest.raises(ToolError, match='DECISION_REPLY_NOT_LINKED'):
            await server.call_tool('remember', {'entry': {'answer_player_decision': {'notice_id': 'another-question'}}})
        _, result = await server.call_tool('remember', {'entry': {'answer_player_decision': {'notice_id': notice_id}}})
        assert result['answeredNoticeId'] == notice_id
        saved = next(n for n in store.state('mock-save-123').notices if n['id'] == notice_id)
        assert saved['answer'] == '放右边吧'
        assert saved['choices'] == ['左侧', '右侧']
    asyncio.run(run())
