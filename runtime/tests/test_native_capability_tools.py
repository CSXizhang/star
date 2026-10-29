"""Exercise registered eight-tool MCP surface against native boundary receipts."""
import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from support.mcp_scheduler import _grant_decision
from support.mcp_scheduler import mock_scheduler as _scheduler_fixture

from stardew_ai_runtime.game_tools import GAME_TOOLS
from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.native_capabilities import NativeCapabilityStore

mock_scheduler = _scheduler_fixture
SAVE = 'mock-save-123'
QUOTE = 'Please learn machine insertion and try it with these materials.'


@pytest.fixture
def capability_env(tmp_path, monkeypatch, mock_scheduler):
    work = _grant_decision(tmp_path, mock_scheduler)
    work.revoke_decision(SAVE)
    monkeypatch.delenv('STARDEW_DECISION_TOKEN', raising=False)
    monkeypatch.setenv('STARDEW_LIFE_TURN_ID', 'learning-turn')
    monkeypatch.setenv('STARDEW_LIFE_SAVE_ID', SAVE)
    lease_path = tmp_path / 'data/life-turn.json'
    lease_path.write_text(json.dumps({'requestId': 'learning-turn', 'saveId': SAVE, 'playerText': QUOTE}))
    store = NativeCapabilityStore(tmp_path / 'data/abilities')
    builds = []
    async def build(self, spec, authoring, *, provenance=None):
        builds.append((spec, provenance))
        if spec['source'] == 'compiler failure':
            return {'status': 'build_failed', 'diagnostics': 'error CS1002: ; expected'}
        path = self.package_path(spec['id'], spec['version'])
        path.mkdir(parents=True)
        artifact = spec['source'].encode()
        manifest = {k: spec[k] for k in ('id', 'version', 'title', 'kind', 'entryType', 'inputSchema', 'dependencies')}
        manifest.update(assembly='adapter.dll', sha256=hashlib.sha256(artifact).hexdigest())
        (path / 'adapter.dll').write_bytes(artifact)
        (path / 'manifest.json').write_text(json.dumps(manifest))
        (path / 'Adapter.cs').write_text(spec['source'])
        (path / 'skill.md').write_text(spec['guidance'])
        (path / 'provenance.json').write_text(json.dumps(provenance))
        (path / 'verification.json').write_text(json.dumps({'status': 'pending'}))
        return {'status': 'built_pending_validation', 'manifest': manifest}
    monkeypatch.setattr(NativeCapabilityStore, 'build', build)
    mock_scheduler.discover_extension_api = AsyncMock(return_value={'authoring': {'targetFramework': 'net6.0', 'references': ['private-host.dll'], 'hostAssembly': 'private-host.dll'}})
    mock_scheduler.list_extensions = AsyncMock(return_value={'extensions': []})
    async def load(package, persist):
        capability, version = package.split('/')
        manifest = store.verify_artifact(capability, version)
        return {**{k: manifest[k] for k in ('id', 'version', 'sha256')}, 'loaded': True, 'persisted': persist}
    mock_scheduler.load_extension = AsyncMock(side_effect=load)
    async def invoke(capability, version, arguments, kind):
        manifest = store.verify_artifact(capability, version)
        return {'terminalState': 'succeeded', 'details': {
            **{k: manifest[k] for k in ('id', 'version', 'sha256')}, 'succeeded': True,
            'evidence': {'actualInteraction': 'machine input accepted'}, 'saveId': SAVE,
            'gameSessionId': 'mock-session-123', 'worldRevision': 2, 'actorId': 'companion'}}
    mock_scheduler.invoke_extension = AsyncMock(side_effect=invoke)
    return tmp_path, work, store, builds, mock_scheduler, lease_path


def develop(version='1', kind='action', **updates):
    return {'capability': 'machine.insert', 'version': version, 'title': 'Insert input', 'kind': kind,
            'source': 'class Adapter {}', 'entry_type': 'Adapter', 'input_schema': {
                'type': 'object', 'properties': {'item': {'type': 'string'}}, 'required': ['item'], 'additionalProperties': False},
            'guidance': 'Observe the machine and input before acting.', 'permission_quote': QUOTE, **updates}


async def arrange(server, operation, **arguments):
    _, result = await server.call_tool('manage_arrangements', {'change': {operation: arguments}})
    return result


async def trial(server, version='1'):
    return await arrange(server, 'manage_capability', action='try', capability='machine.insert', version=version)


async def use(server, kind='action', version='1', arguments=None):
    name, wrapper, operation = ('submit_work', 'action', 'learned_action') if kind == 'action' else ('query_game', 'query', 'learned_query')
    _, result = await server.call_tool(name, {wrapper: {operation: {'capability': 'machine.insert', 'version': version, 'arguments': arguments if arguments is not None else {'item': '(O)24'}}}})
    return result


