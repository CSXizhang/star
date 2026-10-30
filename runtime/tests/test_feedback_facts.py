"""Regression for contradictory crop, cancellation and cumulative feedback."""
import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from stardew_ai_runtime.decision_context import build_decision_context, crop_water_facts
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.scheduler import CompanionScheduler
from stardew_ai_runtime.work_state import ExecutionEntry, Step, Task, WorkStore


def farm():
    return {"locationId": "Farm", "observationStatus": "observed", "deadCropCount": 0,
            "cropUnwateredCount": 2, "cropUnwateredTiles": [{"x": 9, "y": 7}, {"x": 8, "y": 4}],
            "cropUnwateredTruncated": False, "matureCropCount": 1,
            "matureCrops": [{"x": 8, "y": 4}], "matureCropsTruncated": False}


def test_dry_crop_outside_complete_harvestable_set_is_never_empty_soil():
    result = crop_water_facts(farm())
    assert result["complete"]
    assert result["unwateredNonHarvestableCount"] == 1
    assert result["unwateredNonHarvestableTiles"] == [{"x": 9, "y": 7}]
    assert "never empty soil" in result["meaning"]
    watered = farm()
    watered.update(cropUnwateredCount=1, cropUnwateredTiles=[{"x": 8, "y": 4}])
    result = crop_water_facts(watered)
    assert result["unwateredNonHarvestableCount"] == 0
    assert "Neither list inventories all immature crops" in result["meaning"]


def test_unwatered_soil_total_includes_occupied_crops_even_when_tile_lists_are_truncated():
    result = crop_water_facts({**farm(), 'tilledUnwateredCount': 11, 'cropUnwateredCount': 4,
                              'cropUnwateredTruncated': True})
    assert result['unwateredEmptySoilCount'] == 7
    assert not result['complete']
    assert 'BOTH occupied crop soil and empty soil' in result['meaning']
    assert crop_water_facts(farm())['unwateredEmptySoilCount'] == 'unknown'
    assert crop_water_facts({**farm(), 'tilledUnwateredCount': 1})['unwateredEmptySoilCount'] == 'unknown'


@pytest.mark.parametrize("patch", [{"matureCropsTruncated": True},
    {"matureCropsTruncated": None}, {"cropUnwateredCount": 3}, {"cropUnwateredTiles": None}])
def test_incomplete_native_sets_cannot_claim_zero_seedlings(patch):
    result = crop_water_facts({**farm(), **patch})
    assert not result["complete"]
    assert result["unwateredNonHarvestableCount"] == "unknown"


def test_native_totals_deduplicate_recovered_commands_and_ignore_old_narrative(tmp_path):
    store = WorkStore(tmp_path / 'data/work-state.json')
    entry = ExecutionEntry('native-one', 'job', 'step', 'harvest_auto', 'partial',
                           effects=[{"state": "harvested", "stack": 1}, {"state": "harvested", "stack": 1}])
    def seed(state):
        state.executions = [entry, replace(entry), replace(entry, command_id='native-two',
            operation='water_tiles', effects=[{"state": "watered"}])]
        state.last_job = {"actualSummary": "old model narrative: harvested 80", "effects": entry.effects}
    store._mutate('farm', seed)
    overview = store.overview('farm')
    context = build_decision_context({"payload": {"world": {}, "farmWork": farm()}}, work=overview)
    totals = context['confirmedExecutionTotals']
    assert totals['commandCount'] == 2
    assert totals['effectCounts'] == {"harvested": 2, "watered": 1}
    assert 'not inventory or a new delta' in totals['scope']
    assert 'including retained history' in totals['excludes']
    assert not totals['hasUnconfirmedCommands']


