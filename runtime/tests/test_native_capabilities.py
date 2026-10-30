import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from stardew_ai_runtime.native_capabilities import (
    CapabilityError,
    NativeCapabilityStore,
    discover_dotnet,
)


def _package(store, version='1'):
    path = store.package_path('test.capability', version)
    path.mkdir(parents=True)
    (path / 'adapter.dll').write_bytes(b'assembly-' + version.encode())
    manifest = {'id': 'test.capability', 'version': version, 'assembly': 'adapter.dll',
                'sha256': hashlib.sha256((path / 'adapter.dll').read_bytes()).hexdigest()}
    (path / 'manifest.json').write_text(json.dumps(manifest))
    (path / 'skill.md').write_text('Observe actual results.')
    return manifest


def _receipt(manifest):
    return {**{k: manifest[k] for k in ('id', 'version', 'sha256')}, 'succeeded': True,
            'evidence': {'worldRevision': 42, 'inventory': []}, 'saveId': 'isolated-test', 'isolated': True}


def test_success_persists_but_failed_or_tampered_update_keeps_previous(tmp_path):
    store = NativeCapabilityStore(tmp_path)
    one = _package(store)
    store.record_native_validation('test.capability', '1', _receipt(one))
    store.record_native_persist('test.capability', '1', {**one, 'loaded': True, 'persisted': True})
    two = _package(store, '2')
    with pytest.raises(CapabilityError, match='Successful native'):
        store.record_native_validation('test.capability', '2', {**_receipt(two), 'succeeded': False})
    restarted = NativeCapabilityStore(tmp_path)
    assert restarted.inspect('test.capability')['manifest']['version'] == '1'
    (restarted.package_path('test.capability', '2') / 'adapter.dll').write_bytes(b'changed')
    with pytest.raises(CapabilityError, match='hash mismatch'):
        restarted.record_native_validation('test.capability', '2', _receipt(two))
    assert restarted.catalog()['capabilities']['test.capability']['version'] == '1'


def test_load_is_not_validation_and_receipt_must_bind_hash(tmp_path):
    store = NativeCapabilityStore(tmp_path)
    manifest = _package(store)
    store.record_native_load('test.capability', '1', {**manifest, 'loaded': True})
    assert store.inspect('test.capability', '1')['verification']['status'] == 'loaded_for_trial'
    assert not store.catalog()['capabilities']
    with pytest.raises(CapabilityError, match='match the built'):
        store.record_native_validation('test.capability', '1', {**_receipt(manifest), 'sha256': 'invented'})
    with pytest.raises(CapabilityError, match='observed evidence'):
        store.record_native_validation('test.capability', '1', {**_receipt(manifest), 'evidence': {}})
    store.record_native_validation('test.capability', '1', {**_receipt(manifest), 'isolated': False})
    assert not store.catalog()['capabilities']
    assert store.inspect('test.capability', '1')['verification']['scope'] == 'native_invocation_only'


def test_rollback_retains_previous_and_does_not_allow_path_escape(tmp_path):
    store = NativeCapabilityStore(tmp_path)
    for version in ('1', '2'):
        manifest = _package(store, version)
        store.record_native_validation('test.capability', version, _receipt(manifest))
        store.record_native_persist('test.capability', version, {**manifest, 'loaded': True, 'persisted': True})
    assert store.rollback('test.capability')['version'] == '1'
    with pytest.raises(CapabilityError):
        store.package_path('../escape', '1')


