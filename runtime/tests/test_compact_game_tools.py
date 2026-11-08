import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from support.mcp_scheduler import _grant_decision
from support.mcp_scheduler import mock_scheduler as _scheduler_fixture

from stardew_ai_runtime.mcp_server import create_mcp_server

mock_scheduler = _scheduler_fixture


@pytest.mark.parametrize('ingress', ['chat', 'tool-boundary'])
def test_created_project_preserves_player_contract_across_revision_and_next_turn(tmp_path, monkeypatch, mock_scheduler, ingress):
    from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
    from stardew_ai_runtime.turn_messages import exchange
    from stardew_ai_runtime.work_state import WorkStore

    store = _grant_decision(tmp_path, mock_scheduler)
    sid = 'mock-save-123'
    original = '建设内容丰富的田园生活空间，庭院、耕作和养殖应自然联系，保留西边树林。'
    store.record_request(sid, 'original', original)
    store.record_request(sid, 'ambient', '结合当前目标安排下一步', origin='autonomy')
    store.record_request(sid, 'unrelated', '昨天的闲聊')
    store.begin_decision(sid, 'decision-1', request_id='original' if ingress == 'chat' else 'ambient')
    monkeypatch.setenv('STARDEW_DECISION_TOKEN', 'decision-1')
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'turn')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', sid)
    (tmp_path / 'data/life-turn.json').write_text(json.dumps({'requestId': 'turn', 'saveId': sid}))

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        if ingress == 'tool-boundary':
            exchange(tmp_path / 'data/turn-messages.json', 'turn', sid,
                     publish=[{'requestId': 'original', 'text': original}])
            _, delivery = await server.call_tool('manage_arrangements', {'change': {'manage_goal': {'action': 'list'}}})
            assert delivery['playerMessages'][0]['text'] == original
        _, created = await server.call_tool('manage_arrangements', {'change': {'manage_goal': {
            'action': 'create', 'text': '小庭院方案', 'constraints': {'playerRequestId': 'unrelated'}}}})
        goal = created['goal']
        assert goal['constraints']['playerRequestId'] == 'original'
        await server.call_tool('manage_arrangements', {'change': {'manage_goal': {
            'action': 'revise', 'goal_id': goal['id'], 'text': '修改自选方案',
            'constraints': {'preserve': 'west trees', 'playerRequestId': 'unrelated'},
            'project': {'summary': '模型修改了布局', 'remaining': ['继续施工']}}}})
        return goal['id']

    goal_id = asyncio.run(run())
    monkeypatch.setattr('stardew_ai_runtime.compatibility.assert_native_compatible', lambda _: None)
    bridge = ChatBridge(run_dir=tmp_path, backend='mcode', internal_plan_client=MagicMock())
    bridge._work_store = WorkStore(store.state_path)
    seen = []
    backend = MagicMock()
    backend.run.side_effect = lambda task, session, prompt: seen.append(prompt) or {'success': True}
    monkeypatch.setattr(bridge, '_get_backend', lambda: backend)
    bridge._execute_turn(ActiveChatTask('autonomy-next', sid, goal_scope=goal_id), None, '模型修改后的简短方案')
    assert original in seen[0]
    assert bridge._work_store.state(sid).decision['requestId'] == 'original'


def test_new_project_does_not_claim_unread_or_ambiguous_historical_player_requests(tmp_path, monkeypatch, mock_scheduler):
    from stardew_ai_runtime.turn_messages import exchange
    store = _grant_decision(tmp_path, mock_scheduler)
    sid = 'mock-save-123'
    for rid in ('old', 'first', 'second'):
        store.record_request(sid, rid, rid)
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'turn')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', sid)
    (tmp_path / 'data/life-turn.json').write_text(json.dumps({'requestId': 'turn', 'saveId': sid}))
    exchange(tmp_path / 'data/turn-messages.json', 'turn', sid,
             publish=[{'requestId': rid, 'text': rid} for rid in ('first', 'second')])
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        for title in ('before delivery', 'after ambiguous delivery'):
            _, created = await server.call_tool('manage_arrangements', {'change': {'manage_goal': {
                'action': 'create', 'text': title, 'constraints': {'playerRequestId': 'old'}}}})
            assert 'playerRequestId' not in created['goal']['constraints']
    asyncio.run(run())