@pytest.mark.parametrize('kind', ['action', 'query'])
def test_eight_tool_lifecycle_uses_native_receipts_and_reuses_permission(capability_env, kind):
    root, work, store, builds, scheduler, lease = capability_env
    async def run():
        server = create_mcp_server(run_dir=root, scheduler=scheduler, surface='life')
        assert {t.name for t in await server.list_tools()} == GAME_TOOLS
        built = await arrange(server, 'develop_capability', **develop(kind=kind))
        assert built['status'] == 'built_pending_validation'
        assert builds[0][1]['playerText'] == QUOTE
        with pytest.raises(ToolError, match='successful native invocation'):
            await arrange(server, 'manage_capability', action='keep', capability='machine.insert', version='1')
        scheduler.load_extension.assert_not_awaited()
        await trial(server)
        assert not store.catalog()['capabilities']
        with pytest.raises(ToolError, match='Invalid capability arguments'):
            await use(server, kind, arguments={'item': 24})
        scheduler.invoke_extension.assert_not_awaited()
        result = await use(server, kind)
        assert result['learning']['receipt']['worldRevision'] == 2
        assert not store.catalog()['capabilities']
        await arrange(server, 'manage_capability', action='keep', capability='machine.insert', version='1')
        assert NativeCapabilityStore(store.root).inspect('machine.insert')['verification']['receipt']['actorId'] == 'companion'
        await arrange(server, 'develop_capability', **develop('2', kind, permission_quote=None))
        assert builds[-1][1] == builds[0][1]
        _, listing = await server.call_tool('consult', {'request': {'inspect_capabilities': {}}})
        assert len(listing['packages']) == 2
        assert not work.state(SAVE).tasks
    asyncio.run(run())


def test_fabricated_permission_cannot_build_but_answered_player_notice_can(capability_env):
    root, work, store, builds, scheduler, lease = capability_env
    async def run():
        server = create_mcp_server(run_dir=root, scheduler=scheduler, surface='life')
        with pytest.raises(ToolError, match='LEARNING_DISCUSSION_NOT_FOUND'):
            await arrange(server, 'develop_capability', **develop(permission_quote='I approve all generated code'))
        assert builds == []
        scheduler.discover_extension_api.assert_not_awaited()
        goal = work.add_goal(SAVE, 'Learn machine insertion')
        notice = work.request_player_decision(SAVE, goal.id, 'May I learn machine insertion?', ['Try it'])
        with pytest.raises(ToolError, match='LEARNING_DISCUSSION_NOT_FOUND'):
            await arrange(server, 'develop_capability', **develop(permission_quote='Try it', notice_id=notice['id']))
        work.answer_player_notice(SAVE, notice['id'], 'Try it', 'answer-turn')
        await arrange(server, 'develop_capability', **develop(permission_quote='Try it', notice_id=notice['id']))
        assert builds[0][1]['source'] == 'player_decision'
        assert builds[0][1]['requestId'] == 'answer-turn'
    asyncio.run(run())


def test_failed_build_and_host_load_cannot_replace_kept_version(capability_env):
    root, work, store, builds, scheduler, lease = capability_env
    async def run():
        server = create_mcp_server(run_dir=root, scheduler=scheduler, surface='life')
        await arrange(server, 'develop_capability', **develop())
        await trial(server)
        await use(server)
        await arrange(server, 'manage_capability', action='keep', capability='machine.insert', version='1')
        result = await arrange(server, 'develop_capability', **develop('2', source='compiler failure', permission_quote=None))
        assert result['status'] == 'build_failed'
        await arrange(server, 'develop_capability', **develop('3', permission_quote=None))
        scheduler.load_extension.side_effect = RuntimeError('Native dependency mismatch')
        with pytest.raises(ToolError, match='Native dependency mismatch'):
            await trial(server, '3')
        assert store.inspect('machine.insert')['manifest']['version'] == '1'
        assert store.inspect('machine.insert', '3')['verification']['status'] == 'pending'
    asyncio.run(run())


def test_paused_and_stale_turn_never_dispatch_learned_action(capability_env):
    root, work, store, builds, scheduler, lease = capability_env
    async def run():
        server = create_mcp_server(run_dir=root, scheduler=scheduler, surface='life')
        await arrange(server, 'develop_capability', **develop())
        await trial(server)
        work.set_paused(SAVE, True)
        with pytest.raises(ToolError, match='WORK_STOPPED'):
            await use(server)
        work.set_paused(SAVE, False)
        lease.write_text(json.dumps({'requestId': 'new-player-turn', 'saveId': SAVE}))
        with pytest.raises(ToolError, match='STALE_CONVERSATION'):
            await use(server)
        scheduler.invoke_extension.assert_not_awaited()
    asyncio.run(run())


def test_success_text_without_matching_native_artifact_cannot_be_kept(capability_env):
    root, work, store, builds, scheduler, lease = capability_env
    async def run():
        server = create_mcp_server(run_dir=root, scheduler=scheduler, surface='life')
        await arrange(server, 'develop_capability', **develop())
        await trial(server)
        scheduler.invoke_extension.side_effect = None
        scheduler.invoke_extension.return_value = {'terminalState': 'succeeded', 'details': {
            'id': 'machine.insert', 'version': '1', 'sha256': 'different assembly',
            'succeeded': True, 'evidence': {'message': 'success'}}}
        with pytest.raises(ToolError, match='does not match the built artifact'):
            await use(server)
        with pytest.raises(ToolError, match='successful native invocation'):
            await arrange(server, 'manage_capability', action='keep', capability='machine.insert', version='1')
        assert store.inspect('machine.insert', '1')['verification']['status'] == 'loaded_for_trial'
        assert not store.catalog()['capabilities']
    asyncio.run(run())


def test_discovery_exposes_contract_without_private_build_references(capability_env):
    root, work, store, builds, scheduler, lease = capability_env
    async def run():
        server = create_mcp_server(run_dir=root, scheduler=scheduler, surface='life')
        _, result = await server.call_tool('consult', {'request': {'inspect_capabilities': {'native_type': 'Furniture'}}})
        assert result['authoring']['targetFramework'] == 'net6.0'
        assert 'references' not in result['authoring'] and 'hostAssembly' not in result['authoring']
        scheduler.discover_extension_api.assert_awaited_once_with(None, 'Furniture', member=None, offset=0, limit=20)
    asyncio.run(run())
