"""Queued preparation preserves selected work and the literal player contract."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.work_state import WorkStateError, WorkStore


def select(store, goal_id):
    store.begin_decision('S', 'selected-turn')
    return store.submit_plan('S', decision_token='selected-turn', goal_id=goal_id, tasks=[{
        'id': 'harvest', 'title': 'Harvest', 'steps': [
            {'id': 'harvest', 'operation': 'harvest_auto', 'params': {}}]}])


def test_new_turn_cannot_erase_selected_pending_job(tmp_path):
    store = WorkStore(tmp_path / 'work.json')
    goal = store.add_goal('S', 'Harvest', source='user')
    select(store, goal.id)
    saved = store.state_path.read_bytes()
    with pytest.raises(WorkStateError, match='GAME_BUSY'):
        store.begin_decision('S', 'queued-preparation', goal_scope=goal.id)
    assert store.state_path.read_bytes() == saved
    assert store.claim_next_step('S', 'worker')['taskId'] == 'harvest'


def test_preparation_rechecks_selected_job_after_waiting_for_model_slot(tmp_path):
    async def run():
        bridge = ChatBridge(run_dir=tmp_path, backend='mcode', internal_plan_client=MagicMock())
        bridge._latest_snapshot_payload = {'world': {'year': 1, 'season': 'spring', 'dayOfMonth': 2}}
        node = bridge._milestone_store.propose('S', title='Harvest', target_date='1:spring:2',
                                              preparation=['harvest'])
        node = bridge._milestone_store.adopt('S', node['id'], work_store=bridge._work_store)
        bridge.handle_chat_submit = AsyncMock()
        await bridge._busy_lock.acquire()
        waiting = asyncio.create_task(bridge._start_accepted_preparation(None, 'S', {}))
        await asyncio.sleep(0)
        select(bridge._work_store, node['goalId'])
        bridge._busy_lock.release()
        await waiting
        bridge.handle_chat_submit.assert_not_awaited()
        assert bridge._work_store.state('S').decision['token'] == 'selected-turn'
        assert bridge._work_store.claim_next_step('S', 'worker')['taskId'] == 'harvest'
    asyncio.run(run())


def test_accepted_goal_uses_original_request_across_autonomy_and_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr('stardew_ai_runtime.compatibility.assert_native_compatible', lambda _: None)
    bridge = ChatBridge(run_dir=tmp_path, backend='mcode', internal_plan_client=MagicMock())
    instruction = '只整理这个箱子。逐步汇报，本轮与保留记录累计分别说明，结束报告水量体力。'
    bridge._latest_snapshot_payload = {'world': {'year': 1, 'season': 'spring', 'dayOfMonth': 2}}

    def adopt(*args):
        node = bridge._milestone_store.propose('S', title='Store',
                                              summary='Short paraphrase omitted reporting', preparation=['store'],
                                              target_date='1:spring:2')
        bridge._milestone_store.adopt('S', node['id'], work_store=bridge._work_store)
        return {'success': True, 'response': 'Arranged', 'conversation_id': 'life-session'}

    monkeypatch.setattr(bridge, '_execute_life_turn', adopt)
    monkeypatch.setattr(bridge, '_start_accepted_preparation', AsyncMock())
    asyncio.run(bridge._run_life_chat_turn(None, {'request_id': 'player-original', 'save_id': 'S',
                                                'mode': 'plan', 'text': instruction}))
    goal = bridge._work_store.state('S').goals[0]
    assert goal.constraints['playerRequestId'] == 'player-original'
    # Reload the durable request/goal association, as after a bridge restart.
    bridge._work_store = WorkStore(bridge._work_store.state_path)
    seen = []

    def choose(task, session, prompt):
        seen.append(prompt)
        bridge._work_store.submit_plan('S', decision_token=task.decision_token,
                                      goal_id=goal.id, tasks=[{
            'id': 'store', 'title': 'Store', 'steps': [
                {'id': 'store', 'operation': 'deposit_to_chest', 'params': {'chest_x': 1, 'chest_y': 2}}]}])
        return {'success': True}

    backend = MagicMock()
    backend.run.side_effect = choose
    monkeypatch.setattr(bridge, '_get_backend', lambda: backend)
    task = ActiveChatTask('autonomy-internal', 'S', goal_scope=goal.id)
    bridge._execute_turn(task, None, 'Saved goal summary')
    assert instruction in seen[0]
    assert bridge._work_store.find_request_for_task('S', 'store')['id'] == 'player-original'
    claim = bridge._work_store.claim_next_step('S', 'worker')
    bridge._work_store.assign_command_id('S', claim['taskId'], claim['stepId'], 'native-store')
    bridge._work_store.commit_step_result('S', task_id=claim['taskId'], step_id=claim['stepId'],
                                         command_id='native-store', outcome='completed',
                                         effects=[{'state': 'deposited', 'stack': 3}])
    event = bridge._work_store.pending_outbox('S')[0]
    prompt = bridge._format_delivery_prompt([event], 'S', midflight=[], execution_deferred=True)
    assert instruction in prompt
    assert event['requestId'] == 'player-original'