@pytest.mark.parametrize('ingress', ['chat', 'tool-boundary'])
def test_project_feedback_survives_plan_rewrite_trimming_and_reload(tmp_path, monkeypatch, mock_scheduler, ingress):
    from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
    from stardew_ai_runtime.turn_messages import exchange
    from stardew_ai_runtime.work_state import WorkStore

    store = _grant_decision(tmp_path, mock_scheduler)
    sid = 'mock-save-123'
    original = '完成田园，保留树林。'
    feedback = '保留当前布局，让主体作物长成，处理池边遮挡，再查看真实全景。'
    for rid, text in [('original', original), ('feedback', feedback), ('unread', '尚未送达的新话题')]:
        store.record_request(sid, rid, text)
    goal = store.add_goal(sid, '当前自选方案')
    store.bind_goal_request(sid, goal.id, 'original')
    other = store.add_goal(sid, '另一项工程')
    store.record_request(sid, 'other-message', '另一项工程的要求')
    store.bind_goal_feedback(sid, other.id, {'other-message'})
    store.begin_decision(sid, 'decision-1', goal_scope=goal.id,
                         request_id='feedback' if ingress == 'chat' else 'original')
    monkeypatch.setenv('STARDEW_DECISION_TOKEN', 'decision-1')
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'turn')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', sid)
    (tmp_path / 'data/life-turn.json').write_text(json.dumps({'requestId': 'turn', 'saveId': sid}))

    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        if ingress == 'tool-boundary':
            exchange(tmp_path / 'data/turn-messages.json', 'turn', sid,
                     publish=[{'requestId': 'feedback', 'text': feedback}])
        _, resumed = await server.call_tool('manage_arrangements', {'change': {'manage_goal': {
            'action': 'resume', 'goal_id': goal.id}}})
        if ingress == 'tool-boundary':
            assert resumed['playerMessages'][0]['text'] == feedback
            assert 'updates' not in resumed['playerRequest']  # Offered after the operation, not before.
        _, revised = await server.call_tool('manage_arrangements', {'change': {'manage_goal': {
            'action': 'revise', 'goal_id': goal.id,
            'project': {'summary': '只摆几件物品', 'remaining': []}}}})
        expected = {'requestId': 'original', 'text': original,
                    'updates': [{'requestId': 'feedback', 'text': feedback}]}
        assert revised['playerRequest'] == expected
        # Revising another goal within this scoped turn cannot move the feedback.
        await server.call_tool('manage_arrangements', {'change': {'manage_goal': {
            'action': 'revise', 'goal_id': other.id, 'project': {'summary': '其他安排'}}}})
        _, listed = await server.call_tool('manage_arrangements', {'change': {'manage_goal': {'action': 'list'}}})
        assert next(r for r in listed['playerRequests'] if r['goalId'] == goal.id) == {'goalId': goal.id, **expected}
    asyncio.run(run())
    store.revise_goal(sid, goal.id, status='paused')
    store.record_request(sid, 'old-autonomy', '已结束的自动工作', origin='autonomy')
    store.bind_request_task(sid, 'old-autonomy', 'finished-task', goal.id)
    for number in range(55):
        store.record_request(sid, f'noise-{number}', '其他已处理消息')
    reloaded = WorkStore(store.state_path)
    assert reloaded.goal_player_request(sid, goal.id)['updates'][0]['text'] == feedback
    assert not any(r['id'] == 'old-autonomy' for r in reloaded.state(sid).requests)
    assert next(r for r in reloaded.state(sid).requests if r['id'] == 'other-message')['goalId'] == other.id
    reloaded.revise_goal(sid, goal.id, status='active')
    monkeypatch.setattr('stardew_ai_runtime.compatibility.assert_native_compatible', lambda _: None)
    bridge = ChatBridge(run_dir=tmp_path, backend='mcode', internal_plan_client=MagicMock())
    bridge._work_store = reloaded
    seen = []
    backend = MagicMock()
    backend.run.side_effect = lambda task, session, prompt: seen.append(prompt) or {'success': True}
    monkeypatch.setattr(bridge, '_get_backend', lambda: backend)
    bridge._execute_turn(ActiveChatTask('autonomy-next', sid, goal_scope=goal.id), None, '继续当前方案')
    assert original in seen[0] and feedback in seen[0]
    assert '尚未送达的新话题' not in seen[0]


