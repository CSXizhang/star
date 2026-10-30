import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from support.mcp_scheduler import _grant_decision
from support.mcp_scheduler import mock_scheduler as _scheduler_fixture

from stardew_ai_runtime.mcp_server import create_mcp_server

mock_scheduler = _scheduler_fixture


@pytest.mark.parametrize('operation, method_name', [('clear_debris', 'clear_debris'), ('water_tiles', 'execute_tiles')])
def test_typed_action_executes_now_and_rechecks_authority(tmp_path, monkeypatch, mock_scheduler, operation, method_name):
    store = _grant_decision(tmp_path, mock_scheduler)
    monkeypatch.setenv('STARDEW_DECISION_TOKEN', 'decision-1')
    method = AsyncMock(return_value={"status": "completed", "terminalState": "succeeded", "completedCount": 1,
                     "effects": [{"tile": {"x": 4, "y": 5}, "state": "watered"}]})
    setattr(mock_scheduler, method_name, method)
    mock_scheduler.query_production = AsyncMock(return_value={"groundItems": [{"itemId": "(O)388", "tileX": 4, "tileY": 5, "isDropped": True}]})
    mock_scheduler.query_inventory = AsyncMock(return_value={"freeSlots": 2})
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='life' if operation == 'clear_debris' else 'light')
        args = {'action': {operation: {'tiles': [{'x': 4, 'y': 5}]}}}
        _, result = await server.call_tool('submit_work', args)
        method.assert_awaited_once()
        if operation == 'clear_debris':
            assert result['afterWork']['ground']['groundItems'][0]['itemId'] == '(O)388'
            assert result['afterWork']['inventory']['freeSlots'] == 2
        assert result['completedCount'] == 1 and result['terminalState'] == 'succeeded'
        await server.call_tool('submit_work', args)
        assert method.await_count == 2
        assert not store.state('mock-save-123').tasks
        goal = store.add_goal('mock-save-123', '当前观察范围已处理')
        _, completion = await server.call_tool('manage_arrangements', {
            'change': {'manage_goal': {'action': 'complete', 'goal_id': goal.id}}})
        assert completion['goal']['status'] == 'completed'
        assert completion['goal']['project']['completionObservation']['worldRevision'] == 1
        store.revoke_decision('mock-save-123')
        with pytest.raises(ToolError, match='STALE_DECISION'):
            await server.call_tool('submit_work', args)
        assert method.await_count == 2
    asyncio.run(run())


@pytest.mark.parametrize('action', [
    {'water_auto': {}, 'harvest_auto': {}}, {},
    {'water_auto': {'animal_name': 'cow'}},
    {'refill_watering_can': {'tiles': [{'x': ' ', 'y': 7}]}},
    {'invented_action': {}},
])
def test_invalid_actions_never_execute(tmp_path, mock_scheduler, action):
    store = _grant_decision(tmp_path, mock_scheduler)
    mock_scheduler.water_auto = AsyncMock()
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        with pytest.raises(ToolError):
            await server.call_tool('submit_work', {'action': action})
        assert not store.state('mock-save-123').tasks
        mock_scheduler.water_auto.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('action, hint', [
    ({}, 'empty object does not select'),
    ({'items': [{'itemId': '(O)24', 'count': 1}]}, 'action.ship_items.items'),
    ({'ship_items': {'items': [{'itemId': '(O)24', 'count': 'not-a-number'}]}}, 'ship_items.items.0.count'),
])
def test_malformed_shipping_locates_the_error_without_executing(tmp_path, mock_scheduler, action, hint):
    _grant_decision(tmp_path, mock_scheduler)
    mock_scheduler.execute_ship_items = AsyncMock()
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        with pytest.raises(ToolError) as error:
            await server.call_tool('submit_work', {'action': action})
        assert hint in str(error.value)
        mock_scheduler.execute_ship_items.assert_not_awaited()
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
        assert set(args) == {'action'}
        actions = args['action']['properties']
        assert actions['refill_watering_can']['properties']['tiles']['type'] == 'array'
        assert actions['refill_watering_can']['properties']['tiles']['items']['properties']['x']['type'] == 'integer'
        assert actions['water_auto']['additionalProperties'] is False
        assert 'params' not in actions
        assert actions['rest']['required'] == ['target_stamina']
        assert 'water-source tiles' in actions['refill_watering_can']['description']
        assert 'Does not refill' in actions['water_auto']['description']
        assert 'player' in actions['clear_debris']['description']
        assert 'collectedItems' in actions['pickup_items']['description']
        queries = tools['query_game'].inputSchema['properties']['query']['properties']
        assert 'groundObjects' in queries['observe_farming_helpers']['description']
        memory = tools['remember'].inputSchema['properties']['entry']['properties']
        assert memory['record_decision_note']['properties']['category']['enum'] == ['observation', 'hypothesis', 'intent', 'failure']
        # Typed arguments plus a short semantic description for every operation.
        # Useful examples and memory operations may grow modestly; catch an
        # accidental full legacy-schema expansion rather than shave guidance.
        assert len(json.dumps([t.model_dump() for t in tools.values()])) < 65000
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


