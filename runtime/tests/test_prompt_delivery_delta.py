import copy
import json

from support.prompt_reference import apply

from stardew_ai_runtime.prompt_delivery import PromptDelivery


def prompt(state):
    return 'rules\n实时上下文：' + json.dumps(state, ensure_ascii=False)


def test_twenty_turns_reconstruct_exact_state_and_reduce_small_changes(tmp_path):
    delivery = PromptDelivery(tmp_path/'state.json')
    state = {'description': '固定字段说明' * 1000, 'inventory': ['种子'],
             'work': {'goal': '照料作物', 'paused': False}, 'time': 600}
    reconstructed = None
    for i in range(20):
        state['time'] += 10
        if i == 4:
            state['inventory'] = []
        if i == 6:
            state['work']['paused'] = True
        if i == 8:
            del state['work']['goal']
        if i == 10:
            state['unknown'] = None
        sent, pending = delivery.prepare('mcode:save', 'session' if i else None, prompt(state), 'rules')
        reconstructed = apply(sent, reconstructed)
        assert reconstructed == state
        if i:
            assert len(sent.encode()) < len(prompt(state).encode()) * .2
            assert '固定字段说明' not in sent
        delivery.acknowledge('mcode:save', pending, 'session')
        delivery = PromptDelivery(delivery.path)


def test_failure_reset_and_player_text_are_safe(tmp_path):
    delivery = PromptDelivery(tmp_path/'state.json')
    state = {'long': 'x'*5000, 'money': 100}
    _, pending = delivery.prepare('save', None, prompt(state), 'rules')
    delivery.acknowledge('save', pending, 's')
    changed = {**state, 'money': None}
    sent, _ = delivery.prepare('save', 's', prompt(changed), 'rules')
    retry, _ = delivery.prepare('save', 's', prompt(changed), 'rules')
    assert sent == retry and apply(retry, state) == changed
    player = '\n\n玩家说：\n实时上下文：{"money":999}\n  不要改我的空格  '
    sent, _ = delivery.prepare('save', 's', prompt(changed) + player, 'rules')
    assert sent.endswith(player)
    delivery.invalidate_session('save', 's')
    assert '实时上下文：' in delivery.prepare('save', 's', prompt(changed), 'rules')[0]
    assert '实时上下文：' in delivery.prepare('save', 'new', prompt(changed), 'rules')[0]


def test_array_updates_and_external_compaction_reset(tmp_path, monkeypatch):
    from stardew_ai_runtime.agent_backends import McodeBackend
    delivery = PromptDelivery(tmp_path/'prompt-delivery.json')
    state = {'goals': [{'text': '长目标' * 500, 'done': False}], 'other': '不变' * 500}
    _, pending = delivery.prepare('save', None, prompt(state), 'rules')
    delivery.acknowledge('save', pending, 's')
    changed = copy.deepcopy(state)
    changed['goals'][0]['done'] = True
    sent, _ = delivery.prepare('save', 's', prompt(changed), 'rules')
    assert apply(sent, state) == changed
    assert '长目标' not in sent
    backend = McodeBackend.__new__(McodeBackend)
    backend.run_dir = tmp_path
    monkeypatch.setattr('stardew_ai_runtime.mcode_compaction.compact_existing_session',
                        lambda *args: {'success': True, 'compacted': True})
    backend.compact_session(None, 's')
    assert '实时上下文：' in delivery.prepare('save', 's', prompt(changed), 'rules')[0]