def test_real_sdk_build_and_compiler_failure_preserve_working_version(tmp_path):
    dotnet = discover_dotnet(Path(__file__).resolve())
    if not dotnet:
        pytest.skip('Local .NET SDK unavailable')
    reference = next((Path(dotnet).parent / 'packs/Microsoft.NETCore.App.Ref').glob('6.*/ref/net6.0/System.Text.Json.dll'))
    store = NativeCapabilityStore(tmp_path, dotnet=dotnet)
    spec = {'id': 'test.capability', 'version': '1', 'kind': 'query', 'entryType': 'Adapter',
            'inputSchema': {'type': 'object', 'properties': {}}, 'guidance': 'Read the observation.',
            'source': 'public class Adapter { public string Read() => "observed"; }'}
    authoring = {'targetFramework': 'net6.0', 'references': [str(reference)]}
    async def run():
        result = await store.build(spec, authoring, provenance={'playerText': 'Learn this query'})
        assert result['status'] == 'built_pending_validation', result
        manifest = store.verify_artifact('test.capability', '1')
        assert not store.catalog()['capabilities']
        store.record_native_validation('test.capability', '1', _receipt(manifest))
        store.record_native_persist('test.capability', '1', {**manifest, 'loaded': True, 'persisted': True})
        failed = await store.build({**spec, 'version': '2', 'source': 'public class Adapter { syntax error }'}, authoring)
        assert failed['status'] == 'build_failed'
        assert 'error CS' in failed['diagnostics']
        assert store.inspect('test.capability')['manifest']['version'] == '1'
        assert not store.package_path('test.capability', '2').exists()
        with pytest.raises(CapabilityError, match='already exists'):
            await store.build(spec, authoring)
    asyncio.run(run())


def test_persistence_requires_host_receipt_and_reload_preserves_evidence(tmp_path):
    store = NativeCapabilityStore(tmp_path)
    manifest = _package(store)
    persisted = {**manifest, 'loaded': True, 'persisted': True}
    with pytest.raises(CapabilityError, match='invocation evidence'):
        store.record_native_persist('test.capability', '1', persisted)
    store.record_native_validation('test.capability', '1', _receipt(manifest))
    store.record_native_load('test.capability', '1', {**manifest, 'loaded': True})
    with pytest.raises(CapabilityError, match='persisted native'):
        store.record_native_persist('test.capability', '1', {**persisted, 'persisted': False})
    assert not store.catalog()['capabilities']
    store.record_native_persist('test.capability', '1', persisted)
    assert NativeCapabilityStore(tmp_path).inspect('test.capability')['verification']['status'] == 'used_successfully'


def test_saved_native_version_wins_after_index_write_failure_and_restart(tmp_path, monkeypatch):
    from stardew_ai_runtime import native_capabilities as native

    store = NativeCapabilityStore(tmp_path)
    one = _package(store)
    store.record_native_validation('test.capability', '1', _receipt(one))
    store.record_native_persist('test.capability', '1', {**one, 'loaded': True, 'persisted': True})
    two = _package(store, '2')
    store.record_native_validation('test.capability', '2', _receipt(two))
    # Same location and contents written by GameExtensionHost.Persist, before
    # the bridge can update its secondary index.
    active = store.root / 'packages/active.json'
    active.write_text(json.dumps(['test.capability/2']))
    original_write = native._write_json
    def fail_index(path, data):
        if path.name == 'catalog.json':
            raise PermissionError('Index temporarily locked')
        original_write(path, data)
    monkeypatch.setattr(native, '_write_json', fail_index)
    kept = store.record_native_persist('test.capability', '2', {**two, 'loaded': True, 'persisted': True})
    assert 'indexWarning' in kept
    restarted = NativeCapabilityStore(tmp_path)
    assert restarted.inspect('test.capability')['manifest']['version'] == '2'
    assert restarted.catalog()['capabilities']['test.capability']['previousVersion'] == '1'
    monkeypatch.setattr(native, '_write_json', original_write)
    restarted.record_native_persist('test.capability', '2', {**two, 'loaded': True, 'persisted': True})
    # A successful native rollback is reflected even if the bridge exits before
    # recording its receipt. A later keep retains the reverse rollback target.
    active.write_text(json.dumps(['test.capability/1']))
    assert restarted.inspect('test.capability')['manifest']['version'] == '1'
    assert restarted.catalog()['capabilities']['test.capability']['previousVersion'] == '2'


def test_adapter_report_is_not_presented_as_independent_effect_verification(tmp_path):
    store = NativeCapabilityStore(tmp_path)
    manifest = _package(store)
    receipt = {**_receipt(manifest), 'evidence': {'message': 'Nothing needed changing'}}
    store.record_native_validation('test.capability', '1', receipt)
    store.record_native_persist('test.capability', '1', {**manifest, 'loaded': True, 'persisted': True})
    report = NativeCapabilityStore(tmp_path).inspect('test.capability')['verification']
    assert report['effectVerifiedByHost'] is False
    assert report['evidenceSource'] == 'adapter'
    assert report['receipt']['evidence'] == receipt['evidence']