def test_request_totals_survive_reload_and_keep_save_history_separate(tmp_path):
    store = WorkStore(tmp_path / 'data/work-state.json')
    def seed(state):
        state.requests = [{'id': 'original-A', 'text': '收完后报告这轮累计', 'origin': 'player',
                           'taskIds': ['first', 'recovered', 'batch-8', 'batch-9'], 'goalId': 'goal'}]
        state.executions = [ExecutionEntry('older', 'other-request', 's', 'harvest_auto', 'completed',
                                          effects=[{'state': 'harvested', 'stack': 1}] * 16)]
        for task_id, count in [('first', 41), ('recovered', 5), ('batch-8', 8), ('batch-9', 9)]:
            state.executions.append(ExecutionEntry(task_id + '-cmd', task_id, 's', 'harvest_auto',
                                                   'completed', effects=[{'state': 'harvested', 'stack': 1}] * count))
        state.executions.append(replace(state.executions[-1]))
    store._mutate('farm', seed)
    context = build_decision_context({'payload': {'world': {}}},
                                    work=WorkStore(store.state_path).overview('farm'))
    assert context['confirmedExecutionTotals']['effectCounts']['harvested'] == 79
    actual = context['confirmedRequestTotals']['original-A']
    assert actual['effectCounts']['harvested'] == 63
    assert actual['commandCount'] == 4
    assert actual['requestText'] == '收完后报告这轮累计'
    assert actual['scope'] == 'original_player_request'


def test_autonomy_request_history_does_not_repeat_generated_snapshot(tmp_path):
    store = WorkStore(tmp_path / 'data/work-state.json')
    def seed(state):
        state.requests = [{'id': 'auto', 'origin': 'autonomy', 'text': '实时上下文：旧状态', 'taskIds': ['job']}]
        state.executions = [ExecutionEntry('native', 'job', 's', 'water_auto', 'completed',
                                          effects=[{'state': 'watered'}])]
    store._mutate('farm', seed)
    actual = store.overview('farm')['confirmedRequestTotals']['auto']
    assert actual['requestText'] is None
    assert actual['effectCounts'] == {'watered': 1}


def test_accepted_work_prompt_keeps_original_request_totals(tmp_path):
    from stardew_ai_runtime.chat_bridge import ChatBridge
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    def seed(state):
        state.requests = [{'id': 'original-A', 'text': '报告这轮', 'origin': 'player', 'taskIds': ['job']}]
        state.executions = [ExecutionEntry('native', 'job', 's', 'harvest_auto', 'completed',
                                          effects=[{'state': 'harvested', 'stack': 1}] * 63)]
    bridge._work_store._mutate('farm', seed)
    work = bridge._work_context('farm')
    context = build_decision_context({'payload': {'world': {}}}, work=work, origin='accepted-work')
    assert context['confirmedRequestTotals']['original-A']['effectCounts']['harvested'] == 63


def test_delivery_prompt_contains_original_total_when_recent_batch_is_only_nine(tmp_path):
    from stardew_ai_runtime.chat_bridge import ChatBridge
    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._decision_context = lambda *args, **kwargs: {}
    def seed(state):
        state.requests = [{'id': 'original-A', 'text': '报告这轮累计', 'origin': 'player',
                           'taskIds': ['first', 'recovered', 'batch8', 'batch9']}]
        state.executions = [ExecutionEntry('older', 'other', 's', 'harvest_auto', 'completed',
                                          effects=[{'state': 'harvested', 'stack': 1}] * 16)]
        for task_id, count in [('first', 41), ('recovered', 5), ('batch8', 8), ('batch9', 9)]:
            state.executions.append(ExecutionEntry(task_id, task_id, 's', 'harvest_auto', 'completed',
                                                   effects=[{'state': 'harvested', 'stack': 1}] * count))
    bridge._work_store._mutate('farm', seed)
    prompt = bridge._format_delivery_prompt([{'requestId': 'original-A', 'taskId': 'batch9',
        'commandId': 'batch9', 'outcome': 'completed', 'effects': [{'state': 'harvested'}] * 9}],
        'farm', midflight=[], execution_deferred=True)
    payload = json.loads(prompt.split('终态事实：', 1)[1].split('\n', 1)[0])
    assert payload['qualityCodes']['1'] == '银星' and payload['qualityCodes']['2'] == '金星'
    actual = payload['confirmedRequestTotals']['original-A']
    assert actual['effectCounts']['harvested'] == 63
    assert actual['scope'] == 'original_player_request'
    assert len(payload['terminalFacts'][0]['effects']) == 9
    assert set(payload['confirmedRequestTotals']) == {'original-A'}


