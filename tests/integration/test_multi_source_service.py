"""Real PostgreSQL queue safety, including cancellation and incomplete trees."""
from pathlib import Path
import threading
import time
import uuid

import pytest

from atom_search.service.contracts import SearchRequest
from atom_search_core.source import canonical_root


def wait_for(predicate, seconds=90, describe=lambda: ''):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), describe()


@pytest.fixture
def multi_owner(tmp_path):
    from atom_search.service.owner import open_service_owner
    owner = open_service_owner(tmp_path / 'profile', tmp_path / 'missing-model', automatic_catalog=False)
    sources = []
    for label in ('a', 'b'):
        root = tmp_path / label
        root.mkdir()
        (root / f'drawing-{label}.pdf').touch()
        (root / 'unvisited').mkdir()
        (root / 'unvisited' / f'preserved-{label}.pdf').touch()
        sources.append(dict(id=str(uuid.uuid4()), label=label, path=str(root), enabled=True))
    initial = owner.repository.snapshot()
    candidate = initial.settings
    candidate['sources'] = sources
    owner.coordinator.owner_apply(candidate, initial.revision)
    owner.start()
    try:
        yield owner, sources
    finally:
        owner.close()


def test_pg_cancel_and_partial_preserve_rows_and_public_ids(multi_owner, monkeypatch):
    owner, sources = multi_owner
    facade = owner.facade(owner.owner_access)
    ids = facade.start_catalog()
    wait_for(lambda: all(job.state not in ('running', 'queued') for job in owner.scheduler.snapshot() if job.job_id in ids),
             describe=lambda: [(job.phase, job.state, job.succeeded, job.errors) for job in owner.scheduler.snapshot()])
    assert all(job.state == 'completed' for job in owner.scheduler.snapshot() if job.job_id in ids), owner.scheduler.snapshot()
    before = {item.full_path: item.result_id for item in facade.search(SearchRequest('preserved')).results}
    assert len(before) == 2
    entered, release = threading.Event(), threading.Event()
    original = owner.core.catalog.reconcile
    def blocked(source, snapshot):
        result = original(source, snapshot)
        if snapshot.relative_path == '':
            entered.set()
            assert release.wait(30)
        return result
    monkeypatch.setattr(owner.core.catalog, 'reconcile', blocked)
    jobs = facade.start_catalog((sources[0]['id'],))
    assert entered.wait(30)
    facade.cancel_job(jobs[0])
    release.set()
    wait_for(lambda: any(job.job_id == jobs[0] and job.state == 'cancelled' for job in owner.scheduler.snapshot()))
    monkeypatch.setattr(owner.core.catalog, 'reconcile', original)
    import os
    original_scandir = os.scandir
    def denied(path):
        if canonical_root(str(path)).removeprefix('\\\\?\\') == canonical_root(str(Path(sources[1]['path']) / 'unvisited')):
            raise PermissionError('controlled metadata access denied')
        return original_scandir(path)
    monkeypatch.setattr(os, 'scandir', denied)
    partial = facade.start_catalog((sources[1]['id'],))
    wait_for(lambda: any(job.job_id == partial[0] and job.state == 'partial' for job in owner.scheduler.snapshot()))
    after = facade.search(SearchRequest('preserved')).results
    assert {item.full_path: item.result_id for item in after} == before
    assert all(item.stale for item in after)
    state = next(job for job in owner.scheduler.snapshot() if job.job_id == partial[0])
    assert state.total is None and state.errors == 1 and state.processed == state.succeeded + state.skipped + state.errors
    for source in sources:
        scoped = facade.search(SearchRequest('drawing', source_ids=(source['id'],), limit=1))
        assert len(scoped.results) == 1 and scoped.results[0].source_id == source['id']