def test_partial_construction_frontloads_real_remaining_obstacles_without_retry(tmp_path, mock_scheduler):
    store = _grant_decision(tmp_path, mock_scheduler)
    mock_scheduler.latest_snapshot = {"payload": {"companion": {"locationId": "Farm"}}}
    partial = {"status": "executed", "terminalState": "partially-succeeded", "commandId": "native-partial",
        "completedCount": 1, "skippedCount": 1, "failedCount": 0,
        "effects": [{"state": "placed", "tile": {"x": 5, "y": 7}, "itemId": "(O)328"},
                    {"state": "skipped", "tile": {"x": 6, "y": 7}, "reason": "occupied"}]}
    mock_scheduler.place_items = AsyncMock(return_value=partial)
    tree = {"kind": "Tree", "x": 6, "y": 7, "width": 1, "height": 1, "growthStage": 5}
    mock_scheduler.query_farm_space = AsyncMock(return_value={"capturedRevision": 2, "occupants": [tree], "occupantsTruncated": False})
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        _, result = await server.call_tool("submit_work", {"action": {"place_items": {
            "item_id": "(O)328", "tiles": [{"x": 5, "y": 7}, {"x": 6, "y": 7}]}}})
        assert next(iter(result)) == "remainingWork"
        assert result["remainingWork"]["unresolvedTargets"] == [{"x": 6, "y": 7, "reason": "occupied"}]
        assert result["remainingWork"]["observation"]["occupants"] == [tree]
        assert result["commandId"] == "native-partial" and result["completedCount"] == 1
        assert result["effects"] == partial["effects"]
        mock_scheduler.place_items.assert_awaited_once()
        mock_scheduler.query_farm_space.assert_awaited_once_with(location_id="Farm", region={"x": 5, "y": 6, "width": 3, "height": 3})
        assert not store.state("mock-save-123").tasks
    asyncio.run(run())


def test_completion_keeps_submitted_as_built_layout_without_unlocking_main(tmp_path, mock_scheduler):
    store = _grant_decision(tmp_path, mock_scheduler)
    goal = store.add_goal("mock-save-123", "Build the farm", source="agent")
    store.revise_goal("mock-save-123", goal.id, project={"layout":"initial proposal", "preserve":"west trees",
        "remaining":["Connect the woodland route"]})
    main = store.add_goal("mock-save-123", "Long-term farm", source="agent", kind="main")
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        final = {"summary":"Built and observed", "layout":"Revised lakeside terrace and actual access"}
        with pytest.raises(ToolError, match="PROJECT_UNRESOLVED"):
            await server.call_tool("manage_arrangements", {"change":{"manage_goal": {
                "action":"complete", "goal_id":goal.id, "project":final}}})
        unchanged = store.state("mock-save-123").goals[0]
        assert unchanged.status == "active" and unchanged.project["layout"] == "initial proposal"
        assert unchanged.project["remaining"] == ["Connect the woodland route"]
        final["remaining"] = []
        _, result = await server.call_tool("manage_arrangements", {"change":{"manage_goal": {
            "action":"complete", "goal_id":goal.id, "project":final}}})
        assert result["goal"]["status"] == "completed"
        persisted = store.state("mock-save-123").goals[0].project
        assert persisted["layout"] == final["layout"] and persisted["summary"] == final["summary"]
        assert persisted["preserve"] == "west trees" and persisted["completionObservation"]["worldRevision"] == 1
        _, blocked = await server.call_tool("manage_arrangements", {"change":{"manage_goal": {
            "action":"complete", "goal_id":main.id, "project":final}}})
        assert blocked["reasonCode"] == "MAIN_GOAL_LOCKED"
        protected = next(g for g in store.state("mock-save-123").goals if g.id == main.id)
        assert protected.status == "active" and protected.project == {}
    asyncio.run(run())


