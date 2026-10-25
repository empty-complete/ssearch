"""Offline acceptance of the actual managed PostgreSQL application factory."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import shutil
import sys
import tempfile
import time
import uuid
from unittest.mock import patch


@contextmanager
def owned_loopback_only(endpoint):
    """Constrain Python sockets; native libpq/network proof remains P0's gate."""
    original_socket, original_resolve = socket.socket, socket.getaddrinfo
    def allowed(address):
        target = endpoint()
        targets = target if isinstance(target, tuple) else (target,)
        if (not isinstance(address, tuple) or len(address) < 2
                or not any(item is not None and item.host == '127.0.0.1'
                           and address[:2] == (item.host, item.port) for item in targets)):
            raise RuntimeError('self_test_network_forbidden')
    class OwnedSocket(original_socket):
        def connect(self, address):
            allowed(address)
            return super().connect(address)
        def connect_ex(self, address):
            allowed(address)
            return super().connect_ex(address)
    def resolve(host, port, *args, **kwargs):
        allowed((host, port))
        return original_resolve(host, port, *args, **kwargs)
    def private_pair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        # Windows implements socketpair through a private loopback listener.
        # Construct only this pair with original public socket primitives; no
        # endpoint/port is added to the process-wide connect allowlist.
        if family != socket.AF_INET or type != socket.SOCK_STREAM or proto != 0:
            raise ValueError('diagnostic_socketpair_requires_local_stream')
        listener = original_socket(family, type, proto)
        peers, wrapped = [], []
        try:
            listener.settimeout(2)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            client = original_socket(family, type, proto)
            peers.append(client)
            client.settimeout(2)
            client.connect(listener.getsockname())
            server, _ = listener.accept()
            peers.append(server)
            if server.getpeername() != client.getsockname() or client.getpeername() != server.getsockname():
                raise RuntimeError('diagnostic_socketpair_peer_mismatch')
            for peer in peers:
                wrapped.append(OwnedSocket(family, type, proto, fileno=peer.detach()))
            return tuple(wrapped)
        except BaseException:
            for peer in (*peers, *wrapped):
                peer.close()
            raise
        finally:
            listener.close()
    with (patch.object(socket, 'socket', OwnedSocket), patch.object(socket, 'getaddrinfo', resolve),
          patch.object(socket, 'socketpair', private_pair)):
        yield


