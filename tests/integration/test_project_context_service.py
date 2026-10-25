"""The public owner shares metadata context, storage admission and restart policy."""
import json
import threading
import time
import uuid

import pytest


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), 'owned context queue did not reach its boundary'


@pytest.mark.parametrize('headroom_mib', [8, 4])
def test_public_owner_quota_allows_unchanged_context_and_refuses_real_change_without_recovery(tmp_path, headroom_mib):
    from atom_search.runtime import _windows
    from atom_search.service.contracts import ServiceError
    from atom_search.service.owner import open_service_owner
    source = tmp_path / 'documents'; source.mkdir()
    owner = open_service_owner(tmp_path / 'profile', tmp_path / 'missing', automatic_catalog=False)
    try:
        facade = owner.facade(owner.owner_access)
        initial = facade.settings_get()
        candidate = initial.settings
        candidate['sources'] = [dict(id=str(uuid.uuid4()), path=str(source), label='', enabled=True)]
        facade.settings_apply(candidate, initial.revision)
        owner.start()
        current = facade.settings_get()
        revision = owner.core.projection.configuration()[0]
        budget = owner.runtime.storage_budget
        with budget.reserve(budget.quota_bytes - _windows.allocated_bytes(budget.roots) - headroom_mib * 1024**2,
                            'settings-headroom-window'):
            candidate = current.settings
            candidate['ui']['theme'] = 'dark'
            applied = facade.settings_apply(candidate, current.revision)
            assert applied.settings['ui']['theme'] == 'dark'
            assert owner.core.projection.configuration()[0] == revision
            change = applied.settings
            source_id = applied.settings['sources'][0]['id']
            change['projects'] = [dict(id=str(uuid.uuid4()), canonical_name='Новый контекст',
                source_ids=[source_id], bindings=[dict(source_id=source_id, path_prefix='',
                    confirmed=True, basis='QA owner binding')])]
            with pytest.raises(ServiceError) as failure:
                facade.settings_apply(change, applied.revision)
            assert failure.value.code == 'settings_apply_failed'
            restored = facade.settings_get()
            assert restored.settings['sources'] == applied.settings['sources']
            assert restored.settings['ui'] == applied.settings['ui']
            assert not owner.repository._pending
            assert owner.core.projection.configuration()[0] == revision
            with owner.runtime.open_connection() as connection:
                assert connection.execute('SELECT settings_revision,context_revision '
                    'FROM atom_search.project_context_state').fetchone() == (restored.revision, revision)
        assert facade.settings_get() == restored
    finally:
        owner.close()
        assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None


def test_public_automatic_context_cancel_survives_unrelated_apply(tmp_path, monkeypatch):
    from atom_search.service.jobs import CatalogScheduler
    from atom_search.service.owner import open_service_owner
    from atom_search_core import SearchService
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = SearchService.index_embedding_batch
    def blocked(core, *, scope, cancel, publish_if):
        calls.append(scope.source_ids)
        entered.set()
        while not release.wait(.01):
            pass  # Hold the cancelled writer until after the two settings applies.
        return original(core, scope=scope, cancel=cancel, publish_if=publish_if)
    monkeypatch.setattr(SearchService, 'index_embedding_batch', blocked)
    # Keep the independently covered daily timer outside this deterministic
    # context-only test; automatic context admission remains enabled.
    monkeypatch.setattr(CatalogScheduler, '_spread', lambda self, identity: 86400)
    source = tmp_path / 'documents'; source.mkdir()
    owner = open_service_owner(tmp_path / 'profile', tmp_path / 'missing', automatic_catalog=True)
    try:
        facade = owner.facade(owner.owner_access)
        current = facade.settings_get()
        candidate = current.settings
        candidate['sources'] = [dict(id=str(uuid.uuid4()), path=str(source), label='', enabled=True)]
        facade.settings_apply(candidate, current.revision)
        owner.start()
        assert entered.wait(5)
        jobs = owner.scheduler.snapshot()
        assert len(jobs) == 1 and jobs[0].phase == 'embed'
        facade.cancel_job(jobs[0].job_id)
        current = facade.settings_get()
        candidate = current.settings
        candidate['ui']['theme'] = 'dark'
        applied = facade.settings_apply(candidate, current.revision)
        assert len(owner.scheduler.snapshot()) == len(calls) == 1
        # A real new context still has authority to create fresh bounded work.
        candidate = applied.settings
        source_id = applied.settings['sources'][0]['id']
        candidate['projects'] = [dict(id=str(uuid.uuid4()), canonical_name='Новый контекст',
            source_ids=[source_id], bindings=[dict(source_id=source_id, path_prefix='',
                confirmed=True, basis='QA owner binding')])]
        facade.settings_apply(candidate, applied.revision)
        assert len(owner.scheduler.snapshot()) == 2  # Fresh context may queue behind the cancelled writer.
        release.set()
        wait_for(lambda: len(calls) == 2 and all(job.state not in ('queued', 'running')
                 for job in owner.scheduler.snapshot()))
    finally:
        release.set()
        owner.close()
        assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None


