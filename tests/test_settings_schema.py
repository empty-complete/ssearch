"""Owner settings contracts use real files, revision checks and recovery."""
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import uuid

import pytest


def api(name):
    module = f'atom_search.{name}'
    assert importlib.util.find_spec(module) is not None, f'{module} is not implemented'
    return importlib.import_module(module)


def repository(profile):
    if not (profile / 'settings.json').exists():
        profile.mkdir(parents=True, exist_ok=True)
        selected = api('settings.migration').default_settings()
        selected['sources'] = [dict(id=str(uuid.uuid4()), path=r'\\example\documents', label='', enabled=True)]
        (profile / 'settings.json').write_text(json.dumps(selected), encoding='utf-8')
    return api('settings.repository').SettingsRepository(profile)


def test_legacy_root_days_migrate_once_without_default_substitution(tmp_path):
    path = tmp_path / 'settings.json'
    original = json.dumps({'root': r'\\сервер\Папка', 'refresh_days': 7,
                           'last_attempt': 12.5, 'last_refresh': 10}, ensure_ascii=False).encode()
    path.write_bytes(original)
    first = repository(tmp_path).load()
    assert first.schema_version == 3 and first.revision == 1
    assert first.settings['sources'][0]['path'] == r'\\сервер\Папка'
    assert str(uuid.UUID(first.settings['sources'][0]['id'])) == first.settings['sources'][0]['id']
    assert first.settings['indexing']['interval_seconds'] == 604800
    assert 'root' not in first.settings and 'last_attempt' not in first.settings
    assert (tmp_path / 'engine/settings-backups/legacy.json').read_bytes() == original
    runtime = json.loads((tmp_path / 'engine/runtime/desktop-state.json').read_bytes())
    assert runtime['last_attempt'] == 12.5 and runtime['last_refresh'] == 10
    second = repository(tmp_path).load()
    assert second == first
    assert not (tmp_path / 'engine/postgresql/18').exists()


@pytest.mark.parametrize('value', [
    {'root': ''}, {'root': False}, {'refresh_days': True}, {'refresh_days': 0},
    {'refresh_days': 366}, {'schema_version': 99}, {'root': 'C:\\', 'password': 'never persist'},
])
def test_invalid_legacy_or_future_settings_preserve_original(tmp_path, value):
    path = tmp_path / 'settings.json'
    original = json.dumps(value).encode()
    path.write_bytes(original)
    with pytest.raises(Exception) as failure:
        repository(tmp_path).load()
    assert getattr(failure.value, 'code', None) in {'settings_invalid', 'settings_schema_unsupported'}
    assert path.read_bytes() == original
    assert not (tmp_path / 'engine/settings-backups/legacy.json').exists()