def run_service_self_test(report_path: Path, *, data_dir: Path, model_dir: Path | None = None) -> int:
    """Actual packaged owner/two-client proof, without importing or showing Qt."""
    from dataclasses import asdict
    import http.client
    import os
    import subprocess
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from atom_search import search_runtime
    from atom_search.paths import model_path
    from atom_search.runtime.postgres import PostgresRuntime
    from atom_search.runtime import _windows
    from atom_search.service.owner import open_service_owner
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import AccessContext, SearchRequest, ServiceError
    base, destination = Path(data_dir).absolute(), Path(report_path).absolute()
    report = dict(ok=False, execution_mode='frozen' if getattr(sys, 'frozen', False) else 'source',
                  scenario='two-clients', clean_stop=False, model_constructions=0, runtime_starts=0,
                  runtime_stops=0, core_closes=0)
    owner = None
    try:
        if base.exists() or destination.exists() or destination.is_relative_to(base):
            raise RuntimeError('disposable_profile_must_be_new')
        base.mkdir(parents=True)
        sources = []
        for label in ('allowed', 'private'):
            directory = base / label
            directory.mkdir()
            (directory / (label + '-drawing.pdf')).write_text('private body never returned')
            sources.append(dict(id=str(uuid.uuid4()), path=str(directory), label=label, enabled=True))
        owner = open_service_owner(base / 'profile', model_dir or model_path(), automatic_catalog=False)
        snapshot = owner.repository.snapshot()
        candidate = snapshot.settings
        candidate['sources'] = sources
        owner.coordinator.owner_apply(candidate, snapshot.revision)
        load_model, start_runtime, stop_runtime = search_runtime._load_model, PostgresRuntime.start, PostgresRuntime.stop
        def load(*args):
            report['model_constructions'] += 1
            return load_model(*args)
        def start(instance):
            report['runtime_starts'] += 1
            return start_runtime(instance)
        def stop(instance):
            report['runtime_stops'] += 1
            return stop_runtime(instance)
        def endpoints():
            pg = owner.runtime.status().endpoint if owner.runtime is not None else None
            http = (SimpleNamespace(host='127.0.0.1', port=owner._server.server_address[1])
                    if owner._server is not None and owner.generation is not None else None)
            return pg, http
        with patch.object(search_runtime, '_load_model', load), patch.object(PostgresRuntime, 'start', start), \
                patch.object(PostgresRuntime, 'stop', stop), owned_loopback_only(endpoints):
            owner.start()
            close = owner.core.close
            def closing():
                report['core_closes'] += 1
                return close()
            owner.core.close = closing
            report['model_ready'] = owner.core._embedder is not None
            report['owner_pid'] = os.getpid()
            report['pg_pid'] = owner.runtime._record['server']['pid']
            report['installation_id'], report['generation'] = owner.installation_id, owner.generation
            for source in sources:
                assert owner.core.scan(source['path'])['status'] == 'complete'
            owner.core.index_embeddings()
            owner_client = ServiceClient(owner.endpoint_file, owner.credential_ref)
            reference = owner.issue_credential(AccessContext('qa-reader', frozenset({'search', 'fetch', 'status', 'settings_get'}), (sources[0]['id'],)))
            reader = ServiceClient(owner.endpoint_file, reference)
            all_results = owner_client.search(SearchRequest('drawing', mode='literal')).results
            allowed_results = reader.search(SearchRequest('drawing', mode='literal')).results
            assert len(all_results) == 2 and len(allowed_results) == 1
            metadata = reader.fetch(allowed_results[0].result_id)
            assert metadata.score is None and metadata.match_kind == 'metadata'
            assert not {'content', 'snippets', 'text'} & asdict(metadata).keys()
            private_result = next(item for item in all_results if item.source_id == sources[1]['id'])
            checks = []
            for operation, expected in ((lambda: reader.fetch(private_result.result_id), 'result_not_found'),
                    (lambda: reader.search(SearchRequest('drawing', source_ids=(sources[1]['id'],))), 'source_scope_denied'),
                    (lambda: reader.settings_apply({}, 1), 'access_denied')):
                try:
                    operation()
                    raise RuntimeError('qa_access_not_enforced')
                except ServiceError as failure:
                    assert failure.code == expected
                    checks.append(expected)
            parsed = urlsplit(owner.base_url)
            for headers, expected in (({}, 401), ({'Authorization': 'Bearer ' + owner.credentials.resolve(owner.credential_ref), 'Origin': 'null'}, 403)):
                headers.update({'X-Atom-Installation': owner.installation_id, 'X-Atom-Generation': owner.generation})
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
                try:
                    connection.request('GET', '/v1/status', headers=headers)
                    response = connection.getresponse()
                    assert response.status == expected
                    response.read()
                finally:
                    connection.close()
            attached = open_service_owner(owner.profile_dir, owner.model_dir)
            attached.start()
            assert attached.attached and attached.core is None
            attached.close()
            child_report = base / 'second-launch.json'
            command = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'atom_search']
            child = subprocess.run(command + ['--service-attach-test', str(child_report), '--data-dir',
                str(owner.profile_dir), '--model-dir', str(owner.model_dir)], timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW)
            assert child.returncode == 0
            second_launch = json.loads(child_report.read_bytes())
            assert (second_launch['ok'] and second_launch['pid'] != os.getpid()
                    and second_launch['installation_id'] == owner.installation_id
                    and second_launch['generation'] == owner.generation)
            report['second_launch_pid'] = second_launch['pid']
            owner_client.close()
            assert len(reader.search(SearchRequest('drawing')).results) == 1
            snapshot = ServiceClient(owner.endpoint_file, owner.credential_ref).settings_get()
            assert ServiceClient(owner.endpoint_file, owner.credential_ref).settings_validate(snapshot.settings, snapshot.revision).valid
            secret = owner.credentials.resolve(owner.credential_ref).encode()
            assert secret not in repr(sys.argv).encode()
            for path in owner.profile_dir.rglob('*'):
                if path.is_file() and path.suffix in {'.json', '.log', '.out'}:
                    assert secret not in path.read_bytes()
            _windows.assert_private(owner.endpoint_file)
            report.update(two_clients=True, scoped_fetch=True, metadata_only=True, auth=True,
                          access_checks=checks, attach=True, second_launch=True, token_absent=True)
            owner.close()
            report['clean_stop'] = owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
        report['ok'] = (report['clean_stop'] and report['model_ready'] and report['model_constructions'] == 1
                        and report['runtime_starts'] == report['runtime_stops'] == report['core_closes'] == 1)
    except Exception as failure:
        report['error_code'] = getattr(failure, 'code', type(failure).__name__)
    finally:
        if owner is not None:
            try:
                owner.close()
                report['clean_stop'] = owner.runtime is not None and owner.runtime.status().state == 'stopped'
            except Exception as failure:
                report['cleanup_error'] = getattr(failure, 'code', type(failure).__name__)
                report['ok'] = False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def run_multi_source_service_test(report_path: Path, *, data_dir: Path, model_dir: Path | None = None) -> int:
    """Real packaged queue/PG proof with controlled interruption of real I/O.

    The model gate wraps the one installed session; the filesystem source and
    PostgreSQL application composition remain the actual production objects.
    """
    import os
    import threading
    from atom_search.paths import model_path
    from atom_search.service.owner import open_service_owner
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import SearchRequest
    from atom_search_core.source import canonical_root, native
    base, destination = Path(data_dir).absolute(), Path(report_path).absolute()
    report = dict(ok=False, execution_mode='frozen' if getattr(sys, 'frozen', False) else 'source',
                  scenario='multi-source', backend='postgresql', factory='open_service_owner', clean_stop=False)
    owner = None
    model_release, scan_release = threading.Event(), threading.Event()
    # Two real sources require >30 s of existing whole-engine admission. Give
    # each phase the same measured 90 s cap as native queue QA, within 270 s;
    # check_service's 300 s subprocess bound retains time for final cleanup.
    started = phase_started = time.monotonic()
    overall_deadline = started + 270
    def phase(name):
        nonlocal phase_started
        now = time.monotonic()
        if 'phase' in report:
            report.setdefault('phase_seconds', {})[report['phase']] = round(now - phase_started, 2)
        report['phase'] = name
        phase_started = now
    def remaining():
        return max(0, overall_deadline - time.monotonic())
    def wait_for(predicate, seconds=90):
        phase_deadline = time.monotonic() + min(seconds, remaining())
        while time.monotonic() < phase_deadline:
            if predicate():
                return
            time.sleep(.02)
        raise RuntimeError('queue_scenario_timeout')
    try:
        phase('prepare')
        if base.exists() or destination.exists() or destination.is_relative_to(base):
            raise RuntimeError('disposable_profile_must_be_new')
        base.mkdir(parents=True)
        sources = []
        for label in ('a', 'b', 'unavailable'):
            directory = base / label
            if label != 'unavailable':
                directory.mkdir()
                (directory / 'unvisited').mkdir()
                (directory / 'unvisited' / f'preserved-{label}.pdf').touch()
                for index in range(20):
                    (directory / f'drawing-{label}-{index}.pdf').touch()
            sources.append(dict(id=str(uuid.uuid4()), path=str(directory), label=label, enabled=True))
        owner = open_service_owner(base / 'profile', model_dir or model_path(), automatic_catalog=False)
        snapshot = owner.repository.snapshot()
        candidate = snapshot.settings
        candidate['sources'] = sources
        owner.coordinator.owner_apply(candidate, snapshot.revision)
        owner.start()
        if owner.core._embedder is None:
            raise RuntimeError('installed_model_required')
        installed = owner.core._embedder
        model_entered = threading.Event()
        batches = []
        class Gate:
            model_id, dimension = installed.model_id, installed.dimension
            def encode(self, texts, *, kind='passage'):
                if kind == 'passage':
                    batches.append(len(texts))
                    if not model_entered.is_set():
                        model_entered.set()
                        if not model_release.wait(remaining()):
                            raise RuntimeError('qa_model_gate_timeout')
                return installed.encode(texts, kind=kind)
        owner.core._embedder = Gate()
        client = ServiceClient(owner.endpoint_file, owner.credential_ref)
        phase('metadata_wave')
        initial = client.start_catalog()
        wait_for(model_entered.is_set)
        jobs = {job.job_id: job for job in owner.scheduler.snapshot()}
        assert [jobs[identity].state for identity in initial] == ['completed', 'completed', 'failed']
        report.update(metadata_before_embedding=True, unavailable_source=True)
        phase('model_block_search')
        search_started = time.monotonic()
        results = client.search(SearchRequest('drawing', limit=3)).results
        assert len(results) == 3 and time.monotonic() - search_started < 3
        for source in sources[:2]:
            assert client.search(SearchRequest('drawing', source_ids=(source['id'],), limit=1)).results
        report['search_during_model_block'] = True
        preserved = {item.full_path: item.result_id for item in client.search(SearchRequest('preserved')).results}
        old_embed = next(job.job_id for job in owner.scheduler.snapshot()
                         if job.phase == 'embed' and job.state == 'running')
        source_id = owner.registry.resolve((sources[0]['id'],), allowed=(sources[0]['id'],)).source_ids[0]
        scan_entered = threading.Event()
        reconcile = owner.core.catalog.reconcile
        def blocked(identity, snapshot):
            result = reconcile(identity, snapshot)
            if identity == source_id and snapshot.relative_path == '':
                scan_entered.set()
                if not scan_release.wait(remaining()):
                    raise RuntimeError('qa_scan_gate_timeout')
            return result
        with patch.object(owner.core.catalog, 'reconcile', blocked):
            phase('cancel_catalog')
            cancelled = client.start_catalog((sources[0]['id'],))[0]
            wait_for(scan_entered.is_set)
            client.cancel_job(cancelled)
            scan_release.set()
            wait_for(lambda: any(job.job_id == cancelled and job.state == 'cancelled' for job in owner.scheduler.snapshot()))
        report.update(scoped_cancel=True, catalog_preempts_embedding=True)
        scandir = os.scandir
        denied = canonical_root(native(Path(sources[1]['path']) / 'unvisited'))
        def partial(path):
            if canonical_root(str(path)) == denied:
                raise PermissionError('controlled QA metadata denial')
            return scandir(path)
        with patch.object(os, 'scandir', partial):
            phase('partial_catalog')
            partial_job = client.start_catalog((sources[1]['id'],))[0]
            wait_for(lambda: any(job.job_id == partial_job and job.state == 'partial' for job in owner.scheduler.snapshot()))
        after = client.search(SearchRequest('preserved')).results
        assert {item.full_path: item.result_id for item in after} == preserved and all(item.stale for item in after)
        report['partial_preserved'] = True
        phase('embedding_drain')
        model_release.set()
        wait_for(lambda: all(job.state not in ('queued', 'running') for job in owner.scheduler.snapshot()))
        old = next(job for job in owner.scheduler.snapshot() if job.job_id == old_embed)
        assert old.state == 'cancelled' and old.succeeded == 0
        report['late_embedding_rejected'] = True
        assert all(job.total is None and job.processed == job.succeeded + job.skipped + job.errors
                   for job in owner.scheduler.snapshot())
        report.update(honest_counters=True, max_batch=max(batches), source_count=3)
        phase('shutdown')
        owner.close()
        report['clean_stop'] = owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
        report['ok'] = report['clean_stop']
    except Exception as failure:
        report['error_code'] = getattr(failure, 'code', type(failure).__name__)
    finally:
        model_release.set()
        scan_release.set()
        if owner is not None:
            try:
                owner.close()
                report['clean_stop'] = (owner.runtime is not None and owner.runtime.status().state == 'stopped'
                                        and owner.runtime._claimed_process() is None)
            except Exception as failure:
                report['cleanup_error'] = getattr(failure, 'code', type(failure).__name__)
                report['ok'] = False
        report.setdefault('phase_seconds', {})[report.get('phase', 'prepare')] = round(time.monotonic() - phase_started, 2)
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def run_health_offline_service_test(report_path: Path, *, data_dir: Path, model_dir: Path | None = None) -> int:
    """Actual owner/PG/queue: cached rows survive an offline fictional source.

    No Qt or native window is created. Local source removal exercises cached
    metadata; this does not claim qualification of a real UNC/DFS share.
    """
    import os
    import subprocess
    import threading
    from atom_search import search_runtime
    from atom_search.paths import model_path
    from atom_search.runtime.postgres import PostgresRuntime
    from atom_search.service.owner import open_service_owner
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import AccessContext, SearchRequest
    from atom_search_core.source import canonical_root
    base, destination = Path(data_dir).absolute(), Path(report_path).absolute()
    report = dict(ok=False, execution_mode='frozen' if getattr(sys, 'frozen', False) else 'source',
        scenario='health-offline', backend='postgresql', factory='open_service_owner', clean_stop=False,
        model_constructions=0, runtime_starts=0, runtime_stops=0, core_closes=0)
    owner = None
    started = time.monotonic()
    deadline = started + 270
    def wait_for(predicate, seconds=90):
        until = min(deadline, time.monotonic() + seconds)
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.02)
        raise RuntimeError('health_scenario_timeout')
    def increment(name, operation):
        def invoke(*args, **kwargs):
            report[name] += 1
            return operation(*args, **kwargs)
        return invoke
    try:
        if base.exists() or destination.exists() or destination.is_relative_to(base):
            raise RuntimeError('disposable_profile_must_be_new')
        base.mkdir(parents=True)
        sources = []
        for label in ('a', 'b'):
            root = base / label
            root.mkdir()
            (root / ('drawing-' + label + '.pdf')).touch()
            sources.append(dict(id=str(uuid.uuid4()), path=str(root), label=label, enabled=True))
        owner = open_service_owner(base / 'profile', model_dir or model_path(), automatic_catalog=False)
        initial = owner.repository.snapshot()
        candidate = initial.settings
        candidate['sources'] = sources
        settings = owner.coordinator.owner_apply(candidate, initial.revision)
        with patch.object(search_runtime, '_load_model', increment('model_constructions', search_runtime._load_model)), \
                patch.object(PostgresRuntime, 'start', increment('runtime_starts', PostgresRuntime.start)), \
                patch.object(PostgresRuntime, 'stop', increment('runtime_stops', PostgresRuntime.stop)):
            owner.start()
            owner.core.close = increment('core_closes', owner.core.close)
            if owner.core._embedder is None:
                raise RuntimeError('installed_model_required')
            client = ServiceClient(owner.endpoint_file, owner.credential_ref)
            calls = []
            snapshots = owner.core.source.snapshots
            def counted(root, cancel, *, excluded_roots=()):
                calls.append(canonical_root(root))
                return snapshots(root, cancel, excluded_roots=excluded_roots)
            with patch.object(owner.core.source, 'snapshots', counted):
                jobs = client.start_catalog()
                wait_for(lambda: len(owner.scheduler.snapshot()) >= 4 and all(
                    job.state not in ('queued', 'running') for job in owner.scheduler.snapshot()))
                wait_for(lambda: all(source.catalogued_files == source.metadata_embedded_files == 1
                    for source in client.status().components.sources), seconds=10)
                # Explicitly make only this disposable test source unavailable.
                Path(sources[1]['path']).rename(base / 'b-offline')
                offline = client.start_catalog((sources[1]['id'],))[0]
                wait_for(lambda: any(job.job_id == offline and job.state == 'failed' for job in owner.scheduler.snapshot()))
                wait_for(lambda: any(source.source_id == sources[1]['id'] and source.state == 'unreachable'
                    and source.catalogued_files == 1 for source in client.status().components.sources), seconds=10)
                expected_generation = owner.index_generation()
                owner.probes.request()
                wait_for(lambda: client.status().index_generation == expected_generation, seconds=10)
                reference = owner.issue_credential(AccessContext('health-b', frozenset({'search', 'status'}), (sources[1]['id'],)))
                reader = ServiceClient(owner.endpoint_file, reference)
                cached = reader.search(SearchRequest('drawing', mode='literal')).results
                assert len(cached) == 1 and cached[0].source_id == sources[1]['id'] and cached[0].stale
                report['cached_metadata_searchable'] = True
                owner.probes.request('storage')
                wait_for(lambda: client.status().components.storage.checked_at is not None, seconds=10)
                from dataclasses import asdict
                storage = client.status().components.storage
                assert all(getattr(storage, name) is not None for name in
                    ('physical_owned_bytes', 'pgdata_bytes', 'wal_bytes', 'free_bytes', 'quota_bytes', 'reserved_bytes'))
                assert storage.physical_owned_bytes >= storage.pgdata_bytes >= storage.wal_bytes > 0
                assert storage.free_bytes > 0 and storage.quota_bytes > storage.physical_owned_bytes
                report.update(physical_storage_measured=True, storage_observation=asdict(storage))
                # Separate background probes from the server's status read path.
                # These are the actual owner probes and are drained before PG.
                owner.probes.close()
                assert not any(thread.name.startswith('atom-health-') and thread.is_alive()
                               for thread in threading.enumerate())
                attempts = len(calls)
                def forbidden(*args, **kwargs):
                    raise RuntimeError('status_probe_forbidden')
                with patch.object(owner.runtime, 'status', forbidden), \
                        patch.object(owner.runtime, 'open_connection', forbidden), \
                        patch.object(owner.runtime, 'open_health_connection', forbidden):
                    observations = [reader.status() for _ in range(20)]
                    owner_health = client.status()
                assert len(calls) == attempts
                assert all(value.readiness == 'degraded' and value.index_generation == expected_generation
                    and value.config_version.revision == settings.revision and len(value.components.sources) == 1
                    and value.components.sources[0].source_id == sources[1]['id']
                    and value.components.sources[0].catalogued_files == 1 for value in observations)
                observed = observations[0].components.sources[0]
                assert observed.checked_at != observed.catalog_counts_checked_at
                assert all(job.total is None and job.processed == job.succeeded + job.skipped + job.errors
                           for job in owner_health.components.jobs)
                report.update(nonprobing_status=True, scope_before_aggregation=True, truthful_counts=True,
                    revision_generation=True, source_attempts=attempts, distinct_fact_timestamps=True,
                    probes_drained=True, queue_identity=id(owner.scheduler))
            attached = open_service_owner(owner.profile_dir, owner.model_dir)
            try:
                attached.start()
                assert attached.attached and attached.core is None and attached.scheduler is None and attached.probes is None
            finally:
                attached.close()
            child_report = base / 'second-launch.json'
            command = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'atom_search']
            child = subprocess.run(command + ['--service-attach-test', str(child_report), '--data-dir',
                str(owner.profile_dir), '--model-dir', str(owner.model_dir)],
                timeout=min(90, max(1, deadline - time.monotonic())), creationflags=subprocess.CREATE_NO_WINDOW)
            if child.returncode:
                raise RuntimeError('health_second_launch_failed')
            second = json.loads(child_report.read_bytes())
            assert (second['ok'] and second['pid'] != os.getpid()
                and second['installation_id'] == owner.installation_id and second['generation'] == owner.generation)
            report.update(second_launch=True, one_owner=True, one_queue=id(owner.scheduler) == report['queue_identity'])
            owner.close()
            report['clean_stop'] = owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
            report['ok'] = report['clean_stop'] and all(report[name] == 1 for name in
                ('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes'))
    except Exception as failure:
        report['error_code'] = getattr(failure, 'code', type(failure).__name__)
    finally:
        if owner is not None:
            try:
                owner.close()
                report['clean_stop'] = (owner.runtime is not None and owner.runtime.status().state == 'stopped'
                    and owner.runtime._claimed_process() is None)
            except Exception as failure:
                report['cleanup_error'] = getattr(failure, 'code', type(failure).__name__)
                report['ok'] = False
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def _wait_project_support(owner, service, *, seconds=30):
    """Wait for a real background observation; never write the health cache."""
    owner.probes.request('database')
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        snapshot = service.status()
        if {'project_search', 'project_aliases'} <= set(snapshot.capabilities):
            return snapshot
        time.sleep(.02)
    raise RuntimeError('project_readiness_not_observed')