def test_owner_context_budget_apply_and_restart_never_crawl_documents(tmp_path, monkeypatch):
    from atom_search.runtime import _windows
    from atom_search.runtime.storage import StorageBudgetError
    from atom_search.service.owner import open_service_owner
    from atom_search_core.source import FileSystemSource
    source = tmp_path / 'documents'
    source.mkdir()
    (source / 'Ведомость.xlsx').touch()
    profile, missing = tmp_path / 'profile', tmp_path / 'missing-model'
    owner = open_service_owner(profile, missing, automatic_catalog=False)
    try:
        snapshot = owner.repository.snapshot()
        candidate = snapshot.settings
        candidate['sources'] = [dict(id=str(uuid.uuid4()), path=str(source), label='Проекты', enabled=True)]
        owner.coordinator.owner_apply(candidate, snapshot.revision)
        owner.start()
        assert owner.core.scan(str(source))['files'] == 1
        core, budget = owner.core, owner.runtime.storage_budget
        def context_rows():
            with owner.runtime.open_connection() as connection:
                return connection.execute('SELECT context_text,input_hash FROM atom_search.file_project_context').fetchall()
        with budget.reserve(budget.quota_bytes - _windows.allocated_bytes(budget.roots) - 8 * 1024**2,
                            'competing-context-job'):
            with pytest.raises(StorageBudgetError, match='quota_exceeded'):
                core.index_context_batch()
            assert context_rows() == []
        def no_crawl(*args, **kwargs):
            raise AssertionError('context refresh enumerated unchanged documents')
        monkeypatch.setattr(FileSystemSource, 'snapshots', no_crawl)
        assert core.index_context_batch()['published'] == 1
        assert context_rows()[0][1] is None and 'source: Проекты' in context_rows()[0][0]
        before = owner.repository.snapshot()
        changed = before.settings
        changed['sources'][0]['label'] = 'Архив'
        owner.coordinator.owner_apply(changed, before.revision)
        assert core.index_context_batch()['published'] == 1
        expected = context_rows()
        assert 'source: Архив' in expected[0][0]
        with owner.runtime.open_connection() as connection:
            revision = connection.execute('SELECT context_revision FROM atom_search.project_context_state').fetchone()[0]
        owner.close()
        assert owner.runtime.status().state == 'stopped'
        assert json.loads(budget.ledger.read_bytes()) == []
        owner = open_service_owner(profile, missing, automatic_catalog=False)
        owner.start()
        assert context_rows() == expected
        assert owner.core.index_context_batch()['attempted'] == 0
        with owner.runtime.open_connection() as connection:
            assert connection.execute('SELECT context_revision FROM atom_search.project_context_state').fetchone()[0] == revision
    finally:
        owner.close()
        assert owner.runtime.status().state == 'stopped'
        assert owner.runtime._claimed_process() is None