def test_cancelled_native_terminal_updates_latest_result_after_decision_revocation(tmp_path):
    store = WorkStore(tmp_path / 'data/work-state.json')
    def seed(state):
        state.requests = [{'id': 'cancel-request', 'text': '灌满水壶再浇水', 'origin': 'player',
                           'taskIds': ['water'], 'goalId': 'goal'}]
        state.tasks = [Task('water', 'goal', '灌满再浇水', '实际完成', status='cancelled',
                            steps=[Step('refill', 'refill_watering_can', {}, status='running', command_id='native-cancel')])]
        state.last_job = {'taskId': 'old-B', 'status': 'completed', 'actualSummary': '已取出76件'}
        state.decision = {}
    store._mutate('farm', seed)
    store.commit_step_result('farm', task_id='water', step_id='refill', command_id='native-cancel',
                             outcome='cancelled', reason_code='CANCELLED', effects=[])
    state = WorkStore(store.state_path).state('farm')
    assert state.last_job['taskId'] == 'water'
    assert state.last_job['status'] == 'cancelled'
    assert state.executions[-1].request_id == 'cancel-request'
    assert state.executions[-1].goal_id == 'goal'
    assert state.outbox[-1]['requestId'] == 'cancel-request'


@pytest.mark.parametrize('task_status', ['running', 'cancelled'])
def test_native_cancellation_atomically_retires_unexecuted_steps(tmp_path, task_status):
    store = WorkStore(tmp_path / 'data/work-state.json')
    def seed(state):
        state.requests = [{'id': 'cancel-request', 'text': '灌满再浇水', 'origin': 'player',
                           'taskIds': ['water']}]
        state.tasks = [Task('water', 'goal', '灌满再浇水', status=task_status, steps=[
            Step('refill', 'refill_watering_can', {}, status='running', command_id='native-cancel'),
            Step('water', 'water_tiles', {'tiles': [{'x': 1, 'y': 2}]}),
        ])]
    store._mutate('farm', seed)
    store.commit_step_result('farm', task_id='water', step_id='refill', command_id='native-cancel',
                             outcome='cancelled', reason_code='CANCELLED', effects=[])
    state = WorkStore(store.state_path).state('farm')
    assert state.tasks[0].status == 'cancelled'
    assert [step.status for step in state.tasks[0].steps] == ['cancelled', 'cancelled']
    assert state.tasks[0].steps[1].command_id is None
    assert len(state.executions) == 1 and not state.executions[0].effects
    assert state.last_job['remainingSteps'][1]['status'] == 'cancelled'
    assert state.outbox[-1]['feedback']['remainingSteps'][1]['status'] == 'cancelled'


@pytest.mark.parametrize('archived', [False, True])
def test_delivery_uses_persisted_step_state_without_changing_native_receipt(tmp_path, archived):
    from dataclasses import asdict

    from stardew_ai_runtime.chat_bridge import ChatBridge

    bridge = ChatBridge(run_dir=tmp_path, enable_plan_worker=False)
    bridge._decision_context = lambda *args, **kwargs: {}
    task = Task('water', 'goal', '灌满再浇水', status='cancelled', steps=[
        Step('refill', 'refill_watering_can', {}, status='cancelled', command_id='native-cancel'),
        Step('water', 'water_tiles', {}, status='cancelled'),
    ])
    def seed(state):
        state.requests = [{'id': 'cancel-request', 'text': '灌满再浇水', 'origin': 'player',
                           'taskIds': ['water']}]
        if archived:
            state.archive = [{'task': asdict(task)}]
        else:
            state.tasks = [task]
    bridge._work_store._mutate('farm', seed)
    event = {'requestId': 'cancel-request', 'taskId': 'water', 'commandId': 'native-cancel',
             'operation': 'refill_watering_can', 'outcome': 'cancelled', 'effects': [],
             'feedback': {'candidateCount': 4, 'completedCount': 0, 'actualSummary': '未确认实际变化',
                          'remainingSteps': [{'stepId': 'water', 'operation': 'water_tiles', 'status': 'pending'}],
                          'stepResults': [{'stepId': 'refill', 'status': 'cancelled', 'candidateCount': 4}]}}
    prompt = bridge._format_delivery_prompt([event], 'farm', midflight=[], execution_deferred=True)
    fact = json.loads(prompt.split('终态事实：', 1)[1].split('\n', 1)[0])['terminalFacts'][0]
    assert fact['feedback']['taskStatus'] == 'cancelled'
    assert fact['feedback']['remainingSteps'][1]['status'] == 'cancelled'
    assert fact['feedback']['candidateCount'] == 4 and fact['feedback']['completedCount'] == 0
    assert fact['feedback']['stepResults'][0]['candidateCount'] == 4
    assert fact['outcome'] == 'cancelled' and fact['effects'] == []
    assert event['feedback']['remainingSteps'][0]['status'] == 'pending'