def test_upgrade_discards_old_queue_without_cancelling_player_goal(tmp_path, mock_scheduler):
    store = _grant_decision(tmp_path, mock_scheduler)
    goal = store.add_goal('mock-save-123', '照顾农场', source='user')
    store.submit_plan('mock-save-123', goal_id=goal.id, tasks=[{'title': '旧安排',
        'steps': [{'operation': 'water_auto', 'params': {}}]}], decision_token='decision-1')
    store.retire_legacy_execution()
    state = store.state('mock-save-123')
    assert not state.tasks and not state.decision and not state.last_job
    assert next(g for g in state.goals if g.id == goal.id).status == 'active'
    assert state.archive


def test_occupied_conversation_can_execute_after_owner_settles(tmp_path, monkeypatch, mock_scheduler):
    store = _grant_decision(tmp_path, mock_scheduler)
    monkeypatch.delenv('STARDEW_DECISION_TOKEN', raising=False)
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'live-chat')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', 'mock-save-123')
    (tmp_path / 'data' / 'life-turn.json').write_text(json.dumps({
        'requestId': 'live-chat', 'saveId': 'mock-save-123'}), encoding='utf-8')
    mock_scheduler.execute_tiles = AsyncMock(return_value={
        'status': 'completed', 'terminalState': 'succeeded', 'completedCount': 1})

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='life')
        args = {'action': {'water_tiles': {'tiles': [{'x': 4, 'y': 5}]}}}
        with pytest.raises(ToolError, match='GAME_BUSY'):
            await server.call_tool('submit_work', args)
        assert store.state('mock-save-123').decision['token'] == 'decision-1'
        mock_scheduler.execute_tiles.assert_not_awaited()
        store.revoke_decision('mock-save-123')
        store.set_paused('mock-save-123', True)
        with pytest.raises(ToolError, match='WORK_STOPPED'):
            await server.call_tool('submit_work', args)
        mock_scheduler.execute_tiles.assert_not_awaited()
        store.set_paused('mock-save-123', False)
        _, result = await server.call_tool('submit_work', args)
        assert result['terminalState'] == 'succeeded'
        mock_scheduler.execute_tiles.assert_awaited_once()
    asyncio.run(run())


def test_player_update_arrives_after_current_action_and_before_next_dispatch(tmp_path, monkeypatch, mock_scheduler):
    from stardew_ai_runtime.turn_messages import exchange
    _grant_decision(tmp_path, mock_scheduler)
    monkeypatch.setenv('STARDEW_DECISION_TOKEN', 'decision-1')
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'turn')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', 'mock-save-123')
    (tmp_path / 'data/life-turn.json').write_text(json.dumps({'requestId':'turn','saveId':'mock-save-123'}))
    inbox = tmp_path / 'data/turn-messages.json'
    async def execute(**kwargs):
        exchange(inbox, 'turn', 'mock-save-123', publish=[{'requestId':'new','text':'先规划鸡舍'}])
        return {'status':'completed','terminalState':'succeeded','completedCount':1}
    mock_scheduler.execute_tiles = AsyncMock(side_effect=execute)
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        args = {'action':{'water_tiles':{'tiles':[{'x':4,'y':5}]}}}
        _, result = await server.call_tool('submit_work', args)
        assert result['terminalState'] == 'succeeded'
        assert result['playerMessages'][0]['text'] == '先规划鸡舍'
        exchange(inbox, 'turn', 'mock-save-123', publish=[{'requestId':'newer','text':'先别卖东西'}])
        _, result = await server.call_tool('submit_work', args)
        assert result['actionExecuted'] is False
        assert result['playerMessages'][0]['requestId'] == 'newer'
        mock_scheduler.execute_tiles.assert_awaited_once()
    asyncio.run(run())


def test_explicit_speech_is_delivered_through_live_game_chat(tmp_path, monkeypatch, mock_scheduler):
    from stardew_ai_runtime.chat_bridge import ChatBridge
    _grant_decision(tmp_path, mock_scheduler)
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'speech')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', 'mock-save-123')
    (tmp_path/'data/life-turn.json').write_text(json.dumps({'requestId':'speech','saveId':'mock-save-123'}))
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        await server.call_tool('remember', {'entry':{'say_to_player':{'text':'鸡舍放在田边怎么样？'}}})
        bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
        ws = AsyncMock()
        await bridge._apply_life_intent(ws, 'speech', 'mock-save-123')
        replies = [json.loads(c.args[0]) for c in ws.send_text.await_args_list]
        assert any(r.get('payload',{}).get('status') == 'public-message' and r['payload']['replyText'] == '鸡舍放在田边怎么样？' for r in replies)
        assert not list((tmp_path/'data/life-controls').glob('*.json'))
    asyncio.run(run())