def test_wait_uses_existing_action_entry_and_validates_duration(tmp_path, mock_scheduler):
    _grant_decision(tmp_path, mock_scheduler)
    mock_scheduler.execute_wait = AsyncMock(return_value={"status": "completed", "terminalState": "succeeded",
        "details": {"elapsedGameMinutes": 40, "startTime": 820, "endTime": 900}})
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface="light")
        assert len(await server.list_tools()) == 8
        _, result = await server.call_tool("submit_work", {"action": {"wait": {"game_minutes": 40}}})
        assert result["terminalState"] == "succeeded"
        assert result["details"]["elapsedGameMinutes"] == 40
        mock_scheduler.execute_wait.assert_awaited_once_with(40)
        with pytest.raises(ToolError):
            await server.call_tool("submit_work", {"action": {"wait": {"game_minutes": True}}})
        assert mock_scheduler.execute_wait.await_count == 1
    asyncio.run(run())


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
        await server.call_tool('submit_work', args)
        assert method.await_count == 3
        store.set_paused('mock-save-123', True)
        with pytest.raises(ToolError, match='WORK_STOPPED'):
            await server.call_tool('submit_work', args)
        assert method.await_count == 3
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
        assert queries['observe_map_image']['properties']['region']['properties']['width']['type'] == 'integer'
        references = tools['consult'].inputSchema['properties']['request']['properties']
        assert references['read_reference_image']['properties']['image_id']['type'] == 'string'
        # Local image regions and the selected-reference operation add typed
        # fields; retain a small envelope instead of including legacy schemas.
        assert len(json.dumps([t.model_dump() for t in tools.values()])) < 68000
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


def test_player_question_without_goal_is_durable_and_does_not_change_control(tmp_path, mock_scheduler):
    store = _grant_decision(tmp_path, mock_scheduler)
    store.set_paused('mock-save-123', True)
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='light')
        _, notice = await server.call_tool('request_player_decision', {
            'message': '这块地方留作什么？', 'choices': ['留空', '种花']})
        _, repeated = await server.call_tool('request_player_decision', {
            'message': '这块地方留作什么？', 'choices': ['留空', '种花']})
        assert repeated['id'] == notice['id']
        assert store.pending_player_notice('mock-save-123')['id'] == notice['id']
        assert not store.state('mock-save-123').goals
        assert store.state('mock-save-123').paused
        store.answer_player_notice('mock-save-123', notice['id'], '留空', 'player-answer')
        assert store.unresolved_player_notices('mock-save-123') == []
        with pytest.raises(ToolError, match='goal'):
            await server.call_tool('request_player_decision', {'message': '另一件事', 'goal_id': 'missing'})
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

    mock_scheduler.execute_tiles.side_effect = ToolError('GAME_BUSY: native actor already executing')
    async def run():
        server = create_mcp_server(run_dir=tmp_path, scheduler=mock_scheduler, surface='life')
        args = {'action': {'water_tiles': {'tiles': [{'x': 4, 'y': 5}]}}}
        with pytest.raises(ToolError, match='GAME_BUSY'):
            await server.call_tool('submit_work', args)
        assert store.state('mock-save-123').decision['token'] == 'decision-1'
        mock_scheduler.execute_tiles.assert_awaited_once()
        store.revoke_decision('mock-save-123')
        store.set_paused('mock-save-123', True)
        with pytest.raises(ToolError, match='WORK_STOPPED'):
            await server.call_tool('submit_work', args)
        mock_scheduler.execute_tiles.assert_awaited_once()
        store.set_paused('mock-save-123', False)
        mock_scheduler.execute_tiles.side_effect = None
        _, result = await server.call_tool('submit_work', args)
        assert result['terminalState'] == 'succeeded'
        assert mock_scheduler.execute_tiles.await_count == 2
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