def test_withdrawal_quantity_contract_survives_compact_feedback():
    from stardew_ai_runtime.job_feedback import compact_job_feedback
    result = compact_job_feedback({'terminalState': 'partially-succeeded', 'completedCount': 1,
        'effects': [{'state': 'withdrawn', 'stack': 76, 'quality': 0}],
        'details': {'requestedQuantity': 77, 'actualQuantity': 76, 'remainingQuantity': 1,
                    'quantityUnit': 'items', 'nativeCountsScope': 'transferredStacks'}},
        operation='withdraw_from_chest', status='partial')
    assert result['status'] == 'partial'
    assert result['requestedQuantity'] == 77
    assert result['actualQuantity'] == 76
    assert result['remainingQuantity'] == 1
    assert result['nativeCountsScope'] == 'transferredStacks'


def test_transfer_quantities_count_stacks_not_effect_rows_and_keep_qualities(tmp_path):
    store = WorkStore(tmp_path / 'data/work-state.json')
    entry = ExecutionEntry('withdraw', 'job', 'take', 'withdraw_from_chest', 'completed',
        effects=[{'state': 'withdrawn', 'itemId': '(O)472', 'stack': 6, 'quality': 0}])
    deposit = replace(entry, command_id='deposit', operation='deposit_to_chest', effects=[
        {'state': 'deposited', 'itemId': '(O)24', 'stack': 15, 'quality': 0},
        {'state': 'deposited', 'itemId': '(O)24', 'stack': 1, 'quality': 1},
        {'state': 'skipped', 'itemId': '(T)Hoe'}])
    def seed(state):
        state.executions = [entry, replace(entry), deposit,
            replace(entry, command_id='ship', operation='ship_items',
                effects=[{'state': 'shipped', 'itemId': '(O)SummerSquash', 'count': 3}]),
            replace(entry, command_id='plant', operation='plant_seeds',
                effects=[{'state': 'planted', 'stack': 5}])]
    store._mutate('farm', seed)
    totals = build_decision_context({'payload': {'world': {}}}, work=store.overview('farm'))['confirmedExecutionTotals']
    assert totals['effectCounts']['withdrawn'] == 1
    assert totals['effectCounts']['deposited'] == 2
    assert totals['effectCountUnit'] == 'native_effect_records'
    assert totals['itemQuantities'] == {
        'withdrawn': {'unit': 'items', 'count': 6, 'knownCount': 6, 'unknownStackEffects': 0},
        'deposited': {'unit': 'items', 'count': 16, 'knownCount': 16, 'unknownStackEffects': 0},
        'shipped': {'unit': 'items', 'count': 3, 'knownCount': 3, 'unknownStackEffects': 0},
    }


@pytest.mark.parametrize('stack', [None, True, '4', 1.5, 0, -1])
def test_incomplete_item_effects_do_not_invent_a_quantity(tmp_path, stack):
    store = WorkStore(tmp_path / 'data/work-state.json')
    def seed(state):
        state.executions = [ExecutionEntry('native', 'job', 'step', 'harvest_auto', 'partial',
            effects=[{'state': 'harvested', 'stack': 3}, {'state': 'harvested', 'stack': stack}])]
    store._mutate('farm', seed)
    quantities = store.overview('farm')['confirmedExecutionTotals']['itemQuantities']
    assert quantities['harvested'] == {'unit': 'items', 'count': None,
        'knownCount': 3, 'unknownStackEffects': 1}