def test_pg_source_scoped_batch_before_limit_and_generation_cas(pg_catalog, pg_connections):
    from atom_search_core.models import DirectorySnapshot, FileMetadata, SearchScope
    from atom_search_core.service import SearchService
    from atom_search_core.storage.postgres_retrieval import PostgresRetrieval
    KEY = 'fixture-model|filename-parent-v1'
    a, b = pg_catalog.begin_root('C:/queue-a'), pg_catalog.begin_root('C:/queue-b')
    pg_catalog.reconcile(a, DirectorySnapshot('', tuple(FileMetadata(f'a-{i}.pdf', f'a-{i}.pdf', 0, 0) for i in range(40))))
    pg_catalog.reconcile(b, DirectorySnapshot('', (FileMetadata('b.pdf', 'b.pdf', 0, 0),)))
    entered, release = threading.Event(), threading.Event()
    class Embedder:
        model_id, dimension = KEY.split('|')[0], 384
        def encode(self, texts, *, kind='passage'):
            entered.set()
            assert release.wait(30)
            return [(1.,) + (0.,) * 383 for _ in texts]
    core = SearchService(catalog=pg_catalog, retrieval=PostgresRetrieval(pg_connections['application']), embedder=Embedder())
    result = []
    worker = threading.Thread(target=lambda: result.append(core.index_embedding_batch(scope=SearchScope((b,)))))
    worker.start()
    try:
        assert entered.wait(30)
        # Real SQL revision invalidation while inference is in flight.
        pg_catalog.reconcile(b, DirectorySnapshot('', (FileMetadata('b.pdf', 'b.pdf', 99, 1),)))
        release.set()
        worker.join(5)
        assert result == [{'indexed': 0, 'attempted': 1, 'cancelled': False}]
        assert len(pg_catalog.pending_embeddings(KEY, 384, 16, scope=SearchScope((a,)))) == 16
        assert pg_catalog.pending_embeddings(KEY, 384, 16, scope=SearchScope(())) == []
        assert pg_catalog.pending_embeddings(KEY, 384, 16, scope=SearchScope((b,)))[0].file.size == 99
    finally:
        release.set()
        worker.join(5)