def run_project_search_service_test(report_path: Path, *, data_dir: Path, model_dir: Path | None = None) -> int:
    """The packaged shared corpus, real public API/PG and no installed model."""
    import platform
    import os
    import threading
    from types import SimpleNamespace
    from ._postgres_proof import _identity
    from .project_checks import load_project_corpus, project_corpus_path, run_project_search_checks
    from .service.owner import open_service_owner
    from .service.client import ServiceClient
    from .service.contracts import AccessContext
    from atom_search_core.source import FileSystemSource
    base, destination = Path(data_dir).absolute(), Path(report_path).absolute()
    report = dict(ok=False, execution_mode='frozen' if getattr(sys, 'frozen', False) else 'source',
        scenario='project-golden', backend='postgresql', factory='open_service_owner', clean_stop=False)
    owner, clients, started = None, [], time.monotonic()
    allow_publication = threading.Event()
    try:
        if base.exists() or destination.exists() or destination.is_relative_to(base):
            raise RuntimeError('disposable_profile_must_be_new')
        folder = project_corpus_path()
        corpus = load_project_corpus(folder)
        manifest = corpus['manifest']
        report.update(_identity(), corpus_sha256=corpus['digest'], corpus_files=len(manifest['files']),
            corpus_queries=len(corpus['queries']), hardware=dict(os=platform.platform(),
                machine=platform.machine(), processor=platform.processor(), logical_cpus=os.cpu_count()), phases=[])
        base.mkdir(parents=True)
        sources = [dict(id=item['id'], label=item['label'], enabled=True,
                        path=str(base / 'sources' / item['directory'])) for item in manifest['sources']]
        roots = {source['id']: Path(source['path']) for source in sources}
        for item in manifest['files']:
            target = roots[item['source_id']].joinpath(*item['path'].split('/'))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((folder / item['file']).read_bytes())
        owner = open_service_owner(base / 'profile', model_dir or base / 'missing-model', automatic_catalog=False)
        snapshot = owner.repository.snapshot()
        candidate = snapshot.settings
        candidate.update(sources=sources, projects=manifest['projects'])
        candidate['search']['project_aliases'] = manifest['aliases']
        owner.coordinator.owner_apply(candidate, snapshot.revision)
        def endpoints():
            pg = owner.runtime.status().endpoint if owner.runtime is not None else None
            http = (SimpleNamespace(host='127.0.0.1', port=owner._server.server_address[1])
                    if owner._server is not None else None)
            return pg, http
        def reader():
            reference = owner.issue_credential(AccessContext('golden-reader',
                frozenset({'search', 'fetch', 'status'}), tuple(manifest['allowed_source_ids'])))
            client = ServiceClient(owner.endpoint_file, reference)
            clients.append(client)
            return client
        def check(service, phase):
            pending = bool(owner.core.projection.pending_context(1))
            value = run_project_search_checks(service, folder, phase=phase,
                projection_state='pending' if pending else 'current')
            report['phases'].append(value)
            return value
        def backfill():
            allow_publication.set()
            stop = threading.Event()
            timer = threading.Timer(90, stop.set)
            timer.daemon = True
            timer.start()
            try:
                owner.scheduler.queue_context(manual=True)
                owner.scheduler.wait_embeddings(cancel=stop)
                assert not stop.is_set() and not owner.core.projection.pending_context(1)
            finally:
                timer.cancel()
        with owned_loopback_only(endpoints):
            owner.start()
            assert owner.core._embedder is None, 'golden lexical proof requires the declared missing model'
            original = owner.core.index_context_batch
            def controlled_publication(*args, cancel=None, **kwargs):
                # Hold the real worker's publication to exercise CURRENT SQL
                # canonical fallback, including scoped alias-only/direct-root.
                while not allow_publication.wait(.02):
                    if cancel is not None and cancel.is_set():
                        return dict(attempted=0, published=0, cancelled=True)
                return original(*args, cancel=cancel, **kwargs)
            try:
                with patch.object(owner.core, 'index_context_batch', controlled_publication):
                    for source in sources:
                        assert owner.core.scan(source['path'])['status'] == 'complete'
                    service = reader()
                    admin = ServiceClient(owner.endpoint_file, owner.credential_ref)
                    clients.append(admin)
                    _wait_project_support(owner, service)
                    assert owner.core.projection.pending_context(1)
                    check(service, 'baseline')
                    changed = admin.settings_get()
                    changed.settings['sources'][0]['label'] += ' обновлённый'
                    admin.settings_apply(changed.settings, changed.revision)
                    _wait_project_support(owner, service)
                    assert owner.core.projection.pending_context(1)
                    check(service, 'pending')
                    backfill()
                    check(service, 'baseline')
                    allow_publication.clear()
                    changed = admin.settings_get()
                    changed.settings['search']['project_aliases'] = []
                    admin.settings_apply(changed.settings, changed.revision)
                    _wait_project_support(owner, service)
                    assert owner.core.projection.pending_context(1)
                    check(service, 'revoked-pending')
                    backfill()
                    current = check(service, 'revoked-current')
            finally:
                allow_publication.set()
            revision = owner.core.projection.configuration()[0]
            for client in clients:
                client.close()
            clients.clear()
            owner.close()
            assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
            owner = open_service_owner(base / 'profile', base / 'missing-model', automatic_catalog=False)
            owner.start()
            service = reader()
            _wait_project_support(owner, service)
            restarted = check(service, 'revoked-current')
            assert [row.get('public_result_ids') for row in current['cases']] == [
                row.get('public_result_ids') for row in restarted['cases']]
            assert owner.core.projection.configuration()[0] == revision
            def no_documents(*args, **kwargs):
                raise RuntimeError('project_golden_document_probe')
            with patch.object(FileSystemSource, 'snapshots', no_documents):
                identity = owner.registry.resolve((sources[0]['id'],), allowed=(sources[0]['id'],)).source_ids[0]
                owner.core.catalog.finish_root(identity, 'unavailable')
                check(service, 'offline')
            service.close()
            clients.clear()
            owner.close()
            assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
        report['clean_stop'] = True
        if not all(phase['ok'] for phase in report['phases']):
            raise RuntimeError('project_golden_failed')
        report.update(ok=True, clean_stop=True, model_missing_lexical=True,
            cached_readiness=True, pending_scoped_alias=True, restart_public_ids=True,
            no_document_reread=True, catalog_schema=5, settings_schema=3)
    except Exception as failure:
        known = {'disposable_profile_must_be_new', 'project_readiness_not_observed',
                 'project_golden_failed', 'project_golden_document_probe'}
        report['error_code'] = getattr(failure, 'code', str(failure)
            if type(failure) is RuntimeError and str(failure) in known else type(failure).__name__)
    finally:
        allow_publication.set()
        for client in clients:
            client.close()
        if owner is not None:
            try:
                owner.close()
            except Exception as failure:
                report['cleanup_error'] = getattr(failure, 'code', type(failure).__name__)
                report['ok'] = False
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def run_project_retrieval_service_test(report_path: Path, *, data_dir: Path, model_dir: Path | None = None) -> int:
    """Public-owner project retrieval on fictional files, then cached SQL only."""
    from dataclasses import asdict
    import threading
    from types import SimpleNamespace
    from atom_search.paths import model_path
    from atom_search.service.owner import open_service_owner
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import AccessContext, SearchRequest, ServiceError
    from atom_search_core.project_context import INPUT_SCHEMA
    from atom_search_core.source import FileSystemSource
    base, destination = Path(data_dir).absolute(), Path(report_path).absolute()
    report = dict(ok=False, execution_mode='frozen' if getattr(sys, 'frozen', False) else 'source',
        scenario='project-retrieval', backend='postgresql', factory='open_service_owner', clean_stop=False)
    owner, clients, started = None, [], time.monotonic()
    try:
        if base.exists() or destination.exists() or destination.is_relative_to(base):
            raise RuntimeError('disposable_profile_must_be_new')
        base.mkdir(parents=True)
        sources = [dict(id=str(uuid.uuid4()), label=label, path=str(base / label), enabled=True)
                   for label in ('shared', 'direct', 'private')]
        paths = (('Чёто до/Зенитные фонари ПМ/хрень.dwg',
                  'Чёто до/Зенитные фонари ПМ/Спецификация.pdf',
                  'Поставка материалов/Спецификация.pdf', 'Пермские моторы спецификация.txt'),
                 ('Спецификация прямая.pdf',), ('Спецификация.pdf',))
        for source, names in zip(sources, paths):
            for relative in names:
                path = Path(source['path']).joinpath(*relative.split('/'))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fictional metadata fixture; body never searched')
        pm, supply, private = (str(uuid.uuid4()) for _ in range(3))
        owner = open_service_owner(base / 'profile', model_dir or model_path(), automatic_catalog=False)
        current = owner.repository.snapshot()
        candidate = current.settings
        candidate['sources'] = sources
        candidate['projects'] = [
            dict(id=pm, canonical_name='Пермские моторы', source_ids=[s['id'] for s in sources[:2]], bindings=[
                dict(source_id=sources[0]['id'], path_prefix='Чёто до/Зенитные фонари ПМ', confirmed=True, basis='QA'),
                dict(source_id=sources[1]['id'], path_prefix='', confirmed=True, basis='QA')]),
            dict(id=supply, canonical_name='Поставка материалов', source_ids=[sources[0]['id']], bindings=[
                dict(source_id=sources[0]['id'], path_prefix='Поставка материалов', confirmed=True, basis='QA')]),
            dict(id=private, canonical_name='Закрытый проект', source_ids=[sources[2]['id']], bindings=[
                dict(source_id=sources[2]['id'], path_prefix='', confirmed=True, basis='QA')])]
        candidate['search']['project_aliases'] = [dict(id=str(uuid.uuid4()), project_id=project['id'],
            source_ids=project['source_ids'], variants=['ПМ'], confirmed=True, basis='QA')
            for project in candidate['projects']]
        owner.coordinator.owner_apply(candidate, current.revision)
        def endpoints():
            pg = owner.runtime.status().endpoint if owner.runtime is not None else None
            http = (SimpleNamespace(host='127.0.0.1', port=owner._server.server_address[1])
                    if owner._server is not None else None)
            return pg, http
        with owned_loopback_only(endpoints):
            owner.start()
            assert owner.core.project_search_supported and owner.core._embedder is not None
            assert owner.core.input_schema == INPUT_SCHEMA
            for source in sources:
                assert owner.core.scan(source['path'])['status'] == 'complete'
            admin = ServiceClient(owner.endpoint_file, owner.credential_ref)
            reference = owner.issue_credential(AccessContext('project-qa', frozenset({'search', 'fetch', 'status'}),
                                                         tuple(source['id'] for source in sources[:2])))
            reader = ServiceClient(owner.endpoint_file, reference)
            clients.extend((admin, reader))
            # Automatic cataloguing is disabled: these metadata rows have not
            # reached the existing context/embedding queue yet.
            alias_only = reader.search(SearchRequest('ПМ', source_ids=(sources[1]['id'],), mode='lexical'))
            assert len(alias_only.results) == 1
            assert (alias_only.results[0].name, alias_only.results[0].project_id,
                    alias_only.results[0].matched_fields) == (paths[1][0], pm, ('alias',))
            assert alias_only.query_interpretation.project_id is None and len(alias_only.applied_aliases) == 1
            stop = threading.Event()
            timer = threading.Timer(90, stop.set)
            timer.daemon = True
            timer.start()
            try:
                owner.scheduler.queue_context(manual=True)
                owner.scheduler.wait_embeddings(cancel=stop)
                assert not stop.is_set()
                assert all(job.state == 'completed' for job in owner.scheduler.snapshot())
            finally:
                timer.cancel()
            query = 'Пермские моторы зенитные фонари'
            reverse = reader.search(SearchRequest(query, mode='lexical'))
            assert {row.name for row in reverse.results} == {'хрень.dwg', 'Спецификация.pdf'}
            assert reverse.query_interpretation.project_id is None
            assert reverse.query_interpretation.original_query == query
            assert all(row.project_id == pm and {'path', 'project'} <= set(row.matched_fields)
                       for row in reverse.results)
            mixed = reader.search(SearchRequest('Пермские моторы спецификация', mode='lexical'))
            assert mixed.results[0].relative_path == paths[0][1] and mixed.results[-1].project_id is None
            direct = reader.search(SearchRequest('Пермские моторы спецификация',
                source_ids=(sources[1]['id'],), mode='lexical'))
            assert len(direct.results) == 1 and direct.results[0].name == paths[1][0]
            ambiguous = reader.search(SearchRequest('ПМ спецификация', source_ids=(sources[0]['id'],), mode='lexical'))
            assert {option.project_id for option in ambiguous.ambiguities[0].options} == {pm, supply}
            assert all(option.source_ids == (sources[0]['id'],) for option in ambiguous.ambiguities[0].options)
            selected = reader.search(SearchRequest('ПМ спецификация', project_id=pm))
            assert selected.results[0].relative_path == paths[0][1]
            assert {row.project_id for row in selected.results} == {pm}
            assert not selected.ambiguities and selected.query_interpretation.original_query == 'ПМ спецификация'
            assert reader.search(SearchRequest('Спецификация', source_ids=())).results == ()
            for request in (SearchRequest('Спецификация', source_ids=(sources[2]['id'],)),
                            SearchRequest('Спецификация', project_id=private)):
                try:
                    reader.search(request)
                    raise AssertionError('project source scope was not enforced')
                except ServiceError as failure:
                    assert failure.code in ('source_scope_denied', 'project_scope_denied')
            assert not {'content', 'snippets', 'text'} & asdict(selected.results[0]).keys()
            def no_crawl(*args, **kwargs):
                raise RuntimeError('project_retrieval_reread_documents')
            with patch.object(FileSystemSource, 'snapshots', no_crawl):
                before = admin.settings_get()
                changed = before.settings
                changed['search']['project_aliases'] = []
                admin.settings_apply(changed, before.revision)
                pending = reader.search(SearchRequest(query, mode='lexical'))
                assert {row.name for row in pending.results} == {'хрень.dwg', 'Спецификация.pdf'}
                assert not reader.search(SearchRequest('ПМ', project_id=supply, mode='lexical')).results
                assert reader.search(SearchRequest('ПМ спецификация', mode='lexical')).applied_aliases == ()
                identity = owner.registry.resolve((sources[0]['id'],), allowed=(sources[0]['id'],)).source_ids[0]
                owner.core.catalog.finish_root(identity, 'unavailable')
                cached = reader.search(SearchRequest(query, source_ids=(sources[0]['id'],), mode='lexical'))
                assert cached.results and all(row.stale for row in cached.results)
                _wait_project_support(owner, reader)
            with owner.runtime.open_connection() as connection:
                assert connection.execute('SELECT max(version) FROM atom_search.schema_migrations').fetchone() == (5,)
            for client in clients:
                client.close()
            clients.clear()
            owner.close()
            assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
        report.update(ok=True, clean_stop=True, catalog_schema=5, input_schema=INPUT_SCHEMA,
            full_name_reverse=True, mixed_fields_ranked=True, direct_root=True, pending_alias_direct_root=True,
            explicit_project_isolation=True, scoped_ambiguity=True, selection_keeps_query=True,
            source_isolation=True, pending_canonical_fallback=True, alias_revoke_no_stale_hits=True,
            offline_cached_metadata=True, no_document_reread=True, project_capabilities_ready=True,
            current_input_schema=True, metadata_only=True)
    except Exception as failure:
        known = {'disposable_profile_must_be_new', 'project_retrieval_reread_documents'}
        report['error_code'] = getattr(failure, 'code',
            str(failure) if type(failure) is RuntimeError and str(failure) in known else type(failure).__name__)
    finally:
        for client in clients:
            client.close()
        if owner is not None:
            try:
                owner.close()
            except Exception as failure:
                report['cleanup_error'] = getattr(failure, 'code', type(failure).__name__)
                report['ok'] = False
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def run_project_context_service_test(report_path: Path, *, data_dir: Path, model_dir: Path | None = None) -> int:
    """Preserve a frozen v4 seed, backfill v5 on the owned queue, revoke and restart."""
    import threading
    from copy import deepcopy
    from atom_search.paths import model_path
    from atom_search.service.owner import open_service_owner
    from atom_search.service.contracts import SearchRequest
    from atom_search.service.health import database_probe
    from atom_search_core.project_context import INPUT_SCHEMA
    from atom_search_core.models import SearchScope
    from atom_search_core.source import FileSystemSource
    base, destination = Path(data_dir).absolute(), Path(report_path).absolute()
    profile = base / 'application-self-test/profile'
    report = dict(ok=False, execution_mode='frozen' if getattr(sys, 'frozen', False) else 'source',
        scenario='project-context', backend='postgresql', factory='open_service_owner', clean_stop=False)
    owner, started = None, time.monotonic()
    try:
        if destination.exists() or destination.is_relative_to(base):
            raise RuntimeError('disposable_report_must_be_new')
        seed_path = base / 'schema4-seed.json'
        if not seed_path.is_file() or not (profile / 'settings.json').is_file():
            raise RuntimeError('project_context_seed_required')
        with seed_path.open('rb') as stream:
            seed_raw = stream.read(262145)
        if len(seed_raw) > 262144:
            raise RuntimeError('project_context_seed_invalid')
        seed = json.loads(seed_raw)
        if (seed.get('ok') is not True or seed.get('execution_mode') != 'frozen'
                or seed.get('clean_stop') is not True or seed.get('schema_version') != 4
                or len(seed.get('public_file_ids', {})) != 3
                or not seed.get('model_key', '').endswith('|filename-parent-v1')):
            raise RuntimeError('project_context_seed_invalid')
        selected_model = model_dir or model_path()
        def no_crawl(*args, **kwargs):
            raise RuntimeError('project_context_reread_documents')
        def sql(query, parameters=()):
            with owner.runtime.open_connection() as connection:
                return connection.execute(query, parameters).fetchall()
        def vectors(key):
            return sql('''SELECT file_id,file_revision,input_hash,embedding::text
                FROM atom_search.file_embeddings WHERE model_key=%s ORDER BY file_id''', (key,))
        def backfill():
            cancel = threading.Event()
            timer = threading.Timer(90, cancel.set)
            timer.daemon = True
            timer.start()
            try:
                owner.scheduler.queue_context(manual=True)
                owner.scheduler.wait_embeddings(cancel=cancel)
                assert not cancel.is_set()
                assert all(job.state == 'completed' for job in owner.scheduler.snapshot() if job.phase == 'embed')
            finally:
                timer.cancel()
        with patch.object(FileSystemSource, 'snapshots', no_crawl):
            owner = open_service_owner(profile, selected_model, automatic_catalog=False)
            owner.start()
            core, facade = owner.core, owner.facade(owner.owner_access)
            snapshot = facade.settings_get()
            source = snapshot.settings['sources'][0]
            assert snapshot.schema_version == 3 and source['id'] == seed['public_source_id']
            assert sql('SELECT max(version) FROM atom_search.schema_migrations') == [(5,)]
            with owner.runtime.open_connection() as connection:
                assert _index_fingerprint(connection) == seed['index_fingerprint']
                assert _public_id_snapshot(connection, source['path']) == (source['id'], seed['public_file_ids'])
            old_vectors = vectors(seed['model_key'])
            assert len(old_vectors) == 3 and core._embedder is not None and core._preparer is not None
            assert core._model_key.endswith('|' + INPUT_SCHEMA) and core.catalog._model.manifest_digest == seed['model_manifest_digest']
            candidate = deepcopy(snapshot.settings)
            project = str(uuid.uuid4())
            candidate['sources'][0]['label'] = 'Архив чертежей'
            candidate['projects'] = [dict(id=project, canonical_name='Пермские моторы', source_ids=[source['id']],
                bindings=[dict(source_id=source['id'], path_prefix='', confirmed=True, basis='QA owner binding')])]
            candidate['search']['project_aliases'] = [dict(id=str(uuid.uuid4()), project_id=project,
                source_ids=[source['id']], variants=['ПМ'], confirmed=True, basis='QA owner dictionary')]
            applied = facade.settings_apply(candidate, snapshot.revision)
            assert applied.settings['sources'] == [dict(source, label='Архив чертежей')]
            scope = owner.registry.resolve((source['id'],), allowed=(source['id'],))
            assert facade.search(SearchRequest('КМ101', mode='literal', source_ids=(source['id'],))).results
            _wait_project_support(owner, facade)
            backfill()
            def context_rows():
                return sql('''SELECT pc.context_revision,pc.context_text,pc.raw_context_hash,
                    pc.prepared_text,pc.input_hash,pc.token_count,e.input_hash,e.embedding::text
                    FROM atom_search.file_project_context pc JOIN atom_search.file_embeddings e ON e.file_id=pc.file_id
                    WHERE e.model_key=%s ORDER BY pc.file_id''', (core._model_key,))
            first = context_rows()
            assert len(first) == 3 and len(vectors(core._model_key)) == 3
            for revision, raw, raw_hash, text, digest, tokens, vector_hash, _ in first:
                prepared = core._preparer.prepare_input(raw, kind='passage')
                assert (text, digest, tokens) == (prepared.text, prepared.input_hash, prepared.token_count)
                assert raw_hash == hashlib.sha256(raw.encode('utf-8')).hexdigest()
                assert vector_hash == digest and 1 <= tokens <= 512 and 'aliases: пм' in raw
            before_revoke = applied.settings
            before_revoke['search']['project_aliases'] = []
            revoked = facade.settings_apply(before_revoke, applied.revision)
            observed = database_probe(owner, time.monotonic() + 3, threading.Event())
            assert observed['counts']['counts'][source['id']] == (3, 0)
            vector = tuple(json.loads(first[0][-1]))
            assert core.retrieval.vector_candidates(vector, model_key=core._model_key,
                scope=SearchScope(scope.source_ids), limit=10) == []
            backfill()
            current = context_rows()
            assert all(row[0] > first[0][0] and row[4] != old[4] and 'aliases: пм' not in row[1]
                       for row, old in zip(current, first))
            assert database_probe(owner, time.monotonic() + 3, threading.Event())['counts']['counts'][source['id']] == (3, 3)
            assert vectors(seed['model_key']) == old_vectors
            owner.close()
            assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
            owner = open_service_owner(profile, selected_model, automatic_catalog=False)
            owner.start()
            core, facade = owner.core, owner.facade(owner.owner_access)
            assert facade.settings_get() == revoked
            assert context_rows() == current and core.projection.pending_context(1, require_prepared=True) == []
            backfill()
            assert context_rows() == current and vectors(seed['model_key']) == old_vectors
            with owner.runtime.open_connection() as connection:
                assert _public_id_snapshot(connection, source['path']) == (source['id'], seed['public_file_ids'])
            owner.close()
            assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
        report.update(ok=True, clean_stop=True, catalog_schema=5, prior_catalog_schema=4,
            source_settings_preserved=True, public_ids_preserved=True, old_vectors_preserved=True,
            same_manifest_new_input_key=True, prepared_payload_verified=True, token_budget_verified=True,
            stale_vectors_ineligible=True, current_health_counts=True, queue_backfill=True,
            no_document_reread=True, restart_idempotent=True, project_capabilities_ready=True,
            context_revision=current[0][0], vectors=3,
            seed_sha256=hashlib.sha256(seed_raw).hexdigest())
    except Exception as failure:
        known = {'project_context_seed_required', 'project_context_seed_invalid',
                 'project_context_reread_documents', 'disposable_report_must_be_new'}
        report['error_code'] = getattr(failure, 'code',
            str(failure) if type(failure) is RuntimeError and str(failure) in known else type(failure).__name__)
    finally:
        if owner is not None:
            try:
                owner.close()
            except Exception as failure:
                report['cleanup_error'] = getattr(failure, 'code', type(failure).__name__)
                report['ok'] = False
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def run_project_settings_service_test(report_path: Path, *, data_dir: Path, model_dir: Path | None = None) -> int:
    """Upgrade a real old-EXE seed, apply scoped mappings, restart without a crawl.

    tools/check_project_settings.py creates the seed using the accepted prior
    executable's existing application self-test. Its SQL fingerprint/public IDs
    are compared before any indexing could conceal a migration regression.
    """
    from copy import deepcopy
    from atom_search.paths import model_path
    from atom_search.runtime import _windows
    from atom_search.service.owner import open_service_owner
    from atom_search.service.contracts import SearchRequest
    from atom_search.settings.repository import SettingsRepository, _read_json
    from atom_search.settings.schema import validate_settings
    from atom_search_core.source import FileSystemSource
    started = time.monotonic()
    base, destination = Path(data_dir).absolute(), Path(report_path).absolute()
    profile = base / 'application-self-test/profile'
    report = dict(ok=False, execution_mode='frozen' if getattr(sys, 'frozen', False) else 'source',
        scenario='project-settings', backend='postgresql', factory='open_service_owner', clean_stop=False)
    owner = None
    try:
        if destination.exists() or destination.is_relative_to(base):
            raise RuntimeError('disposable_report_must_be_new')
        seed_path = base / 'schema1-seed.json'
        if not seed_path.is_file() or not (profile / 'settings.json').is_file():
            raise RuntimeError('project_settings_seed_required')
        with seed_path.open('rb') as stream:
            seed_raw = stream.read(262145)
        if len(seed_raw) > 262144:
            raise RuntimeError('project_settings_seed_invalid')
        seed = json.loads(seed_raw)
        if (seed.get('ok') is not True or seed.get('clean_stop') is not True
                or seed.get('execution_mode') != 'frozen' or seed.get('schema_version') != 4
                or len(seed.get('public_file_ids', {})) != 3):
            raise RuntimeError('project_settings_seed_invalid')
        before, raw = _read_json(profile / 'settings.json', 65536)
        validate_settings(before, allow_legacy=True)
        if (before['schema_version'] != 1 or len(before['sources']) != 1
                or before['sources'][0]['id'] != seed['public_source_id']):
            raise RuntimeError('project_settings_seed_invalid')
        expected_migration = dict(before, schema_version=3, revision=before['revision'] + 1)
        expected_ids = (seed['public_source_id'], seed['public_file_ids'])
        selected_model = model_dir or model_path()
        def no_crawl(*args, **kwargs):
            raise RuntimeError('project_settings_reread_documents')
        with patch.object(FileSystemSource, 'snapshots', no_crawl):
            owner = open_service_owner(profile, selected_model, automatic_catalog=False)
            facade = owner.facade(owner.owner_access)
            snapshot = facade.settings_get()
            assert snapshot.settings == expected_migration and snapshot.schema_version == 3
            assert (profile / 'engine/settings-backups/schema1.json').read_bytes() == raw
            owner.start()
            def unchanged_catalog():
                with owner.runtime.open_connection() as connection:
                    assert _index_fingerprint(connection) == seed['index_fingerprint']
                    assert _public_id_snapshot(connection, before['sources'][0]['path']) == expected_ids
                    assert connection.execute('SELECT max(version) FROM atom_search.schema_migrations').fetchone()[0] == 5
                results = facade.search(SearchRequest('КМ101', mode='literal', source_ids=(expected_ids[0],))).results
                assert results and results[0].result_id == expected_ids[1]['КМ-101 узлы фасада.dwg']
                assert facade.fetch(results[0].result_id).match_kind == 'metadata'
                _wait_project_support(owner, facade)
            unchanged_catalog()
            candidate = deepcopy(snapshot.settings)
            project_id = str(uuid.uuid4())
            candidate['projects'] = [dict(id=project_id, canonical_name='Учебный проект',
                source_ids=[expected_ids[0]], bindings=[dict(source_id=expected_ids[0], path_prefix='',
                    confirmed=True, basis='Подтверждение владельца QA-проекта')])]
            candidate['search']['project_aliases'] = [dict(id=str(uuid.uuid4()), project_id=project_id,
                source_ids=[expected_ids[0]], variants=['УП'], confirmed=True, basis='QA owner dictionary')]
            assert facade.settings_validate(candidate, snapshot.revision).valid
            applied = facade.settings_apply(candidate, snapshot.revision)
            assert applied.schema_version == 3 and applied.revision == snapshot.revision + 1
            assert applied.settings['sources'] == before['sources']
            assert applied.settings['indexing'] == before['indexing']
            unchanged_catalog()
            owner.close()
            assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
            owner = open_service_owner(profile, selected_model, automatic_catalog=False)
            facade = owner.facade(owner.owner_access)
            assert facade.settings_get() == applied
            owner.start()
            unchanged_catalog()
            owner.close()
            assert owner.runtime.status().state == 'stopped' and owner.runtime._claimed_process() is None
            assert SettingsRepository(profile, model_dir=selected_model).load() == applied
        report.update(migration_published_once=True, original_bytes_preserved=True,
            owner_apply=True, restart_idempotent=True, source_settings_preserved=True,
            catalog_fingerprint_preserved=True, public_ids_preserved=True,
            scoped_metadata_search=True, no_document_reread=True, project_capabilities_ready=True,
            clean_stop=True, prior_revision=before['revision'], migration_revision=snapshot.revision,
            applied_revision=applied.revision, restarted_revision=applied.revision,
            settings_schema=3, catalog_schema=5,
            seed_sha256=hashlib.sha256(seed_raw).hexdigest(), original_settings_sha256=hashlib.sha256(raw).hexdigest())
        report['ok'] = True
    except Exception as failure:
        known = {'project_settings_seed_required', 'project_settings_seed_invalid',
                 'disposable_report_must_be_new', 'project_settings_reread_documents'}
        report['error_code'] = getattr(failure, 'code',
            str(failure) if type(failure) is RuntimeError and str(failure) in known else type(failure).__name__)
    finally:
        if owner is not None:
            try:
                owner.close()
            except Exception as failure:
                report['cleanup_error'] = getattr(failure, 'code', type(failure).__name__)
                report['ok'] = False
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def run_service_attach_test(report_path: Path, *, data_dir: Path, model_dir: Path | None = None) -> int:
    """A separate hidden process attaches; it cannot bootstrap another host."""
    import os
    from atom_search.paths import model_path
    from atom_search.service.owner import open_service_owner
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import SearchRequest
    from atom_search import search_runtime
    report = {'ok': False, 'pid': os.getpid()}
    attached = None
    try:
        def forbidden(*a, **k):
            raise RuntimeError('second_owner_construction_forbidden')
        with patch.object(search_runtime, 'open_search_service', forbidden):
            attached = open_service_owner(data_dir, model_dir or model_path())
            attached.start()
            assert attached.attached and attached.core is None
            client = ServiceClient(attached.endpoint_file, attached.credential_ref)
            snapshot = client.settings_get()
            assert client.settings_validate(snapshot.settings, snapshot.revision).valid
            results = client.search(SearchRequest('drawing', mode='literal')).results
            assert len(results) == 2 and client.fetch(results[0].result_id).match_kind == 'metadata'
            report.update(ok=True, installation_id=attached.installation_id, generation=attached.generation)
    except Exception as failure:
        report['error_code'] = getattr(failure, 'code', type(failure).__name__)
    finally:
        if attached is not None:
            attached.close()
    with Path(report_path).open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def run_self_test(report_path: Path, *, data_dir: Path | None = None,
                  model_dir: Path | None = None) -> int:
    from atom_search.search_runtime import open_search_service
    from atom_search.paths import model_path
    from atom_search._postgres_proof import _identity
    from atom_search_core import __version__
    from atom_search_core.source import canonical_root
    from atom_search.settings.repository import SettingsRepository
    from atom_search.service.sources import SourceRegistry
    started = time.monotonic()
    report = dict(_identity(), ok=False, model_ready=False, backend='postgresql',
                  core_version=__version__, factory='open_search_service', clean_stop=False,
                  execution_mode='frozen' if getattr(sys, 'frozen', False) else 'source')
    service = None
    temporary = None
    try:
        if data_dir is None:
            temporary = Path(tempfile.mkdtemp(prefix='atom-qa-'))
            base = temporary
        else:
            base = Path(data_dir) / 'application-self-test'
            base.mkdir(parents=True, exist_ok=True)
        source = base / 'fictional-documents'
        source.mkdir(exist_ok=True)
        names = ('Контракт монтажных работ.docx', 'КМ-101 узлы фасада.dwg',
                 'График отпусков сотрудников.xlsx')
        for name in names:
            path = source / name
            if not path.exists():
                path.touch()
        endpoint = None
        # This bounded QA scenario owns its fictional root. Stage it before PG
        # starts, so the factory cannot bind the default corporate source while
        # the scenario actually scans a different root.
        profile = base / 'profile'
        prior_settings = (profile / 'settings.json').exists()
        repository = SettingsRepository(profile, model_dir=model_dir or model_path())
        snapshot = repository.load()
        selected = snapshot.settings['sources']
        if len(selected) != 1 or canonical_root(selected[0]['path']) != canonical_root(str(source)):
            if prior_settings:
                raise RuntimeError('self_test_settings_scope_mismatch')
            candidate = snapshot.settings
            candidate['sources'] = [dict(id=str(uuid.uuid4()), path=str(source), label='', enabled=True)]
            repository.commit(candidate, snapshot.revision)
        with owned_loopback_only(lambda: endpoint):
            service = open_search_service(profile, model_dir or model_path())
            endpoint = service.runtime.status().endpoint
            report['installation_id'] = endpoint.installation_id
            report['model_ready'] = service.status()['semantic_ready']
            with service.runtime.open_connection() as connection:
                report['prior_files'] = connection.execute('SELECT count(*) FROM atom_search.files').fetchone()[0]
                report['catalog_id'] = connection.execute("SELECT id FROM atom_search.catalogs WHERE state='active'").fetchone()[0]
                prior_count, prior_dimension = connection.execute('SELECT count(*),min(public.vector_dims(embedding)) FROM atom_search.file_embeddings').fetchone()
                prior_models = connection.execute('SELECT model_key,manifest_digest FROM atom_search.embedding_models').fetchall()
                report['prior_index_fingerprint'] = _index_fingerprint(connection)
            model = service.catalog._model
            report.update(prior_vector_count=prior_count, prior_dimension=prior_dimension)
            prior_literal = service.search('КМ101') if report['prior_files'] else []
            prior_semantic = service.search('договор с заказчиком') if report['prior_files'] else []
            report['prior_index_verified'] = (report['prior_files'] == 3 and prior_count == 3
                and prior_dimension == 384 and model is not None
                and prior_models == [(model.model_key, model.manifest_digest)]
                and bool(prior_literal) and prior_literal[0].name == names[1]
                and bool(prior_semantic) and prior_semantic[0].name == names[0])
            if report['prior_files'] and not report['prior_index_verified']:
                raise RuntimeError('self_test_prior_index_invalid')
            if not report['model_ready']:
                raise RuntimeError('self_test_model_unavailable')
            repository.bind_runtime_budget(service.runtime.storage_budget)
            registry = SourceRegistry(repository, service.public_ids)
            registry.activate()
            with service.runtime.open_connection() as connection:
                report['prior_schema_version'] = connection.execute(
                    'SELECT max(version) FROM atom_search.schema_migrations').fetchone()[0]
                report['prior_public_source_id'], report['prior_public_file_ids'] = _public_id_snapshot(connection, source)
            scope = registry.resolve(None, allowed=(report['prior_public_source_id'],))
            for result_id in report['prior_public_file_ids'].values():
                file_id = registry.internal_file_id(result_id, scope=scope)
                if file_id is None or service.catalog.get_file(file_id, scope=scope) is None:
                    raise RuntimeError('self_test_scoped_metadata_invalid')
            scan = service.scan(str(source))
            report.update(scan_status=scan['status'], files=scan['files'])
            service.index_embeddings()
            with service.runtime.open_connection() as connection:
                report['server_version'] = connection.execute('SHOW server_version').fetchone()[0]
                report['extension_version'] = connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]
                report['schema_version'] = connection.execute('SELECT max(version) FROM atom_search.schema_migrations').fetchone()[0]
                count, dimension = connection.execute('SELECT count(*),min(public.vector_dims(embedding)) FROM atom_search.file_embeddings').fetchone()
                model_key, model_digest = connection.execute('SELECT model_key,manifest_digest FROM atom_search.embedding_models').fetchone()
                report['index_fingerprint'] = _index_fingerprint(connection)
                report['public_source_id'], report['public_file_ids'] = _public_id_snapshot(connection, source)
            report.update(vector_count=count, dimension=dimension, model_key=model_key,
                          model_manifest_digest=model_digest)
            literal, semantic = service.search('КМ101'), service.search('договор с заказчиком')
            report['literal_result'] = literal[0].name if literal else None
            report['semantic_result'] = semantic[0].name if semantic else None
            report['public_identity_preserved'] = (
                report['public_source_id'] == report['prior_public_source_id'] and
                (not report['prior_files'] or report['public_file_ids'] == report['prior_public_file_ids']))
            report['ok'] = (scan['status'] == 'complete' and count == 3 and dimension == 384
                and report['literal_result'] == names[1] and report['semantic_result'] == names[0]
                and report['server_version'] == '18.6' and report['extension_version'] == '0.8.7'
                and report['schema_version'] == 5 and report['public_identity_preserved']
                and len(report['public_file_ids']) == 3)
    except Exception as error:
        # Stable host errors only; no connection strings, credentials or SQL.
        report['error'] = getattr(error, 'code', type(error).__name__)
    finally:
        if service is not None:
            try:
                service.close()
                report['clean_stop'] = service.runtime.status().state == 'stopped'
            except Exception as error:
                report['cleanup_error'] = getattr(error, 'code', type(error).__name__)
        if not report['clean_stop']:
            report['ok'] = False
        if temporary is not None and report['clean_stop']:
            from atom_search.runtime import _windows
            for path in temporary.rglob('*'):
                _windows.reject_reparse(path)
            shutil.rmtree(temporary)
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['ok'] else 1