def test_validation_is_pure_and_rejects_unknown_secrets_bool_and_duplicate_roots(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    current = repo.load()
    coordinator = api('service.mutations').MutationCoordinator(repo)
    candidate = deepcopy(current.settings)
    candidate['database']['password'] = 'must not appear in errors'
    candidate['indexing']['interval_seconds'] = True
    candidate['sources'].append(dict(candidate['sources'][0], id=str(uuid.uuid4()),
                                     path=r'//EXAMPLE/documents/'))
    before = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    monkeypatch.setattr(Path, 'open', lambda *a, **k: pytest.fail('validate performed file I/O'))
    result = coordinator.validate(candidate, current.revision)
    assert not result.valid
    assert {'database.password', 'indexing.interval_seconds', 'sources.1.path'} <= {e.path for e in result.errors}
    assert 'must not appear' not in repr(result.errors)
    assert candidate['indexing']['interval_seconds'] is True
    with pytest.raises(Exception) as failure:
        coordinator.validate(current.settings, current.revision + 1)
    assert failure.value.code == 'revision_conflict' and failure.value.status_code == 409
    monkeypatch.undo()
    assert {p: p.read_bytes() for p in tmp_path.rglob('*.json')} == before


def test_committed_apply_failure_restores_old_values_with_new_revision(tmp_path):
    repo = repository(tmp_path)
    old = repo.load()
    for revision in range(2, 9):
        old = repo.commit(dict(old.settings, revision=revision), old.revision)
    before_policy = (tmp_path / 'engine/settings.json').read_bytes()
    candidate = deepcopy(old.settings)
    candidate['sources'][0]['path'] = r'D:\Новая папка'
    candidate['indexing']['interval_seconds'] = 604800
    applied = []
    def hook(snapshot):
        applied.append(snapshot)
        if snapshot.settings['sources'][0]['path'] == r'D:\Новая папка':
            raise RuntimeError('controlled postcommit component failure')
    coordinator = api('service.mutations').MutationCoordinator(repo, apply=hook)
    with pytest.raises(Exception) as failure:
        coordinator.owner_apply(candidate, 8)
    assert failure.value.code == 'settings_apply_failed'
    restored = repo.load()
    assert restored.revision == 10
    assert restored.settings['sources'] == old.settings['sources']
    assert restored.settings['indexing'] == old.settings['indexing']
    assert [s.revision for s in applied] == [9, 10]
    assert (tmp_path / 'engine/settings.json').read_bytes() == before_policy
    assert json.loads((tmp_path / 'engine/runtime/settings-operation.json').read_bytes())['state'] == 'rolled_back'


def test_crash_after_disk_commit_is_recovered_before_next_request(tmp_path):
    repo = repository(tmp_path)
    old = repo.load()
    candidate = deepcopy(old.settings)
    candidate['sources'][0]['path'] = r'D:\Unapplied'
    coordinator = api('service.mutations').MutationCoordinator(
        repo, apply=lambda snapshot: (_ for _ in ()).throw(SystemExit('simulated crash')))
    with pytest.raises(SystemExit):
        coordinator.owner_apply(candidate, old.revision)
    assert json.loads((tmp_path / 'settings.json').read_bytes())['revision'] == 2
    assert json.loads((tmp_path / 'engine/runtime/settings-operation.json').read_bytes())['state'] == 'committed'
    recovered_repo = repository(tmp_path)
    recovered = recovered_repo.load()
    assert recovered.revision == 3
    assert recovered.settings['sources'] == old.settings['sources']
    assert api('service.mutations').MutationCoordinator(recovered_repo).validate(recovered.settings, 3).valid


def test_busy_worker_prevents_source_change_without_committing(tmp_path):
    import threading
    from PySide6.QtCore import QObject
    from atom_search.desktop.workers import Workers
    repo = repository(tmp_path)
    old = repo.load()
    before = (tmp_path / 'settings.json').read_bytes()
    candidate = deepcopy(old.settings)
    candidate['sources'][0]['path'] = r'D:\Busy'
    started, release, read_finished, cancelling = (threading.Event() for _ in range(4))
    class Service:
        def close(self):
            pass
    owner = QObject()
    workers = Workers(Service, owner)
    def busy(service, progress, cancel):
        started.set()
        cancel.wait(2)
        cancelling.set()
        release.wait(2)
    job = workers.start('scan', busy)
    assert started.wait(1)
    coordinator = api('service.mutations').MutationCoordinator(
        repo, quiesce=lambda changes, timeout: workers.quiesce((job,), .2))
    failures = []
    def apply():
        try:
            coordinator.owner_apply(candidate, old.revision)
        except Exception as error:
            failures.append(error)
    thread = threading.Thread(target=apply)
    try:
        thread.start()
        assert cancelling.wait(1)
        workers.start('status', lambda service, progress, cancel: read_finished.set())
        assert read_finished.wait(1), 'read-only worker waited for the mutation'
        assert coordinator.validate(old.settings, old.revision).valid
        thread.join(2)
        assert not thread.is_alive() and len(failures) == 1
        assert failures[0].code == 'settings_busy'
        assert (tmp_path / 'settings.json').read_bytes() == before
        assert repo.load() == old
    finally:
        release.set()
        thread.join(3)
        workers.shutdown()
        workers.wait()


def test_quota_change_is_rejected_and_reopen_keeps_actual_p1_policy(tmp_path):
    from atom_search.search_runtime import _prepare_profile
    (tmp_path / 'settings.json').write_text(json.dumps({'root': r'C:\Docs', 'storage': {
        'quota_bytes': 800000000, 'min_free_bytes': 0}}))
    with _prepare_profile(tmp_path, tmp_path / 'missing'):
        pass
    # External edits cannot replace the canonical P1 domain policy on reopen.
    (tmp_path / 'settings.json').write_text(json.dumps({'root': r'C:\Docs', 'storage': {
        'quota_bytes': 900000000, 'min_free_bytes': 0}}))
    repo = repository(tmp_path)
    old = repo.load()
    assert old.settings['storage'] == {'quota_bytes': 800000000, 'min_free_bytes': 0}
    assert not any(field.startswith('storage') for field in old.mutable_fields)
    candidate = deepcopy(old.settings)
    candidate['storage']['quota_bytes'] = 1000000000
    with pytest.raises(Exception) as failure:
        api('service.mutations').MutationCoordinator(repo).owner_apply(candidate, old.revision)
    assert failure.value.code == 'settings_immutable'
    assert repository(tmp_path).load().settings['storage'] == old.settings['storage']


def test_oversized_outer_json_is_not_overwritten(tmp_path):
    path = tmp_path / 'settings.json'
    original = json.dumps({'root': 'C:\\' + 'я' * 40000}).encode()
    path.write_bytes(original)
    with pytest.raises(Exception) as failure:
        repository(tmp_path).load()
    assert failure.value.code == 'settings_too_large'
    assert path.read_bytes() == original


def test_incomplete_commit_blocks_owner_requests_until_recovery(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    old = repo.load()
    candidate = deepcopy(old.settings)
    candidate['ui']['theme'] = 'light'
    module = api('settings.repository')
    real_write = module.atomic_write_json
    def interrupted(path, value):
        if path == repo.journal_path and value.get('state') == 'committed':
            raise OSError('controlled loss of journal acknowledgement')
        return real_write(path, value)
    monkeypatch.setattr(module, 'atomic_write_json', interrupted)
    coordinator = api('service.mutations').MutationCoordinator(repo)
    with pytest.raises(OSError):
        coordinator.owner_apply(candidate, old.revision)
    with pytest.raises(Exception) as failure:
        coordinator.validate(old.settings, old.revision)
    assert failure.value.code == 'settings_recovery_required'
    monkeypatch.undo()
    restored = repository(tmp_path).load()
    assert restored.revision == 3 and restored.settings['ui']['theme'] == 'system'


def test_stale_second_writer_cannot_replace_newer_disk_revision(tmp_path):
    first = repository(tmp_path)
    old = first.load()
    second = repository(tmp_path)
    second.load()
    current = first.commit(dict(old.settings, revision=2), 1)
    before = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(Exception) as failure:
        second.commit(old.settings, 1)
    assert failure.value.code == 'revision_conflict' and failure.value.status_code == 409
    assert (tmp_path / 'settings.json').read_bytes() == before
    assert repository(tmp_path).load() == current


def test_postcommit_admission_release_failure_restores_settings(tmp_path, monkeypatch):
    from contextlib import contextmanager
    repo = repository(tmp_path)
    old = repo.load()
    candidate = deepcopy(old.settings)
    candidate['ui']['theme'] = 'light'
    reserve = repo.budget.reserve
    failed = False
    @contextmanager
    def release_failure(size, operation):
        nonlocal failed
        with reserve(size, operation) as reservation:
            yield reservation
        if operation == 'owner_settings' and not failed:
            failed = True
            raise OSError('controlled postcommit reservation release failure')
    monkeypatch.setattr(repo.budget, 'reserve', release_failure)
    with pytest.raises(Exception) as failure:
        api('service.mutations').MutationCoordinator(repo).owner_apply(candidate, old.revision)
    assert failure.value.code == 'settings_apply_failed'
    restored = repository(tmp_path).load()
    assert restored.revision == 3 and restored.settings['ui'] == old.settings['ui']


def test_utf8_limit_matches_written_bytes_and_next_byte_is_rejected(tmp_path):
    repo = repository(tmp_path)
    old = repo.load()
    candidate = deepcopy(old.settings)
    base_size = len(json.dumps(candidate, ensure_ascii=False).encode('utf-8'))
    remaining = 65536 - base_size
    candidate['sources'][0]['label'] = 'я' * (remaining // 2) + 'x' * (remaining % 2)
    current = repo.commit(candidate, old.revision)
    written = (tmp_path / 'settings.json').read_bytes()
    assert len(written) == 65536
    assert repository(tmp_path).load() == current
    candidate = deepcopy(current.settings)
    candidate['sources'][0]['label'] += 'x'
    with pytest.raises(Exception) as failure:
        repo.commit(candidate, current.revision)
    assert failure.value.code == 'settings_too_large'
    assert (tmp_path / 'settings.json').read_bytes() == written


def test_early_preparation_uses_selected_model_context_without_inference(tmp_path, monkeypatch):
    from atom_search_core import embeddings
    monkeypatch.setattr(embeddings, 'OnnxEmbedder', lambda _: pytest.fail('settings loaded inference'))
    repo = api('settings.repository').SettingsRepository(tmp_path, model_dir=tmp_path / 'missing')
    repo.load()
    state = json.loads((tmp_path / 'engine/lifecycle.json').read_bytes())
    assert state['target_manifest'] is None and state['source'] is None and not state['used']
    assert not (tmp_path / 'engine/postgresql/18').exists()


def test_crash_after_rollback_file_write_finishes_same_rollback_revision(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    old = repo.load()
    candidate = deepcopy(old.settings)
    candidate['ui']['theme'] = 'light'
    module = api('settings.repository')
    real_write = module.atomic_write_json
    def interrupted(path, value):
        if path == repo.journal_path and value.get('state') == 'rolled_back':
            raise SystemExit('simulated interruption after rollback replace')
        return real_write(path, value)
    monkeypatch.setattr(module, 'atomic_write_json', interrupted)
    def failed_hook(snapshot):
        raise RuntimeError('controlled apply failure')
    coordinator = api('service.mutations').MutationCoordinator(repo, apply=failed_hook)
    with pytest.raises(SystemExit):
        coordinator.owner_apply(candidate, 1)
    assert json.loads((tmp_path / 'settings.json').read_bytes())['revision'] == 3
    monkeypatch.undo()
    restored = repository(tmp_path).load()
    assert restored.revision == 3 and restored.settings['ui'] == old.settings['ui']
    assert json.loads((tmp_path / 'engine/runtime/settings-operation.json').read_bytes())['state'] == 'rolled_back'


@pytest.mark.parametrize('legacy', [True, False])
def test_concurrent_first_loads_keep_one_source_identity_under_real_host_guard(tmp_path, monkeypatch, legacy):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    import threading
    from atom_search import search_runtime
    if legacy:
        (tmp_path / 'settings.json').write_text(json.dumps({'root': r'C:\Saved', 'refresh_days': 7}))
    original_prepare = search_runtime._prepare_profile
    barrier = threading.Barrier(2)
    @contextmanager
    def concurrent_prepare(*args, **kwargs):
        barrier.wait(timeout=10)
        with original_prepare(*args, **kwargs) as prepared:
            yield prepared
    monkeypatch.setattr(search_runtime, '_prepare_profile', concurrent_prepare)
    def load():
        return api('settings.repository').SettingsRepository(tmp_path, model_dir=tmp_path / 'missing').load()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(load) for _ in range(2)]
        snapshots = [future.result(timeout=20) for future in futures]
    assert snapshots[0] == snapshots[1]
    persisted = json.loads((tmp_path / 'settings.json').read_bytes())
    if legacy:
        assert persisted['sources'][0]['id'] == snapshots[0].settings['sources'][0]['id']
        assert persisted['sources'][0]['path'] == r'C:\Saved'
    else:
        assert persisted['sources'] == []
    assert persisted['revision'] == 1


@pytest.mark.parametrize('revision', [8, 98])
def test_commit_rejects_rollback_revision_growth_before_any_write(tmp_path, revision):
    repo = repository(tmp_path)
    candidate = repo.load().settings
    candidate['revision'] = revision
    padding = 65536 - len(json.dumps(candidate, ensure_ascii=False).encode('utf-8'))
    candidate['sources'][0]['label'] = 'x' * padding
    original = json.dumps(candidate, ensure_ascii=False).encode('utf-8')
    assert len(original) == 65536
    (tmp_path / 'settings.json').write_bytes(original)
    repo = repository(tmp_path)
    current = repo.load()
    applied = []
    def hook(snapshot):
        applied.append(snapshot)
        if snapshot.revision == revision + 1:
            raise RuntimeError('controlled component failure')
    with pytest.raises(Exception) as failure:
        api('service.mutations').MutationCoordinator(repo, apply=hook).owner_apply(current.settings, revision)
    assert failure.value.code == 'settings_too_large'
    assert applied == []
    assert (tmp_path / 'settings.json').read_bytes() == original
    assert not repo.journal_path.exists()
    assert repository(tmp_path).load() == current


@pytest.mark.parametrize('updates', [
    {'enabled': False, 'channel': 'nightly', 'source': None},
    {'enabled': True, 'channel': 'stable', 'source': {
        'kind': 'https', 'manifest_location': 'https://updates.example.test/release.json',
        'allowed_origins': ['https://updates.example.test'], 'credential_ref': 'user-choice'}},
])
def test_schema2_upgrade_preserves_sources_uuid_updates_and_original_bytes(tmp_path, updates):
    original = api('settings.migration').default_settings()
    original.update(schema_version=2, revision=41)
    original['sources'] = [dict(id=str(uuid.uuid4()), path=r'D:\Selected', label='Keep me', enabled=False)]
    original['updates'] = deepcopy(updates)
    raw = json.dumps(original, ensure_ascii=False, indent=3).encode('utf-8')
    (tmp_path / 'settings.json').write_bytes(raw)
    repo = api('settings.repository').SettingsRepository(tmp_path)
    loaded = repo.load()
    assert loaded.settings == dict(original, schema_version=3, revision=42)
    assert (tmp_path / 'engine/settings-backups/schema2.json').read_bytes() == raw
    assert api('settings.repository').SettingsRepository(tmp_path).load() == loaded
    with pytest.raises(api('settings.schema').SettingsValidationError):
        api('service.mutations').MutationCoordinator(repo).owner_apply(
            dict(loaded.settings, schema_version=2), loaded.revision)


def test_empty_profile_read_apply_restart_has_no_implicit_source(tmp_path):
    repo = api('settings.repository').SettingsRepository(tmp_path)
    fresh = repo.load()
    assert fresh.schema_version == 3 and fresh.revision == 1
    assert fresh.settings['sources'] == []
    assert fresh.settings['updates']['source'] == api('settings.migration').DEFAULT_UPDATE_SOURCE
    coordinator = api('service.mutations').MutationCoordinator(repo)
    assert coordinator.validate(fresh.settings, fresh.revision).valid
    candidate = deepcopy(fresh.settings)
    candidate['ui']['theme'] = 'dark'
    applied = coordinator.owner_apply(candidate, fresh.revision)
    assert applied.settings['sources'] == [] and applied.revision == 2
    assert api('settings.repository').SettingsRepository(tmp_path).load() == applied
    assert not (tmp_path / 'engine/postgresql/18').exists()


def test_empty_profile_can_add_and_remove_source_without_changing_identity(tmp_path):
    repo = api('settings.repository').SettingsRepository(tmp_path)
    fresh = repo.load()
    source = dict(id=str(uuid.uuid4()), path=r'C:\Documents', label='', enabled=True)
    coordinator = api('service.mutations').MutationCoordinator(repo)
    chosen = coordinator.owner_apply(dict(fresh.settings, sources=[source]), fresh.revision)
    empty = coordinator.owner_apply(dict(chosen.settings, sources=[]), chosen.revision)
    assert api('settings.repository').SettingsRepository(tmp_path).load() == empty
    restored = coordinator.owner_apply(dict(empty.settings, sources=[source]), empty.revision)
    assert restored.settings['sources'] == [source]