def test_cancelling_build_kills_only_owned_compiler(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    store = NativeCapabilityStore(tmp_path, dotnet=str(_fake_dotnet(tmp_path / 'compiler', sdk='6.0.428', targeting_pack=True, runtime=True)))
    reference = tmp_path / 'host.dll'
    reference.write_bytes(b'host')
    spec = {'id': 'test.cancel', 'version': '1', 'kind': 'action', 'entryType': 'Adapter',
            'inputSchema': {'type': 'object'}, 'source': 'public class Adapter {}'}
    async def run():
        started = asyncio.Event()
        async def communicate():
            started.set()
            await asyncio.Event().wait()
        process = Mock(returncode=None, communicate=communicate, wait=AsyncMock())
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
        task = asyncio.create_task(store.build(spec, {'targetFramework': 'net6.0', 'references': [str(reference)]}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        assert not store.catalog()['capabilities']
        assert not store.package_path('test.cancel', '1').exists()
        records = list((tmp_path / 'learning').glob('*/build.json'))
        assert json.loads(records[0].read_text())['status'] == 'cancelled'
    asyncio.run(run())


def test_sdk_corrupt_download_never_becomes_installed(tmp_path, monkeypatch):
    import io
    import os
    import urllib.request
    if os.name != 'nt':
        pytest.skip('Automatic SDK setup targets Windows')
    import stardew_ai_runtime.native_capabilities as module
    monkeypatch.setattr(module.platform, 'machine', lambda: 'AMD64')
    store = NativeCapabilityStore(tmp_path)
    store.dotnet = None
    metadata = {'latest-sdk': '6.0.test', 'releases': [{'sdks': [{'version': '6.0.test', 'files': [
        {'rid': 'win-x64', 'name': 'sdk.zip', 'url': 'https://example.invalid/sdk.zip', 'hash': '0' * 128}
    ]}]}]}
    def response(url, **kwargs):
        return io.BytesIO(json.dumps(metadata).encode() if url.endswith('releases.json') else b'corrupt download')
    monkeypatch.setattr(urllib.request, 'urlopen', response)
    with pytest.raises(CapabilityError, match='checksum mismatch'):
        asyncio.run(store.prepare_builder())
    assert not (tmp_path / 'toolchain').exists()
    assert store.dotnet is None



def test_failed_compile_revision_reuses_real_discussion_within_same_scope(tmp_path):
    from stardew_ai_runtime.capability_consent import learning_provenance
    dotnet = discover_dotnet(Path(__file__).resolve())
    if not dotnet:
        pytest.skip('Local .NET SDK unavailable')
    reference = next((Path(dotnet).parent / 'packs/Microsoft.NETCore.App.Ref').glob('6.*/ref/net6.0/System.Text.Json.dll'))
    store = NativeCapabilityStore(tmp_path, dotnet=dotnet)
    spec = {'id': 'machine.insert', 'version': '1', 'kind': 'action', 'entryType': 'Adapter',
            'inputSchema': {'type': 'object'}, 'dependencies': [], 'source': 'public class Adapter { broken syntax }'}
    authoring = {'targetFramework': 'net6.0', 'references': [str(reference)]}
    provenance = learning_provenance(save_id='farm', request_id='player-turn',
        lease={'saveId': 'farm', 'requestId': 'player-turn', 'playerText': 'Learn this machine input'},
        quote='Learn this machine input')
    async def run():
        failed = await store.build(spec, authoring, provenance=provenance)
        assert failed['status'] == 'build_failed'
        assert not store.list_packages()
        restarted = NativeCapabilityStore(tmp_path, dotnet=dotnet)
        prior = restarted.recent_learning_provenance('machine.insert', save_id='farm', kind='action')
        reused = learning_provenance(save_id='farm', request_id='revision-turn', lease={}, quote=None, previous=prior)
        result = await restarted.build({**spec, 'version': '2', 'source': 'public class Adapter {}'}, authoring, provenance=reused)
        assert result['status'] == 'built_pending_validation'
        assert restarted.inspect('machine.insert', '2')['provenance'] == provenance
        assert restarted.recent_learning_provenance('machine.insert', save_id='other-farm', kind='action') is None
        assert restarted.recent_learning_provenance('machine.insert', save_id='farm', kind='query') is None
        assert restarted.recent_learning_provenance('machine.insert', save_id='farm', kind='action', dependencies=[{'id': 'New.Mod', 'version': '1'}]) is None
    asyncio.run(run())



def _fake_dotnet(root, *, sdk=None, targeting_pack=False, runtime=False):
    import os
    files = ["dotnet.exe" if os.name == "nt" else "dotnet"]
    if sdk:
        files.append(f"sdk/{sdk}/MSBuild.dll")
    if targeting_pack:
        files.append("packs/Microsoft.NETCore.App.Ref/6.0.36/ref/net6.0/System.Text.Json.dll")
    if runtime:
        files.append("shared/Microsoft.NETCore.App/6.0.36/System.Private.CoreLib.dll")
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test toolchain marker")
    return root / files[0]


def test_compiler_discovery_rejects_runtime_only_and_modern_sdk_missing_net6(tmp_path, monkeypatch):
    import stardew_ai_runtime.native_capabilities as module
    runtime = _fake_dotnet(tmp_path / 'runtime-only', runtime=True)
    modern = _fake_dotnet(tmp_path / 'modern-sdk', sdk='9.0.100')
    incomplete = _fake_dotnet(tmp_path / 'missing-runtime', sdk='6.0.428', targeting_pack=True)
    complete = _fake_dotnet(tmp_path / 'complete-sdk', sdk='6.0.428', targeting_pack=True, runtime=True)
    monkeypatch.setattr(module.shutil, 'which', lambda name: str(modern))
    monkeypatch.setenv('DOTNET_ROOT', str(complete.parent))
    assert module.dotnet6_sdk_version(runtime) is None
    assert module.dotnet6_sdk_version(modern) is None
    assert module.dotnet6_sdk_version(incomplete) is None
    assert discover_dotnet() == str(complete.resolve())
    assert NativeCapabilityStore(tmp_path / 'store', dotnet=str(modern)).dotnet is None


def test_build_reports_setup_required_for_sdk_without_net6_pack(tmp_path):
    incomplete = _fake_dotnet(tmp_path / 'compiler', sdk='6.0.428', runtime=True)
    store = NativeCapabilityStore(tmp_path / 'store', dotnet=str(incomplete))
    result = asyncio.run(store.build({'id': 'example', 'version': '1', 'kind': 'action',
        'entryType': 'Adapter', 'inputSchema': {'type': 'object'}, 'source': 'class Adapter {}'}, {}))
    assert result['status'] == 'setup_required'
    assert not store.list_packages()



def test_self_contained_reference_filter_uses_sdk_names_not_game_directory_or_prefix(tmp_path):
    from stardew_ai_runtime.native_capabilities import _adapter_references
    compiler = _fake_dotnet(tmp_path / 'sdk', sdk='6.0.428', targeting_pack=True, runtime=True)
    private_xml = compiler.parent / 'shared/Microsoft.NETCore.App/6.0.36/System.Private.Xml.dll'
    private_xml.write_bytes(b'framework implementation behind facade')
    game = tmp_path / 'self-contained-game'
    game.mkdir()
    names = ['System.Text.Json.dll', 'System.Private.CoreLib.dll', 'System.Private.Xml.dll',
             'Stardew Valley.dll', 'StardewModdingAPI.dll', 'MonoGame.Framework.dll',
             'Netcode.dll', 'System.CustomGame.dll']
    for name in names:
        (game / name).write_bytes(b'reference marker')
    references = [str(game / name) for name in names]
    selected = _adapter_references(str(compiler), references + references[-1:])
    assert {Path(p).name for p in selected} == set(names[3:])
    assert len(selected) == len(names[3:])