def test_pg_cached_health_counts_exclude_incompatible_vectors_and_status_is_memory_only(pg_catalog, pg_connections, pg_runtime, monkeypatch):
    """Real persisted SQL rows, with no source enumeration or status-time SQL."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from contextlib import nullcontext
    from atom_search.service.contracts import AccessContext, SettingsSnapshot
    from atom_search.service.health import HealthCache, database_probe
    from atom_search.service.application import AtomSearchService
    from atom_search_core.models import DirectorySnapshot, FileMetadata
    from atom_search_core.service import SearchService
    from atom_search_core.storage.postgres_retrieval import PostgresRetrieval
    from atom_search_core.storage.public_ids import PostgresPublicIds
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    roots = ('C:/fictional-health-a', 'C:/fictional-health-b')
    public = PostgresPublicIds(pg_catalog)
    public.bind_many(((a, roots[0]), (b, roots[1])))
    for root in roots:
        identity = pg_catalog.begin_root(root)
        pg_catalog.reconcile(identity, DirectorySnapshot('', tuple(FileMetadata(f'drawing-{i}.pdf', f'drawing-{i}.pdf', 0, 0) for i in range(3))))
        pg_catalog.finish_root(identity, 'complete')
    class Model:
        model_id, dimension = 'fixture-model', 384
        def encode(self, texts, *, kind='passage'):
            return [(1.,) + (0.,) * 383 for _ in texts]
    core = SearchService(catalog=pg_catalog, retrieval=PostgresRetrieval(pg_connections['application']), embedder=Model())
    core.index_embedding_batch(scope=pg_catalog.resolve_scope(roots[0]))
    # A vector with the right revision but wrong input is not compatible.
    with pg_connections['application']() as connection:
        connection.execute("UPDATE atom_search.file_embeddings SET input_hash=%s WHERE file_id=(SELECT min(file_id) FROM atom_search.file_embeddings)", ('b' * 64,))
    health = HealthCache()
    health.configure(SettingsSnapshot(1, 4, {'sources': [dict(id=a, path=roots[0], enabled=True), dict(id=b, path=roots[1], enabled=True)]}, ()))
    owner = SimpleNamespace(health=health, core=core, runtime=pg_runtime[0], operation=lambda: nullcontext())
    result = database_probe(owner, time.monotonic() + 2, threading.Event())
    assert result['counts']['counts'] == {a: (3, 2), b: (3, 0)}
    checked = datetime.now(timezone.utc)
    for component, value in result.items():
        if component == 'counts':
            health.record_counts(**value, checked_at=checked)
        elif component == 'project_support':
            health.record_project_support(**value, checked_at=checked)
        else:
            health.record(component, value, checked_at=checked)
    def forbidden(*args, **kwargs):
        raise AssertionError('cached status performed I/O')
    reader = AtomSearchService(owner, AccessContext('reader-b', frozenset({'status'}), (b,)))
    with monkeypatch.context() as patch:
        patch.setattr(owner.runtime, 'status', forbidden)
        patch.setattr(owner.runtime, 'open_connection', forbidden)
        patch.setattr(owner.runtime, 'open_health_connection', forbidden)
        patch.setattr('builtins.open', forbidden)
        patch.setattr('pathlib.Path.stat', forbidden)
        values = [reader.status() for _ in range(20)]
    assert all(value.components.sources[0].source_id == b and value.components.sources[0].catalogued_files == 3 for value in values)
    core._embedder = None
    assert database_probe(owner, time.monotonic() + 2, threading.Event())['counts']['counts'][a] == (3, 0)


def test_desktop_remote_refresh_uses_same_scheduler_and_search(multi_owner, monkeypatch):
    from atom_search.service.owner import open_service_owner
    owner, sources = multi_owner
    attached = open_service_owner(owner.profile_dir, owner.model_dir)
    calls = []
    original = owner.scheduler.start_catalog
    def start(ids):
        calls.append(ids)
        return original(ids)
    monkeypatch.setattr(owner.scheduler, 'start_catalog', start)
    try:
        view = attached.desktop_view()
        assert view.scan(sources[0]['path'])['status'] == 'complete'
        assert calls == [tuple(item['id'] for item in sources)]
        assert len(view.search('drawing', root=sources[1]['path'])) == 1
        assert attached.scheduler is None and attached.core is None
    finally:
        attached.close()


def test_pg_health_connections_are_read_only_deadlined_and_do_not_block_runtime(pg_catalog, pg_runtime):
    from concurrent.futures import ThreadPoolExecutor
    import psycopg
    from atom_search.runtime.postgres import PostgresRuntimeError
    runtime = pg_runtime[0]
    with runtime.open_health_connection(deadline=time.monotonic() + 2) as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone() == ('on',)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute('UPDATE atom_search.catalogs SET generation=generation')
    # Disable the server timeout for the following statement, so the client's
    # deadline must close its live query and context cleanup must preserve the
    # typed timeout rather than inspecting an already-finished libpq handle.
    before = time.monotonic()
    with pytest.raises(PostgresRuntimeError, match='health_probe_timeout'):
        with runtime.open_health_connection(deadline=before + 2) as connection:
            connection.execute('SET LOCAL statement_timeout = 0; SELECT pg_sleep(10)')
    assert time.monotonic() - before < 2.5
    connection.close()
    before = time.monotonic()
    with pytest.raises((psycopg.errors.QueryCanceled, PostgresRuntimeError)):
        with runtime.open_health_connection(deadline=before + 2) as connection:
            connection.execute('SELECT pg_sleep(10)')
    assert time.monotonic() - before < 2.5
    with runtime._thread_lock, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.open_health_connection, deadline=time.monotonic() + .03)
        with pytest.raises(PostgresRuntimeError, match='health_probe_timeout'):
            future.result(timeout=1)
    assert runtime.status().state == 'ready'


def test_pg_health_generation_and_counts_share_a_concurrent_sql_snapshot(pg_catalog, pg_connections, pg_runtime, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from atom_search.service.health import HealthCache, database_probe
    from atom_search.service.contracts import SettingsSnapshot
    from atom_search_core.models import DirectorySnapshot, FileMetadata
    from atom_search_core.storage.public_ids import PostgresPublicIds
    root, public_id = 'C:/fictional-concurrent-health', str(uuid.uuid4())
    PostgresPublicIds(pg_catalog).bind_many(((public_id, root),))
    internal = pg_catalog.begin_root(root)
    before = (FileMetadata('old.pdf', 'old.pdf', 0, 0),)
    pg_catalog.reconcile(internal, DirectorySnapshot('', before))
    with pg_connections['application']() as connection:
        generation = connection.execute("SELECT generation FROM atom_search.catalogs WHERE state='active'").fetchone()[0]
    health = HealthCache()
    health.configure(SettingsSnapshot(1, 1, {'sources': [dict(id=public_id, path=root, enabled=True)]}, ()))
    runtime = pg_runtime[0]
    owner = SimpleNamespace(health=health, runtime=runtime, core=SimpleNamespace(_embedder=None, catalog=pg_catalog))
    opened = runtime.open_health_connection
    @contextmanager
    def concurrent(*, deadline):
        with opened(deadline=deadline) as connection:
            execute = connection.execute
            class GenerationCursor:
                def __init__(self, cursor):
                    self.cursor = cursor
                def fetchone(self):
                    row = self.cursor.fetchone()
                    # Another real transaction commits between the two reads.
                    pg_catalog.reconcile(internal, DirectorySnapshot('', (*before, FileMetadata('new.pdf', 'new.pdf', 0, 0))))
                    return row
            def run(query, parameters=None):
                cursor = execute(query, parameters)
                return GenerationCursor(cursor) if query.startswith('SELECT generation') else cursor
            connection.execute = run
            yield connection
    with monkeypatch.context() as patch:
        patch.setattr(runtime, 'open_health_connection', concurrent)
        observed = database_probe(owner, time.monotonic() + 2, threading.Event())['counts']
    assert (observed['generation'], observed['counts'][public_id]) == (generation, (1, 0))
    after = database_probe(owner, time.monotonic() + 2, threading.Event())['counts']
    assert (after['generation'], after['counts'][public_id]) == (generation + 1, (2, 0))


def test_pg_full_database_probe_bounds_sql_and_drains_its_fixed_worker(pg_catalog, pg_runtime, monkeypatch):
    from types import SimpleNamespace
    from atom_search.runtime.postgres import _HealthConnection
    from atom_search.service.contracts import SettingsSnapshot
    from atom_search.service.health import HealthCache, ProbeScheduler, database_probe, utc_now
    health = HealthCache()
    health.configure(SettingsSnapshot(1, 1, {'sources': []}, ()))
    owner = SimpleNamespace(health=health, runtime=pg_runtime[0], core=SimpleNamespace(_embedder=None, catalog=pg_catalog))
    entered = threading.Event()
    original = _HealthConnection.execute
    def sleeping(connection, query, parameters=None):
        if query.startswith('SELECT generation'):
            entered.set()
            return original(connection, 'SELECT pg_sleep(10)')
        return original(connection, query, parameters)
    monkeypatch.setattr(_HealthConnection, 'execute', sleeping)
    probes = ProbeScheduler(health, {'database': lambda deadline, stop: database_probe(owner, deadline, stop)}, budget_seconds=.2)
    began = time.monotonic()
    try:
        probes.tick(utc_now())
        assert entered.wait(1)
        probes.close()
        assert time.monotonic() - began < .7
        assert health.snapshot(now=utc_now(), access=owner_access()).components.database.checked_at is None
        assert all(connection.closed for connection in owner.runtime._connections)
        assert not any(thread.name.startswith('atom-health-') and thread.is_alive() for thread in threading.enumerate())
        assert owner.runtime.status().state == 'ready'
    finally:
        probes.close()


def owner_access():
    from atom_search.service.contracts import AccessContext
    return AccessContext('qa', frozenset({'owner'}), ())


def test_local_and_attached_cancel_neighbor_wait_then_owner_drains_once(multi_owner, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from atom_search.service.owner import open_service_owner
    owner, sources = multi_owner
    attached = open_service_owner(owner.profile_dir, owner.model_dir)
    entered, release = threading.Event(), threading.Event()
    neighbor_cancel = []
    original = owner.core.source.snapshots
    closes = []
    close = owner.core.close
    def closing():
        closes.append('close')
        return close()
    monkeypatch.setattr(owner.core, 'close', closing)
    def blocked(root, cancel, *, excluded_roots=()):
        if canonical_root(root) == canonical_root(sources[1]['path']):
            neighbor_cancel.append(cancel)
            entered.set()
            while not release.wait(.02) and not cancel.is_set():
                pass
        return original(root, cancel, excluded_roots=excluded_roots)
    monkeypatch.setattr(owner.core.source, 'snapshots', blocked)
    try:
        for index, view in enumerate((owner.desktop_view(), attached.desktop_view())):
            entered.clear()
            release.clear()
            cancel = threading.Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(view.scan, sources[0]['path'], cancel=cancel)
                try:
                    assert entered.wait(90), owner.scheduler.snapshot()
                    cancel.set()
                    assert future.result(timeout=5)['status'] == 'cancelled'
                    assert not neighbor_cancel[-1].is_set()
                    if index == 0:
                        release.set()
                        wait_for(lambda: all(job.state not in ('queued', 'running') for job in owner.scheduler.snapshot()))
                    else:
                        attached.close()
                        assert owner.runtime.status().state == 'ready' and not neighbor_cancel[-1].is_set()
                        owner.close()  # Cancels and drains the actual remaining B worker.
                finally:
                    release.set()
                    cancel.set()
        assert closes == ['close'] and owner.runtime.status().state == 'stopped'
        assert owner.runtime._claimed_process() is None
        import json
        assert json.loads(owner.runtime.storage_budget.ledger.read_text()) == []
    finally:
        release.set()
        attached.close()


def test_pg_skipped_links_are_partial_preserve_uuid_and_last_success(pg_catalog, pg_connections):
    from atom_search_core.models import DirectorySnapshot, FileMetadata
    from atom_search_core.service import SearchService
    from atom_search_core.storage.postgres_retrieval import PostgresRetrieval
    from atom_search_core.storage.public_ids import PostgresPublicIds
    root = 'C:/fictional-linked-source'
    identity = pg_catalog.begin_root(root)
    pg_catalog.reconcile(identity, DirectorySnapshot('', directories=('linked',)))
    pg_catalog.reconcile(identity, DirectorySnapshot('linked', (FileMetadata('linked/kept.pdf', 'kept.pdf', 0, 0),)))
    pg_catalog.finish_root(identity, 'complete')
    scope = pg_catalog.resolve_scope(root)
    before = pg_catalog.status(scope=scope)
    file = pg_catalog.pending_embeddings('fixture-model|filename-parent-v1', 384, 1)[0].file
    public_ids = PostgresPublicIds(pg_catalog)
    public_id = public_ids.public_file_id(file.file_id)
    class Source:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            yield DirectorySnapshot('', protected_names=('linked',), skipped_links=1)
    service = SearchService(catalog=pg_catalog, retrieval=PostgresRetrieval(pg_connections['application']), source=Source())
    report = service.scan(root)
    after = service.status(root=root)
    assert report['status'] == after['roots'][0]['status'] == 'partial'
    assert not after['metadata_ready'] and after['last_success'] == before['last_success']
    assert after['files'] == 1 and public_ids.public_file_id(file.file_id) == public_id
    assert service.search('kept', root=root)[0].stale


def test_fresh_empty_owner_start_apply_restart_never_probes_a_source(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from atom_search.service.owner import open_service_owner
    from atom_search.service.contracts import ServiceError
    from atom_search_core.source import FileSystemSource
    def forbidden(*args, **kwargs):
        pytest.fail('empty profile enumerated a source')
    monkeypatch.setattr(FileSystemSource, 'snapshots', forbidden)
    profile, model = tmp_path / 'empty-profile', tmp_path / 'missing-model'
    applied = None
    for attempt in range(2):
        owner = open_service_owner(profile, model, automatic_catalog=True)
        try:
            current = owner.repository.snapshot()
            assert current.schema_version == 3 and current.settings['sources'] == []
            if applied is not None:
                assert current == applied
            owner.start()
            facade = owner.facade(owner.owner_access)
            assert facade.start_catalog() == ()
            owner.scheduler.tick(datetime.now(timezone.utc) + timedelta(days=365))
            assert owner.scheduler.snapshot() == ()
            view = owner.desktop_view()
            assert view.search('drawing') == []
            with pytest.raises(ServiceError) as failure:
                view.scan(None)
            assert failure.value.code == 'source_scope_denied'
            candidate = current.settings
            candidate['ui']['theme'] = 'dark'
            applied = owner.coordinator.owner_apply(candidate, current.revision)
            assert applied.settings['sources'] == []
        finally:
            owner.close()