def test_control_receipt_is_queued_and_queries_use_live_native_facts(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', 'farm')
        monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'turn')
        data = tmp_path / 'data'
        data.mkdir()
        (data/'life-turn.json').write_text(json.dumps({'saveId':'farm','requestId':'turn'}))
        # An old chat snapshot must not replace a fresh native query.
        (data/'life-snapshot.json').write_text(json.dumps({'saveId':'farm',
            'capturedAt':time.time(), 'worldRevision':1, 'payload':{'world':{}}}))
        scheduler = CompanionScheduler(run_dir=tmp_path)
        scheduler.get_status = AsyncMock(return_value={'saveId':'farm'})
        scheduler.get_work_overview = AsyncMock(return_value={
            'saveId':'farm', 'worldRevision':2, 'companion':{'nonToolItems':[
                {'itemId':'(O)24','stack':8}]}, 'farmWork':farm()})
        server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler, surface='life')
        _, receipt = await server.call_tool('manage_arrangements', {'change': {'manage_companion': {'action':'pause'}}})
        assert receipt['accepted'] and receipt['status'] == 'queued'
        assert receipt['nativeOutcomeConfirmed'] is False
        _, fresh = await server.call_tool('query_game', {'query': {'get_work_overview': {}}})
        assert fresh['worldRevision'] == 2
        assert fresh['companion']['nonToolItems'][0]['stack'] == 8
        scheduler.get_work_overview.assert_awaited_once()
    asyncio.run(scenario())


def test_unreachable_receipt_requests_obstacle_decision_without_choosing_an_action():
    from stardew_ai_runtime.job_feedback import compact_job_feedback
    result = compact_job_feedback({"effects": [{"state": "skipped", "reason": "unreachable"}],
                                   "skippedCount": 1}, operation="refill_watering_can", status="partial")
    assert result["skipReasons"] == {"unreachable": 1}
    assert "player preservation" in result["obstacleAssessment"]
    assert "does not prove removable" in result["obstacleAssessment"]
    assert result["nextBusiness"] == "new_model_decision_required"
    assert "obstacleAssessment" not in compact_job_feedback({"reasonCode": "OUT_OF_STAMINA"})


def test_model_context_retains_totals_for_all_requests_offered_to_decision(tmp_path):
    from stardew_ai_runtime.decision_context import model_decision_context

    store = WorkStore(tmp_path / "work.json")
    def seed(state):
        state.requests = [{"id": name, "text": name, "origin": "player", "taskIds": [name]}
                          for name in ("current", "offered", "unrelated")]
        state.executions = [ExecutionEntry(name, name, "s", "harvest_auto", "completed",
                                           effects=[{"state": "harvested", "stack": count}])
                            for name, count in (("current", 3), ("offered", 8), ("unrelated", 5))]
    store._mutate("farm", seed)
    store.begin_decision("farm", "turn", request_id="current", request_ids=["offered"])
    context = build_decision_context({"payload": {"world": {}}}, work=store.overview("farm"))
    projected = model_decision_context(model_decision_context(context))
    assert set(projected["confirmedRequestTotals"]) == {"current", "offered"}
    assert projected["confirmedRequestTotals"]["offered"]["itemQuantities"]["harvested"]["count"] == 8
    assert "unrelated" in context["confirmedRequestTotals"]


def test_pickup_partial_failure_keeps_accepted_items_in_saved_feedback(tmp_path):
    store = WorkStore(tmp_path / 'work.json')
    def seed(state):
        state.tasks = [Task('pickup', 'goal', '拾取并入箱', status='running', steps=[
            Step('collect', 'pickup_items', {}, status='running', command_id='native-pickup')])]
    store._mutate('farm', seed)
    effects = [
        {'state': 'picked-up', 'collectedItems': [
            {'itemId': '(O)388', 'quality': 0, 'stack': 3},
            {'itemId': '(O)16', 'quality': 2, 'stack': 2}]},
        {'state': 'failed', 'reason': 'inventory-full', 'collectedItems': [
            {'itemId': '(O)388', 'quality': 0, 'stack': 1}]}]
    store.commit_step_result('farm', task_id='pickup', step_id='collect', command_id='native-pickup',
                             outcome='failed', reason_code='inventory-full', effects=effects)
    saved = WorkStore(store.state_path).state('farm')
    assert saved.last_job['status'] == 'failed'
    assert '已拾取 6 件' in saved.last_job['actualSummary']
    assert saved.executions[-1].effects == effects
