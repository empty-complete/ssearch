"""Public cached health: truthful facts, caller scopes and no read-path I/O."""
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import threading
import time
from types import SimpleNamespace

import pytest

from atom_search.service.contracts import (AccessContext, SettingsSnapshot, SourceHealth,
    ModelHealth, DatabaseHealth, JobSnapshot)

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
NOW = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
OWNER = AccessContext('owner', frozenset({'owner'}), ())


def module():
    name = 'atom_search.service.health'
    assert importlib.util.find_spec(name) is not None, 'Task6 requires a memory-only health cache'
    return importlib.import_module(name)


def cache():
    value = module().HealthCache()
    value.configure(SettingsSnapshot(1, 7, {'sources': [
        dict(id=A, path='C:/private-a', enabled=True),
        dict(id=B, path='C:/allowed-b', enabled=True)]}, ()))
    value.record('database', DatabaseHealth('postgresql', '18.6', '0.8.7', 'ready', None, True, None), checked_at=NOW)
    value.record('model', ModelHealth('ready', 'test-model', None, True, False), checked_at=NOW)
    return value


def source(identity, state='available'):
    return SourceHealth(identity, state, None, None, True, None, None, None, None, True)


@pytest.mark.parametrize('available', [False, True])
def test_project_capabilities_require_observed_readiness_and_keep_lexical_without_model(available, monkeypatch):
    value = cache()
    value.configure(SettingsSnapshot(2, 7, {'sources': [dict(id=B, path='C:/allowed', enabled=True)]}, ()))
    value.record('model', ModelHealth('unavailable', None, None, True, False), checked_at=NOW)
    record = getattr(value, 'record_project_support', None)
    assert callable(record), 'project capability readiness needs a revision-fenced cache observation'
    reader = AccessContext('reader', frozenset({'search', 'status'}), (B,))
    assert not {'project_search', 'project_aliases'} & set(value.snapshot(now=NOW, access=reader).capabilities)
    record(available=available, revision=7, checked_at=NOW)
    def forbidden(*args, **kwargs):
        pytest.fail('capability status performed I/O')
    monkeypatch.setattr('pathlib.Path.stat', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    snapshot = value.snapshot(now=NOW, access=reader)
    assert ({'project_search', 'project_aliases'} <= set(snapshot.capabilities)) is available
    assert not {'project_search', 'project_aliases'} & set(value.snapshot(
        now=NOW, access=AccessContext('status-only', frozenset({'status'}), (B,))).capabilities)
    assert not {'project_search', 'project_aliases'} & set(value.snapshot(
        now=NOW + timedelta(seconds=301), access=reader).capabilities)


def test_project_capability_drops_on_apply_and_rejects_old_or_failed_observation():
    value = cache()
    value.configure(SettingsSnapshot(2, 7, {'sources': [dict(id=B, enabled=True)]}, ()))
    record = getattr(value, 'record_project_support', None)
    assert callable(record), 'project readiness must not survive an unobserved settings revision'
    record(available=True, revision=7, checked_at=NOW)
    assert 'project_search' in value.snapshot(now=NOW, access=OWNER).capabilities
    value.configure(SettingsSnapshot(2, 8, {'sources': [dict(id=B, enabled=True)]}, ()))
    record(available=True, revision=7, checked_at=NOW)
    assert 'project_search' not in value.snapshot(now=NOW, access=OWNER).capabilities
    record(available=False, revision=8, checked_at=NOW)
    assert 'project_search' not in value.snapshot(now=NOW, access=OWNER).capabilities
    record(available=True, revision=8, checked_at=NOW)
    value.record('database', DatabaseHealth('postgresql', None, None, 'blocked', None, True,
                                          'migration_failed'), checked_at=NOW)
    assert 'project_search' not in value.snapshot(now=NOW, access=OWNER).capabilities


def test_failed_background_database_observation_drops_project_claim_without_freshening_database():
    value = cache()
    value.configure(SettingsSnapshot(2, 7, {'sources': [dict(id=B, enabled=True)]}, ()))
    now = module().utc_now()
    value.record('database', DatabaseHealth('postgresql', '18.6', '0.8.7', 'ready', None, True, None), checked_at=now)
    value.record_project_support(available=True, revision=7, checked_at=now)
    before = value.snapshot(now=now, access=OWNER)
    assert 'project_search' in before.capabilities
    entered = threading.Event()
    def failed(deadline, stop):
        entered.set()
        raise RuntimeError('projection observation failed')
    scheduler = module().ProbeScheduler(value, {'database': failed})
    try:
        scheduler.start()
        assert entered.wait(1)
        deadline = time.monotonic() + 1
        while 'project_search' in value.snapshot(now=now, access=OWNER).capabilities and time.monotonic() < deadline:
            time.sleep(.01)
        after = value.snapshot(now=now, access=OWNER)
        assert 'project_search' not in after.capabilities
        assert after.components.database.checked_at == before.components.database.checked_at
    finally:
        scheduler.close()


def test_status_does_not_probe_smb_database_disk_or_model(monkeypatch):
    value = cache()
    value.record('source', source(A), checked_at=NOW)
    value.record_counts({A: (12, 7), B: (2, 0)}, generation=19, revision=7, checked_at=NOW)
    def forbidden(*args, **kwargs):
        raise AssertionError('status attempted I/O')
    owner = SimpleNamespace(health=value, operation=lambda: nullcontext(),
        repository=SimpleNamespace(snapshot=forbidden), runtime=SimpleNamespace(status=forbidden),
        index_generation=forbidden)
    from atom_search.service.application import AtomSearchService
    import atom_search.service.application as application
    monkeypatch.setattr(application, 'utc_now', lambda: (NOW + timedelta(seconds=1)).isoformat())
    monkeypatch.setattr('builtins.open', forbidden)
    monkeypatch.setattr('pathlib.Path.stat', forbidden)
    result = AtomSearchService(owner, OWNER).status()
    assert (result.config_version.revision, result.index_generation) == (7, 19)
    assert result.observed_at != result.components.sources[0].checked_at
    assert result.components.sources[0].catalogued_files == 12


def test_probe_freshness_and_unknown_source_are_explicit():
    value = cache()
    value.record('source', source(A), checked_at=NOW)
    fresh = value.snapshot(now=NOW + timedelta(seconds=300), access=OWNER)
    stale = value.snapshot(now=NOW + timedelta(seconds=300, microseconds=1), access=OWNER)
    a, b = fresh.components.sources
    assert not a.stale and stale.components.sources[0].stale
    assert (b.state, b.checked_at, b.last_success_at, b.catalogued_files) == ('unknown', None, None, None)
    assert b.stale and b.catalog_counts_stale


def test_readiness_keeps_lexical_search_when_model_or_source_unavailable():
    value = cache()
    assert value.snapshot(now=NOW, access=OWNER).readiness == 'warming'
    value.record_counts({A: (12, 7), B: (0, 0)}, generation=9, revision=7, checked_at=NOW)
    value.record('source', source(A), checked_at=NOW)
    value.record('source', source(B), checked_at=NOW)
    assert value.snapshot(now=NOW, access=OWNER).readiness == 'ready'
    value.record('model', ModelHealth('unavailable', None, None, True, False), checked_at=NOW)
    degraded = value.snapshot(now=NOW, access=OWNER)
    assert degraded.readiness == 'degraded' and degraded.components.model.lexical_available
    value.record('source', source(B, 'unreachable'), checked_at=NOW)
    assert value.snapshot(now=NOW, access=OWNER).readiness == 'degraded'
    value.record('database', DatabaseHealth('postgresql', None, None, 'blocked', None, True, 'server_exited'), checked_at=NOW)
    assert value.snapshot(now=NOW, access=OWNER).readiness == 'blocked'


def test_storage_counts_pgdata_once_and_separates_logical_and_reserve():
    storage = module().storage_health(dict(pgdata_bytes=1000, wal_bytes=200, temp_bytes=100,
        cache_bytes=50, outbox_bytes=20, backup_bytes=30, staging_bytes=40,
        physical_owned_bytes=1140, database_logical_bytes=700, reserved_bytes=500,
        quota_bytes=2000, free_bytes=9000), checked_at=NOW)
    assert storage.physical_owned_bytes == 1140
    assert (storage.database_logical_bytes, storage.reserved_bytes) == (700, 500)
    assert module().storage_health({}, checked_at=None).physical_owned_bytes is None
    assert module().storage_health({}, checked_at=None).reserved_bytes is None


def test_health_redacts_other_source_jobs_and_paths_and_drops_removed_scope():
    value = cache()
    jobs = tuple(JobSnapshot(identity, source_id, 'running', 'catalog', None, None,
        3, 3, 3, 0, 0, None, None) for identity, source_id in ((A, A), (B, B)))
    value.record('jobs', jobs, checked_at=NOW)
    value.record_counts({A: (12, 7), B: (2, 0)}, generation=9, revision=7, checked_at=NOW)
    access = AccessContext('reader-b', frozenset({'search', 'status', 'deep_content', 'agent_repair'}), (B,))
    result = value.snapshot(now=NOW, access=access)
    assert [item.source_id for item in result.components.sources] == [B]
    assert [item.source_id for item in result.components.jobs] == [B]
    assert 'private-a' not in str(asdict(result)) and A not in str(asdict(result))
    assert result.capabilities == ('search', 'status')
    value.configure(SettingsSnapshot(1, 8, {'sources': [dict(id=A, enabled=True)]}, ()))
    assert value.snapshot(now=NOW, access=access).components.sources == ()
    assert value.snapshot(now=NOW, access=access).components.jobs == ()


def test_catalog_counts_are_cached_persisted_counts_not_current_job_progress():
    value = cache()
    value.record_counts({A: (12, 7), B: (4, 0)}, generation=10, revision=7, checked_at=NOW)
    value.record('source', source(A, 'unreachable'), checked_at=NOW + timedelta(seconds=10))
    value.record('jobs', (JobSnapshot(B, A, 'running', 'catalog', None, None, 3, 3, 3, 0, 0, None, None),), checked_at=NOW)
    result = value.snapshot(now=NOW + timedelta(seconds=11), access=OWNER)
    a = result.components.sources[0]
    assert (a.catalogued_files, a.metadata_embedded_files) == (12, 7)
    assert a.checked_at != a.catalog_counts_checked_at
    value.record_counts({A: (99, 99)}, generation=11, revision=6, checked_at=NOW)
    assert value.snapshot(now=NOW, access=OWNER).components.sources[0].catalogued_files == 12


@pytest.mark.parametrize('reserved', [None, 0, 500])
def test_health_json_client_roundtrip_preserves_unknown_and_known_reservations(reserved):
    from atom_search.http.serialization import encode, decode, envelope, unpack
    from atom_search.service.client import ServiceClient
    value = cache()
    value.record('storage', module().storage_health({'reserved_bytes': reserved}, checked_at=NOW), checked_at=NOW)
    wire = encode(envelope(asdict(value.snapshot(now=NOW, access=OWNER))))
    client = ServiceClient.__new__(ServiceClient)
    client._http = lambda method, path: unpack(decode(wire))
    restored = client.status()
    assert restored.components.storage.reserved_bytes == reserved
    assert restored.components.storage.physical_owned_bytes is None


def test_probe_scheduler_bounds_workers_and_discards_over_budget_results():
    health = cache()
    entered, release = threading.Event(), threading.Event()
    foreign_entered, foreign_release = threading.Event(), threading.Event()
    workers, foreign_workers = [], []
    def delayed(deadline, stop):
        workers.append(threading.current_thread())
        entered.set()
        release.wait(2)
        return {'model': ModelHealth('unavailable', None, None, True, False)}
    def independent(deadline, stop):
        foreign_workers.append(threading.current_thread())
        foreign_entered.set()
        foreign_release.wait(2)
    probes = module().ProbeScheduler(health, {'local': delayed}, budget_seconds=.02, interval_seconds=10)
    foreign = module().ProbeScheduler(cache(), {'foreign': independent}, interval_seconds=10)
    try:
        foreign.tick(NOW)
        assert foreign_entered.wait(1)
        probes.tick(NOW)
        assert entered.wait(1)
        time.sleep(.03)
        for _ in range(20):
            probes.tick(NOW + timedelta(seconds=20))
        release.set()
        probes.close()
        assert health.snapshot(now=NOW, access=OWNER).components.model.state == 'ready'
        assert len(workers) == 1  # Repeated due ticks coalesce into this lane.
        assert len(foreign_workers) == 1 and foreign_workers[0].is_alive()
        # Drain belongs to this scheduler. Another legitimate owner keeps its
        # own worker alive until that owner closes it.
        assert not any(worker.is_alive() for worker in workers)
    finally:
        release.set()
        foreign_release.set()
        try:
            probes.close()
        finally:
            foreign.close()


def test_actual_catalog_report_classifies_denied_without_disclosing_error_paths(tmp_path, monkeypatch):
    import os
    from atom_search_core.service import SearchService
    from atom_search_core.source import canonical_root
    root = tmp_path / 'private-documents'
    root.mkdir()
    core = SearchService(tmp_path / 'catalog.sqlite')
    original = os.stat
    def denied(path, *args, **kwargs):
        if canonical_root(str(path)).removeprefix('\\\\?\\') == canonical_root(str(root)):
            raise PermissionError('controlled denial with a private path')
        return original(path, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'stat', denied)
            report = core.scan(str(root))
        value = cache()
        value.record_source_report(A, report, revision=7, checked_at=NOW)
        result = value.snapshot(now=NOW, access=OWNER).components.sources[0]
        assert (result.state, result.error_code) == ('access_denied', 'source_access_denied')
        assert 'private path' not in str(asdict(result))
    finally:
        core.close()


def test_scheduler_publishes_running_before_progress_and_does_not_invent_source_failures(tmp_path):
    from test_catalog_scheduler import subject
    entered, release = threading.Event(), threading.Event()
    class BrokenSource:
        def snapshots(self, root, cancel, *, excluded_roots=()):
            entered.set()
            release.wait(2)
            raise RuntimeError('controlled worker failure, no availability observation')
    settings, core, queue = subject(tmp_path, source=BrokenSource())
    value = module().HealthCache()
    value.configure(SettingsSnapshot(1, 1, settings, ()))
    queue.owner.health = value
    try:
        identity = queue.start_catalog((settings['sources'][0]['id'],))[0]
        assert entered.wait(1)
        observed = value.snapshot(now=datetime.now(timezone.utc), access=OWNER)
        assert next(job for job in observed.components.jobs if job.job_id == identity).state == 'running'
        release.set()
        queue.wait_catalog(identity)
        observed = value.snapshot(now=datetime.now(timezone.utc), access=OWNER)
        source_health = observed.components.sources[0]
        assert source_health.state == 'unknown' and source_health.checked_at is None
    finally:
        release.set()
        queue.close()
        core.close()


def test_server_exited_runtime_state_blocks_readiness_even_with_cached_rows():
    from atom_search.runtime.postgres import RuntimeState
    value = cache()
    value.record_counts({A: (12, 7)}, generation=10, revision=7, checked_at=NOW)
    actual = RuntimeState('degraded', None, 'server_exited')
    value.record('database', DatabaseHealth('postgresql', None, None,
        actual.state, None, True, actual.error_code), checked_at=NOW)
    observed = value.snapshot(now=NOW, access=OWNER)
    assert observed.readiness == 'blocked' and not observed.components.model.lexical_available


def test_full_database_probe_bounds_busy_runtime_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from atom_search.runtime.postgres import PostgresRuntime, PostgresRuntimeError
    runtime = PostgresRuntime(tmp_path / 'unused-runtime', tmp_path / 'unused-profile')
    owner = SimpleNamespace(health=cache(), runtime=runtime, core=SimpleNamespace(_embedder=None))
    with ThreadPoolExecutor(max_workers=1) as pool:
        runtime._thread_lock.acquire()
        try:
            future = pool.submit(module().database_probe, owner, time.monotonic() + .03, threading.Event())
            with pytest.raises(PostgresRuntimeError, match='health_probe_timeout'):
                future.result(timeout=.3)
        finally:
            runtime._thread_lock.release()


def test_full_database_probe_uses_remaining_subsecond_connect_budget(tmp_path):
    import os
    import socket
    import psycopg  # Warm import before measuring the actual connect budget.
    from dataclasses import asdict
    from atom_search.runtime import _windows
    from atom_search.runtime.postgres import PostgresRuntime, RuntimeState, RuntimeEndpoint, PostgresRuntimeError
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)  # TCP accepts, but PostgreSQL authentication never answers.
        runtime = PostgresRuntime(tmp_path / 'unused-runtime', tmp_path / 'unused-profile')
        runtime._record = dict(port=listener.getsockname()[1], server=asdict(_windows.process_identity(os.getpid())),
                               credentials={'maintenance': 'qa-maintenance', 'application': 'qa-application'})
        runtime._credentials = SimpleNamespace(resolve=lambda reference: 'qa-only-password')
        runtime._state = RuntimeState('ready', RuntimeEndpoint('127.0.0.1', listener.getsockname()[1],
            'postgres', A, '18.6', '0.8.7', 'qa-application'), None)
        owner = SimpleNamespace(health=cache(), runtime=runtime, core=SimpleNamespace(_embedder=None))
        began = time.monotonic()
        with pytest.raises(PostgresRuntimeError, match='health_probe_timeout'):
            module().database_probe(owner, began + .05, threading.Event())
        assert time.monotonic() - began < .4
        assert all(connection.closed for connection in runtime._connections)
        # The public async connect was interrupted during authentication.
        # Verify TCP EOF, rather than relying on an empty tracked-connection set.
        listener.settimeout(.3)
        peer, _ = listener.accept()
        with peer:
            peer.settimeout(.3)
            while peer.recv(4096):
                pass
        assert not any(thread.name.startswith('asyncio_') and thread.is_alive() for thread in threading.enumerate())