def _public_id_snapshot(connection, root):
    from atom_search_core.source import canonical_root
    source = connection.execute("""SELECT p.source_uuid::text,s.id FROM atom_search.source_public_ids p
        JOIN atom_search.sources s ON s.id=p.source_id JOIN atom_search.catalogs c ON c.id=s.catalog_id
        WHERE c.state='active' AND s.path=%s""", (canonical_root(str(root)),)).fetchone()
    if source is None or str(uuid.UUID(source[0])) != source[0]:
        raise RuntimeError('self_test_source_uuid_invalid')
    files = connection.execute("""SELECT f.relative_path,p.file_uuid::text FROM atom_search.files f
        JOIN atom_search.file_public_ids p ON p.file_id=f.id WHERE f.source_id=%s ORDER BY f.id LIMIT 4""",
        (source[1],)).fetchall()
    if any(str(uuid.UUID(identity)) != identity for _, identity in files):
        raise RuntimeError('self_test_file_uuid_invalid')
    return source[0], dict(files)


def _index_fingerprint(connection):
    """Bounded QA seed identity before any scan can repair its metadata/vectors."""
    rows = connection.execute("""SELECT f.id,s.path,f.relative_path,f.name,f.size,f.modified_ns,
        f.revision,e.model_key,e.file_revision,e.input_hash,e.embedding::text
        FROM atom_search.files f JOIN atom_search.sources s ON s.id=f.source_id
        JOIN atom_search.catalogs c ON c.id=s.catalog_id
        LEFT JOIN atom_search.file_embeddings e ON e.file_id=f.id
        WHERE c.state='active' ORDER BY f.id,e.model_key LIMIT 4""").fetchall()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def run_postgres_self_test(report_path: Path, *, data_dir: Path,
                           runtime_dir: Path | None = None,
                           lifecycle: str = "oneshot", run_token: str | None = None) -> int:
    """Owned synthetic runtime/restore proof; retained QA requires a run token."""
    from atom_search.paths import resource_path
    from atom_search._postgres_proof import run
    return run(report_path, data_dir=data_dir,
               runtime_dir=runtime_dir or resource_path("postgresql"),
               lifecycle=lifecycle, run_token=run_token)
