"""Queue ordering, honest progress, scoped cancellation and owned boundaries."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from atom_search_core.models import DirectorySnapshot, FileMetadata
from atom_search_core.service import SearchService
from atom_search_core.source import FileSystemSource, canonical_root
from atom_search.service.contracts import SettingsSnapshot, ServiceError


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), 'queue did not reach the required state'


class Model:
    model_id, dimension = 'queue-test', 3

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []

    def encode(self, texts, *, kind='passage'):
        self.calls.append((kind, tuple(texts)))
        if kind == 'passage':
            self.entered.set()
            assert self.release.wait(8)
        return [(1., 0., 0.) for _ in texts]


def subject(tmp_path, *, source=None, model=None, automatic=False, clock=None):
    module = importlib.import_module('atom_search.service.jobs')
    settings = dict(revision=1, indexing={'interval_seconds': 86400}, sources=[])
    for name in ('a', 'b', 'c'):
        root = tmp_path / name
        root.mkdir()
        for index in range(20):
            (root / f'drawing-{name}-{index}.pdf').touch()
        settings['sources'].append(dict(id=str(uuid.uuid4()), path=str(root), enabled=True))
    core = SearchService(tmp_path / 'catalog.sqlite', source=source, embedder=model)
    for item in settings['sources']:
        core.catalog.begin_root(canonical_root(item['path']))
    owner = SimpleNamespace(core=core, profile_dir=tmp_path / 'profile', generation='client-test')
    owner.repository = SimpleNamespace(snapshot=lambda: SettingsSnapshot(1, settings['revision'], deepcopy(settings), ()))
    owner.index_generation = lambda: 0
    @contextmanager
    def operation(cancel=None):
        yield
    owner.operation = operation
    class Registry:
        def resolve(self, ids, *, allowed):
            from atom_search_core.models import SearchScope
            roots = [item['path'] for item in settings['sources'] if item['id'] in ids]
            return SearchScope(tuple(value for root in roots for value in core.catalog.resolve_scope(root).source_ids))
        def owned_exclusions(self):
            return ()
    owner.registry = Registry()
    options = {} if clock is None else {'clock': clock}
    scheduler = module.CatalogScheduler(owner, automatic=automatic, jitter=lambda: 0, **options)
    owner.scheduler = scheduler
    return settings, core, scheduler


def test_all_sources_catalog_before_any_embedding_and_search_survives_model_block(tmp_path):
    model = Model()
    settings, core, queue = subject(tmp_path, model=model)
    try:
        ids = queue.start_catalog(tuple(item['id'] for item in settings['sources'][:2]))
        assert model.entered.wait(3)
        assert all(job.state == 'completed' for job in queue.snapshot() if job.job_id in ids)
        assert core.catalog.status(scope=core.catalog.resolve_scope(None))['files'] == 40
        before = time.monotonic()
        assert len(core.search('drawing', root=settings['sources'][1]['path'])) == 20
        assert time.monotonic() - before < .3
        assert all(len(texts) <= 16 for kind, texts in model.calls if kind == 'passage')
    finally:
        model.release.set()
        queue.close()
        core.close()


def test_metadata_context_resumes_on_existing_queue_without_new_source_scan(tmp_path, monkeypatch):
    settings, core, original = subject(tmp_path)
    original.close()
    owner = original.owner
    core.projection = object()
    calls = []
    def no_crawl(*args, **kwargs):
        raise AssertionError('context-only work enumerated the source')
    monkeypatch.setattr(core, 'scan', no_crawl)
    remaining = {1: True, 2: True, 3: True}
    def batch(*, scope, cancel, publish_if):
        assert publish_if() and not cancel.is_set()
        calls.append(scope.source_ids)
        attempt = int(remaining.pop(scope.source_ids[0], False))
        return dict(indexed=0, attempted=0, context_attempted=attempt, cancelled=False)
    monkeypatch.setattr(core, 'index_embedding_batch', batch)
    from atom_search.service.jobs import CatalogScheduler
    # No timer is started. This is the actual automatic context queue, with its
    # shared inference worker; ordinary timer/daily policy has separate coverage.
    queue = CatalogScheduler(owner, automatic=True, autostart=False)
    queue._threads = queue._threads[:2]
    try:
        assert len(queue.snapshot()) == 3 and {job.phase for job in queue.snapshot()} == {'embed'}
        queue.queue_context()
        assert len(queue.snapshot()) == 3
        queue.start()
        wait_for(lambda: all(job.state == 'completed' for job in queue.snapshot()))
        assert len(calls) == 6 and set(calls) == {(1,), (2,), (3,)}
        assert all(job.total is None and job.processed == job.succeeded == 0 for job in queue.snapshot())
    finally:
        queue.close()
        core.close()


def test_new_catalog_preempts_next_embedding_batch_and_manual_daily_deduplicate(tmp_path):
    model = Model()
    settings, core, queue = subject(tmp_path, model=model)
    try:
        a, b, c = (item['id'] for item in settings['sources'])
        queue.start_catalog((a, b))
        assert model.entered.wait(3)
        created = queue.start_catalog((c,))
        assert queue.start_catalog((c,)) == created
        queue.tick(datetime.now(timezone.utc) + timedelta(days=100))
        assert len([job for job in queue.snapshot() if job.source_id == c and job.phase == 'catalog']) == 1
        wait_for(lambda: any(job.job_id == created[0] and job.state == 'completed' for job in queue.snapshot()))
        assert core.catalog.status(scope=core.catalog.resolve_scope(settings['sources'][2]['path']))['files'] == 20
    finally:
        model.release.set()
        queue.close()
        core.close()


def test_partial_counters_unknown_total_and_scoped_cancel_preserve_unvisited(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Source:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            yield DirectorySnapshot('', (FileMetadata('kept.pdf', 'kept.pdf', 0, 0),),
                protected_names=('denied',), complete=False,
                errors=({'path': 'denied', 'operation': 'enumerate', 'message': 'denied'},), skipped_links=1)
            entered.set()
            release.wait(3)
    settings, core, queue = subject(tmp_path, source=Source())
    try:
        a, b = (item['id'] for item in settings['sources'][:2])
        ids = queue.start_catalog((a, b))
        assert entered.wait(2)
        queue.cancel(ids[1])
        release.set()
        wait_for(lambda: all(job.state not in ('queued', 'running') for job in queue.snapshot()))
        first = next(job for job in queue.snapshot() if job.job_id == ids[0])
        second = next(job for job in queue.snapshot() if job.job_id == ids[1])
        assert (first.state, first.total) == ('partial', None)
        assert (first.processed, first.succeeded, first.skipped, first.errors) == (3, 1, 1, 1)
        assert second.state == 'cancelled'
    finally:
        release.set()
        queue.close()
        core.close()


def test_disabled_source_rejects_late_embedding_and_old_completion(tmp_path):
    model = Model()
    settings, core, queue = subject(tmp_path, model=model)
    try:
        identity = settings['sources'][0]['id']
        queue.start_catalog((identity,))
        assert model.entered.wait(2)
        settings['sources'][0]['enabled'] = False
        settings['revision'] += 1
        queue.apply_settings(SettingsSnapshot(1, 2, deepcopy(settings), ()))
        model.release.set()
        wait_for(lambda: all(job.state not in ('queued', 'running') for job in queue.snapshot()))
        with core.catalog.store.connection() as connection:
            assert connection.execute('SELECT count(*) FROM files WHERE embedding IS NOT NULL').fetchone()[0] == 0
        assert any(job.phase == 'embed' and job.state == 'cancelled' for job in queue.snapshot())
    finally:
        model.release.set()
        queue.close()
        core.close()


@pytest.mark.parametrize('phase', ['catalog', 'embed'])
def test_compatible_settings_apply_reschedules_pending_work_without_waiting_a_day(tmp_path, phase):
    entered, release = threading.Event(), threading.Event()
    class Source:
        calls = 0
        def snapshots(self, root, cancel, *, excluded_roots=()):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(5)
            yield DirectorySnapshot('', (FileMetadata('kept.pdf', 'kept.pdf', 1, 1),))
    model = Model() if phase == 'embed' else None
    settings, core, queue = subject(tmp_path, source=Source() if phase == 'catalog' else None, model=model)
    identity = settings['sources'][0]['id']
    try:
        original = queue.start_catalog((identity,))
        assert (model.entered if model else entered).wait(3)
        old_jobs = {job.job_id for job in queue.snapshot()}
        settings['revision'] = 2
        settings['projects'] = []  # Compatible owner-only format/settings change.
        queue.apply_settings(SettingsSnapshot(2, 2, deepcopy(settings), ()))
        release.set()
        if model:
            model.release.set()
        wait_for(lambda: any(job.source_id == identity and job.job_id not in old_jobs
                            and job.phase == phase and job.state == 'completed'
                            for job in queue.snapshot()))
        if model:
            with core.catalog.store.connection() as connection:
                assert connection.execute('SELECT count(*) FROM files WHERE embedding IS NOT NULL').fetchone()[0] == 20
        else:
            assert core.catalog.status(scope=core.catalog.resolve_scope(None))['files'] == 1
        assert any(job.job_id in old_jobs and job.state == 'cancelled' for job in queue.snapshot())
    finally:
        release.set()
        if model:
            model.release.set()
        queue.close()
        core.close()


@pytest.mark.parametrize('phase, denied, explicit_cancel', [
    ('catalog', False, False), ('embed', False, False),
    ('catalog', True, False), ('embed', True, False), ('catalog', False, True),
])
def test_owner_publication_handoff_keeps_compatible_work(tmp_path, monkeypatch, phase, denied, explicit_cancel):
    """Real owner/coordinator/file publication, controlled non-PG worker ports."""
    from atom_search.service.owner import open_service_owner
    from atom_search.service.jobs import CatalogScheduler
    from atom_search.settings import repository as repository_module
    entered, release = threading.Event(), threading.Event()
    class Source(FileSystemSource):
        def snapshots(self, root, cancel, *, excluded_roots=()):
            entered.set()
            assert release.wait(8)
            yield from super().snapshots(root, cancel, excluded_roots=excluded_roots)
    model = Model() if phase == 'embed' else None
    settings, core, original = subject(tmp_path, source=Source() if phase == 'catalog' else None, model=model)
    original.close()
    owner = open_service_owner(tmp_path / 'profile', tmp_path / 'missing', automatic_catalog=False)
    facade = owner.facade(owner.owner_access)
    current = facade.settings_get()
    selected = current.settings
    selected['sources'] = [dict(source, label='') for source in settings['sources']]
    current = facade.settings_apply(selected, current.revision)
    # Keep the actual owner/coordinator and guarded repository; only the worker
    # catalogue port is the same small SQLite port used by these queue tests.
    owner.core, owner.registry = core, original.owner.registry
    owner.index_generation = original.owner.index_generation
    owner._bound_sources = deepcopy(current.settings['sources'])
    queue = owner.scheduler = CatalogScheduler(owner, automatic=False)
    identity = settings['sources'][0]['id']
    try:
        queue.start_catalog((identity,))
        assert (model.entered if model else entered).wait(3)
        old_ids = {job.job_id for job in queue.snapshot()}
        active = next(job for job in queue.snapshot() if job.phase == phase and job.state == 'running')
        before = owner.repository.path.read_bytes()
        atomic = repository_module.atomic_write_json
        observed = []
        def interleaved(path, value):
            if path == owner.repository.path and value['revision'] == current.revision + 1:
                with pytest.raises(ServiceError) as failure:
                    facade.settings_get()
                assert failure.value.code == 'settings_recovery_required'
                if explicit_cancel:
                    queue.cancel(active.job_id)
                release.set()
                if model:
                    model.release.set()
                wait_for(lambda: any(job.job_id == active.job_id and job.state == 'cancelled'
                                    for job in queue.snapshot()))
                observed.append('terminal-while-repository-pending')
                if denied:
                    raise PermissionError('controlled settings publication denial')
            return atomic(path, value)
        candidate = deepcopy(current.settings)
        candidate['projects'] = [dict(id=str(uuid.uuid4()), canonical_name='Owner project',
                                     source_ids=[identity], bindings=[])]
        with monkeypatch.context() as patch:
            patch.setattr(repository_module, 'atomic_write_json', interleaved)
            if denied:
                with pytest.raises(PermissionError):
                    facade.settings_apply(candidate, current.revision)
            else:
                assert facade.settings_apply(candidate, current.revision).revision == current.revision + 1
        assert observed == ['terminal-while-repository-pending']
        if denied:
            assert owner.repository.path.read_bytes() == before
            assert facade.settings_get() == current
        if explicit_cancel:
            assert queue.quiesce(3)
            queue.resume()
            assert not any(job.job_id not in old_ids for job in queue.snapshot())
        else:
            wait_for(lambda: any(job.job_id not in old_ids and job.phase == phase
                                and job.state == 'completed' for job in queue.snapshot()))
            with core.catalog.store.connection() as connection:
                column = 'embedding IS NOT NULL' if model else '1=1'
                assert connection.execute('SELECT count(*) FROM files WHERE ' + column).fetchone()[0] == 20
    finally:
        release.set()
        if model:
            model.release.set()
        owner.close()


@pytest.mark.parametrize('denied', [False, True])
def test_due_tick_retries_when_public_settings_publication_prevents_admission(tmp_path, monkeypatch, denied):
    from atom_search.service.owner import open_service_owner
    from atom_search.service.jobs import CatalogScheduler
    from atom_search.settings import repository as repository_module
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    settings, core, original = subject(tmp_path)
    original.close()
    owner = open_service_owner(tmp_path / 'profile', tmp_path / 'missing', automatic_catalog=False)
    facade = owner.facade(owner.owner_access)
    current = facade.settings_get()
    selected = current.settings
    selected['sources'] = [dict(settings['sources'][0], label='')]
    current = facade.settings_apply(selected, current.revision)
    owner.core, owner.registry = core, original.owner.registry
    owner.index_generation = original.owner.index_generation
    owner._bound_sources = deepcopy(current.settings['sources'])
    queue = owner.scheduler = CatalogScheduler(owner, automatic=False, clock=lambda: now)
    selected_due, publishing, attempted = (threading.Event() for _ in range(3))
    results, failures = [], []
    candidate = deepcopy(current.settings)
    candidate['ui']['theme'] = 'dark'
    atomic, start = repository_module.atomic_write_json, queue.start_catalog

    def publication(path, value):
        if path == owner.repository.path and value['revision'] == current.revision + 1:
            # The real coordinator is pending and has no active intent to save.
            with pytest.raises(ServiceError) as failure:
                facade.settings_get()
            assert failure.value.code == 'settings_recovery_required'
            assert queue.snapshot() == ()
            publishing.set()
            assert attempted.wait(5)
            if denied:
                raise PermissionError('controlled settings publication denial')
        return atomic(path, value)

    def admission(ids):
        selected_due.set()
        assert publishing.wait(5)
        try:
            return start(ids)
        finally:
            attempted.set()

    def apply():
        try:
            assert selected_due.wait(5)
            results.append(facade.settings_apply(candidate, current.revision))
        except BaseException as failure:
            failures.append(failure)

    thread = threading.Thread(target=apply)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(repository_module, 'atomic_write_json', publication)
            patch.setattr(queue, 'start_catalog', admission)
            thread.start()
            with pytest.raises(ServiceError) as failure:
                queue.tick(now + timedelta(seconds=60))
            assert failure.value.code == 'settings_busy'
            thread.join(10)
            assert not thread.is_alive()
        if denied:
            assert len(failures) == 1 and isinstance(failures[0], PermissionError)
            assert facade.settings_get() == current
        else:
            assert not failures and results[0].revision == current.revision + 1
        # The next timer observation, at the same time, must admit one wave.
        # Rejected admission must not defer the source for a whole interval.
        queue.tick(now + timedelta(seconds=60))
        wait_for(lambda: any(job.state == 'completed' for job in queue.snapshot()))
        jobs = queue.snapshot()
        assert len(jobs) == 1 and jobs[0].succeeded == 20
        queue.tick(now + timedelta(seconds=60))
        assert queue.snapshot() == jobs
    finally:
        attempted.set()
        if thread.ident is not None:
            thread.join()
        owner.close()


def test_owned_subtrees_are_not_descended_and_prefix_neighbor_is_kept(tmp_path, monkeypatch):
    root = tmp_path / 'parent'
    root.mkdir()
    excluded = []
    for name in ('profile', 'demo', 'cache', 'app', 'cache-other'):
        directory = root / name
        directory.mkdir()
        (directory / 'file.pdf').touch()
        if name != 'cache-other':
            excluded.append(directory)
    original = __import__('os').scandir
    def guarded(path):
        assert canonical_root(str(path)) not in {canonical_root(str(item)) for item in excluded}
        return original(path)
    monkeypatch.setattr('os.scandir', guarded)
    snapshots = list(FileSystemSource().snapshots(str(root), threading.Event(), excluded_roots=tuple(excluded)))
    assert [item.relative_path for item in snapshots] == ['', 'cache-other']
    assert snapshots[1].files[0].relative_path == 'cache-other/file.pdf'


def test_direct_owned_root_is_invalid_before_settings_commit_and_registration_is_host_only(tmp_path):
    from atom_search.service.sources import SourceRegistry
    from atom_search.service.mutations import MutationCoordinator
    from atom_search.settings.repository import SettingsRepository
    from test_source_registry import MemoryIdentities
    repo = SettingsRepository(tmp_path / 'profile')
    original = repo.load()
    registry = SourceRegistry(repo, MemoryIdentities(), owner_id='host')
    registry.register_owned_exclusion(tmp_path / 'cache', owner_id='host')
    with pytest.raises(ServiceError):
        registry.register_owned_exclusion(tmp_path / 'neighbor', owner_id='renderer')
    candidate = deepcopy(original.settings)
    candidate['sources'] = [dict(id=str(uuid.uuid4()), path=str(tmp_path / 'cache' / 'child'),
                                 label='', enabled=True)]
    coordinator = MutationCoordinator(repo, validate_sources=registry.validate_sources)
    validated = coordinator.validate(candidate, original.revision)
    assert not validated.valid and validated.errors[0].code == 'owned_source_root'
    assert repo.snapshot().revision == original.revision
    assert repo.snapshot().settings == original.settings


def test_interval_while_alive_and_sleep_has_one_wave_and_unavailable_backoff(tmp_path):
    now = [datetime(2026, 10, 24, tzinfo=timezone.utc)]
    calls = []
    class Source:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            calls.append(root)
            raise FileNotFoundError('fictional unavailable source')
            yield  # generator-shaped Source contract
    settings, core, queue = subject(tmp_path, source=Source(), clock=lambda: now[0])
    identity = settings['sources'][0]['id']
    try:
        queue.start_catalog((identity,))
        wait_for(lambda: len(queue.snapshot()) == 1 and queue.snapshot()[0].state == 'failed')
        # Keep other sources outside this scenario through validated snapshot.
        settings['sources'][1]['enabled'] = settings['sources'][2]['enabled'] = False
        queue.apply_settings(SettingsSnapshot(1, 1, deepcopy(settings), ()))
        for delay in (1, 2, 4, 8, 16, 32, 60, 60):
            prior = len(calls)
            now[0] += timedelta(seconds=delay - .1)
            queue.tick(now[0])
            assert len(calls) == prior
            now[0] += timedelta(seconds=.1)
            queue.tick(now[0])
            wait_for(lambda: len(calls) == prior + 1 and all(job.state == 'failed' for job in queue.snapshot()))
        # An explicit manual refresh is not held behind a daily/backoff deadline.
        prior = len(calls)
        queue.start_catalog((identity,))
        wait_for(lambda: len(calls) == prior + 1 and all(job.state == 'failed' for job in queue.snapshot()))
        now[0] += timedelta(days=100)
        queue.tick(now[0])
        wait_for(lambda: len(calls) == prior + 2 and all(job.state == 'failed' for job in queue.snapshot()))
        queue.tick(now[0])
        assert len(calls) == prior + 2
    finally:
        queue.close()
        core.close()


def test_unknown_job_has_stable_public_error(tmp_path):
    settings, core, queue = subject(tmp_path)
    try:
        with pytest.raises(ServiceError) as failure:
            queue.cancel(str(uuid.uuid4()))
        assert failure.value.code == 'job_not_found' and failure.value.status_code == 404
    finally:
        queue.close()
        core.close()


def test_failed_catalog_counts_error_without_claiming_success(tmp_path):
    class Source:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            raise ValueError('controlled metadata failure')
            yield
    settings, core, queue = subject(tmp_path, source=Source())
    try:
        identities = queue.start_catalog((settings['sources'][0]['id'],))
        wait_for(lambda: queue.snapshot()[0].state == 'failed')
        job = queue.snapshot()[0]
        assert (job.processed, job.succeeded, job.skipped, job.errors, job.total) == (1, 0, 0, 1, None)
        assert queue.wait_catalog(identities[0])['status'] == 'unavailable'
    finally:
        queue.close()
        core.close()


def test_desktop_cancel_interrupts_neighbor_wait_without_cancelling_neighbor(tmp_path):
    from atom_search.service.desktop import DesktopView
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    neighbor_cancel = []
    class Source:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            if root.endswith('b'):
                neighbor_cancel.append(cancel)
                entered.set()
                assert release.wait(5)
            yield DirectorySnapshot('')
    settings, core, queue = subject(tmp_path, source=Source())
    settings['sources'][2]['enabled'] = False
    queue.apply_settings(SettingsSnapshot(1, 1, deepcopy(settings), ()))
    cancel, reports = threading.Event(), []
    def run():
        try:
            reports.append(DesktopView(queue.owner).scan(settings['sources'][0]['path'], cancel=cancel))
        finally:
            done.set()
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(3)
        cancel.set()
        assert done.wait(.5), 'caller still waits on the neighboring catalog'
        assert reports[0]['status'] == 'cancelled'
        assert not neighbor_cancel[0].is_set(), 'caller cancellation escaped its source'
    finally:
        release.set()
        worker.join(5)
        queue.close()
        core.close()


def test_scheduler_can_be_closed_before_starting_workers(tmp_path):
    from atom_search.service.jobs import CatalogScheduler
    settings, core, original = subject(tmp_path)
    original.close()
    queue = None
    try:
        queue = CatalogScheduler(original.owner, automatic=False, autostart=False)
        queue.start_catalog((settings['sources'][0]['id'],))
        time.sleep(.03)
        assert queue.snapshot()[0].state == 'queued'
        queue.close()
    finally:
        if queue is not None:
            queue.close()
        core.close()


def test_quiesce_waits_for_the_terminal_catalog_schedule_write(tmp_path, monkeypatch):
    from atom_search.service.jobs import CatalogScheduler
    from atom_search.settings.repository import SettingsRepository
    settings, core, original = subject(tmp_path)
    original.close()
    owner = original.owner
    repo = SettingsRepository(owner.profile_dir)
    before = repo.load()
    candidate = before.settings
    candidate['sources'] = [dict(item, label='') for item in settings['sources']]
    repo.commit(candidate, before.revision)
    owner.repository = repo
    entered, release = threading.Event(), threading.Event()
    queue = CatalogScheduler(owner, automatic=False)
    mutation = repo.mutation
    @contextmanager
    def blocked_terminal_write():
        with mutation():
            if queue.snapshot() and all(job.state == 'completed' for job in queue.snapshot()):
                entered.set()
                assert release.wait(5)
            yield
    monkeypatch.setattr(repo, 'mutation', blocked_terminal_write)
    try:
        queue.start_catalog((settings['sources'][0]['id'],))
        assert entered.wait(3)
        assert queue.snapshot()[0].state == 'completed'
        assert not queue.quiesce(timeout=.05), 'terminal visibility is not writer drain'
        release.set()
        assert queue.quiesce(timeout=3)
    finally:
        release.set()
        queue.resume()
        queue.close()
        core.close()


def test_maximum_valid_sources_produce_readable_bounded_pending_cache(tmp_path):
    import json
    from atom_search.settings.repository import SettingsRepository
    from atom_search.service.jobs import CatalogScheduler
    from atom_search_core.models import SearchScope
    entered = threading.Event()
    class Source:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            entered.set()
            assert cancel.wait(5)
            yield DirectorySnapshot('', complete=False)
    settings, core, original = subject(tmp_path, source=Source())
    original.close()
    owner = original.owner
    repo = SettingsRepository(owner.profile_dir)
    snapshot = repo.load()
    candidate = snapshot.settings
    candidate['sources'] = [dict(id=str(uuid.uuid4()), label='', enabled=True,
        path=f'C:/fictional-{index}/' + 'я' * 400) for index in range(64)]
    assert 56000 < len(json.dumps(candidate, ensure_ascii=False).encode()) < 65536
    repo.commit(candidate, snapshot.revision)
    owner.repository = repo
    mapping = {item['id']: index + 1 for index, item in enumerate(candidate['sources'])}
    owner.registry.resolve = lambda ids, allowed: SearchScope(tuple(mapping[identity] for identity in ids))
    queue = CatalogScheduler(owner, automatic=False)
    restarted = None
    try:
        queue.start_catalog(tuple(mapping))
        assert entered.wait(3)
        path = repo.runtime_dir / 'catalog-schedule.json'
        content = path.read_bytes()
        assert len(content) <= 65536, 'writer exceeded its own reader byte limit'
        crashed = json.loads(content)
        assert len(crashed['sources']) == 64 and all(item['pending'] for item in crashed['sources'].values())
        queue.close()
        from atom_search.runtime import _windows
        _windows.atomic_json(path, crashed)
        restarted = CatalogScheduler(owner, automatic=False)
        assert restarted.snapshot() == ()
    finally:
        queue.close()
        if restarted is not None:
            restarted.close()
        core.close()


def test_skipped_link_coverage_is_partial_and_preserves_catalog(tmp_path):
    class Source:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            yield DirectorySnapshot('', protected_names=('linked',), skipped_links=1)
    core = SearchService(tmp_path / 'catalog.sqlite', source=Source())
    root = canonical_root(str(tmp_path / 'documents'))
    identifier = core.catalog.begin_root(root)
    core.catalog.reconcile(identifier, DirectorySnapshot('', directories=('linked',)))
    core.catalog.reconcile(identifier, DirectorySnapshot('linked', (FileMetadata('linked/kept.pdf', 'kept.pdf', 0, 0),)))
    core.catalog.finish_root(identifier, 'complete')
    before = core.status(root=root)['last_success']
    try:
        report = core.scan(root)
        assert report['status'] == 'partial'
        status = core.status(root=root)
        assert status['files'] == 1 and status['last_success'] == before and not status['metadata_ready']
    finally:
        core.close()


@pytest.mark.parametrize('fault', [None, 'source_generation', 'settings_revision', 'root_digest', 'source_ids', 'catalog_generation'])
def test_restart_rechecks_pending_token_without_restoring_unwritten_counters(tmp_path, fault):
    import json
    from atom_search.runtime import _windows
    from atom_search.settings.repository import SettingsRepository
    from atom_search.service.jobs import CatalogScheduler
    now = [datetime(2026, 10, 24, tzinfo=timezone.utc)]
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Source:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            calls.append(root)
            if root.endswith('a'):
                raise FileNotFoundError('fictional unavailable source')
            entered.set()
            assert release.wait(5)
            if not cancel.is_set():
                yield DirectorySnapshot('', (FileMetadata('saved.pdf', 'saved.pdf', 0, 0),))
    settings, core, queue = subject(tmp_path, source=Source(), clock=lambda: now[0])
    owner = queue.owner
    queue.close()
    repo = SettingsRepository(owner.profile_dir)
    original = repo.load()
    candidate = original.settings
    candidate['sources'] = [dict(item, label='') for item in settings['sources'][:2]]
    repo.commit(candidate, original.revision)
    owner.repository = repo
    a, b = (item['id'] for item in candidate['sources'])
    queue = CatalogScheduler(owner, automatic=False, clock=lambda: now[0], jitter=lambda: 0)
    restarted = None
    try:
        queue.start_catalog((a, b))
        assert entered.wait(3), queue.snapshot()
        # Capture exactly the durable state that a crash while B is blocked
        # would leave, then clean up this test's real threads cooperatively.
        path = repo.runtime_dir / 'catalog-schedule.json'
        crashed = json.loads(path.read_bytes())
        for job in queue.snapshot():
            if job.source_id == b:
                queue.cancel(job.job_id)
        release.set()
        queue.close()
        if fault is not None:
            pending = crashed['sources'][b]['pending']
            pending[fault] = ({'root_digest': 'f' * 64, 'source_ids': [999], 'catalog_generation': 999}.get(
                fault, pending[fault] + 1 if type(pending[fault]) is int else None))
        _windows.atomic_json(path, crashed)
        now[0] += timedelta(seconds=61)
        restarted = CatalogScheduler(owner, automatic=False, clock=lambda: now[0], jitter=lambda: 0)
        assert restarted.snapshot() == ()
        now[0] += timedelta(seconds=61)
        restarted.tick(now[0])
        wait_for(lambda: all(job.state not in ('queued', 'running') for job in restarted.snapshot()))
        jobs = {job.source_id: job for job in restarted.snapshot()}
        assert set(jobs) == ({a, b} if fault is None else {a}), jobs
        if fault is None:
            assert jobs[b].succeeded == jobs[b].processed == 1 and jobs[b].total is None
        assert jobs[a].errors == 1 and jobs[a].succeeded == 0
    finally:
        release.set()
        queue.close()
        if restarted is not None:
            restarted.close()
        core.close()


def test_empty_sources_admit_no_automatic_catalog_or_model_jobs(tmp_path, monkeypatch):
    settings, core, queue = subject(tmp_path, automatic=False)
    try:
        settings['sources'] = []
        settings['revision'] += 1
        queue.apply_settings(queue.owner.repository.snapshot())
        monkeypatch.setattr(core, 'scan', lambda *a, **k: pytest.fail('empty profile scanned'))
        monkeypatch.setattr(core, 'index_embeddings', lambda *a, **k: pytest.fail('empty profile embedded'))
        queue.tick(datetime.now(timezone.utc) + timedelta(days=365))
        assert queue.snapshot() == ()
    finally:
        queue.close()
        core.close()
