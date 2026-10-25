"""Ownership transfer, scoped metadata and drain public contracts."""
import importlib
import json
from pathlib import Path
import threading
import time

import pytest


def test_owner_prepares_settings_without_pg_or_inference(tmp_path, monkeypatch):
    import atom_search.search_runtime as host
    def forbidden(*a, **k):
        pytest.fail('early settings constructed PG or inference')
    monkeypatch.setattr(host, '_load_model', forbidden)
    monkeypatch.setattr(host, 'PostgresRuntime', forbidden)
    assert importlib.util.find_spec('atom_search.service.owner') is not None, 'ServiceOwner missing'
    owner = importlib.import_module('atom_search.service.owner').open_service_owner(tmp_path, tmp_path / 'missing')
    assert owner.repository.snapshot().schema_version == 3
    assert owner.core is None
    owner.close()


def test_starting_owner_prevents_second_settings_load_and_recovery(tmp_path, monkeypatch):
    from atom_search.service.owner import open_service_owner
    from atom_search.service.contracts import ServiceError
    from atom_search.settings.repository import SettingsRepository
    first = open_service_owner(tmp_path, tmp_path / 'missing')
    def forbidden(*a, **k):
        pytest.fail('a second starting owner entered settings load/recovery')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(SettingsRepository, 'load', forbidden)
            patch.setattr(SettingsRepository, '_load_prepared', forbidden)
            patch.setattr(SettingsRepository, '_recover', forbidden)
            with pytest.raises(ServiceError):
                open_service_owner(tmp_path, tmp_path / 'missing')
        assert first.repository.snapshot().revision == 1
    finally:
        first.close()


@pytest.mark.parametrize('phase', ['_load_prepared', 'upgrade'])
def test_constructor_failure_releases_only_its_preparing_claim(tmp_path, monkeypatch, phase):
    from atom_search.service.owner import open_service_owner
    from atom_search.settings.repository import SettingsRepository
    with monkeypatch.context() as patch:
        def fail(*a, **k):
            raise RuntimeError('controlled_settings_load_failure')
        patch.setattr(SettingsRepository, phase, fail)
        with pytest.raises(RuntimeError, match='controlled_settings_load_failure'):
            open_service_owner(tmp_path, tmp_path / 'missing')
    # A confirmed PG-free failure must not permanently block the same process.
    recovered = open_service_owner(tmp_path, tmp_path / 'missing')
    try:
        assert recovered.repository.snapshot().revision == 1 and recovered.core is None
    finally:
        recovered.close()


def test_foreign_preparing_claim_is_rejected_before_settings_load(tmp_path, monkeypatch):
    from atom_search.service.owner import open_service_owner
    from atom_search.service.contracts import ServiceError
    from atom_search.settings.repository import SettingsRepository
    from atom_search.runtime import _windows
    owner = open_service_owner(tmp_path, tmp_path / 'missing')
    original = json.loads(owner.claim_file.read_bytes())
    try:
        _windows.atomic_json(owner.claim_file, dict(original, profile=str(tmp_path / 'foreign')))
        with monkeypatch.context() as patch:
            patch.setattr(SettingsRepository, '_load_prepared', lambda *a: pytest.fail('foreign claim reached settings writer'))
            with pytest.raises(ServiceError) as failure:
                open_service_owner(tmp_path, tmp_path / 'missing')
        assert failure.value.code == 'service_identity_mismatch'
    finally:
        _windows.atomic_json(owner.claim_file, original)
        owner.close()


@pytest.mark.parametrize('factory_fails', [False, True])
def test_close_waits_for_startup_and_closes_one_core_without_deadlock(tmp_path, monkeypatch, factory_fails):
    from types import SimpleNamespace
    from atom_search.service.owner import open_service_owner
    from atom_search_core.service import SearchService
    from test_storage_ports import Ports
    from test_source_registry import MemoryIdentities
    owner = open_service_owner(tmp_path, tmp_path / 'missing')
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    ports = Ports()
    core = SearchService(catalog=ports, retrieval=ports,
                         on_close=lambda: ports.events.append('runtime stopped'))
    core.runtime = SimpleNamespace(storage_budget=owner.repository.budget,
        status=lambda: SimpleNamespace(endpoint=SimpleNamespace(installation_id='11111111-1111-4111-8111-111111111111')))
    core.public_ids = MemoryIdentities()
    def factory(*a, **k):
        entered.set()
        assert release.wait(10)
        if factory_fails:
            core.close()  # A failing P1 factory retains ownership of its cleanup.
            raise RuntimeError('controlled_start_failure')
        return core
    monkeypatch.setattr('atom_search.search_runtime.open_search_service', factory)
    failures = []
    close_failures = []
    def start():
        try:
            owner.start()
        except Exception as failure:
            failures.append(failure)
    def close():
        try:
            owner.close()
        except Exception as failure:
            close_failures.append(failure)
        finally:
            closed.set()
    starter, closer = threading.Thread(target=start), threading.Thread(target=close)
    starter.start()
    assert entered.wait(1)
    closer.start()
    try:
        time.sleep(.05)
        before = time.monotonic()
        snapshot = owner.repository.snapshot()
        assert time.monotonic() - before < .5
        if not factory_fails:
            from atom_search.runtime._windows import WindowsError
            candidate = snapshot.settings
            candidate['ui']['theme'] = 'light'
            before = time.monotonic()
            with pytest.raises(WindowsError, match='mutex_timeout'):
                owner.coordinator.owner_apply(candidate, snapshot.revision)
            assert time.monotonic() - before < 6.5, 'warmup save waited on the startup lifetime lock'
        assert not closed.is_set(), 'close returned while the factory still owned startup'
    finally:
        release.set()
        starter.join(10); closer.join(10)
        # Own only this test's fake factory and loopback resources on RED too.
        if owner._server is not None:
            owner._server.stop()
        if owner._ipc is not None:
            owner._ipc.close()
        core.close()
    assert not starter.is_alive() and not closer.is_alive()
    assert bool(failures) == factory_fails
    assert bool(close_failures) == factory_fails
    assert ports.events == ['catalog closed', 'runtime stopped']
    assert not owner.endpoint_file.exists()


def test_second_launch_early_settings_are_applied_by_the_existing_writer(owner_profile, monkeypatch):
    from atom_search.service.owner import open_service_owner
    from atom_search.settings.repository import SettingsRepository
    owner = owner_profile
    with monkeypatch.context() as patch:
        patch.setattr(SettingsRepository, '_load_prepared', lambda *a: pytest.fail('attach entered local recovery/writer'))
        attached = open_service_owner(owner.profile_dir, owner.model_dir)
    try:
        before = owner.repository.snapshot()
        candidate = attached.repository.snapshot().settings
        candidate['ui']['theme'] = 'light' if before.settings['ui']['theme'] != 'light' else 'dark'
        applied = attached.coordinator.owner_apply(candidate, before.revision)
        assert owner.repository.snapshot().revision == applied.revision
        assert owner.repository.snapshot().settings['ui']['theme'] == candidate['ui']['theme']
        assert attached.core is None
        attached.start()
        assert attached.attached
    finally:
        attached.close()


def test_ready_owner_missing_descriptor_fails_before_recovery(owner_profile, monkeypatch):
    from atom_search.service.owner import open_service_owner
    from atom_search.service.contracts import ServiceError
    from atom_search.settings.repository import SettingsRepository
    owner = owner_profile
    retained = owner.endpoint_file.with_name('endpoint.qa-retained.json')
    owner.endpoint_file.rename(retained)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(SettingsRepository, '_load_prepared', lambda *a: pytest.fail('missing descriptor reached recovery'))
            with pytest.raises(ServiceError) as failure:
                open_service_owner(owner.profile_dir, owner.model_dir)
        assert failure.value.code == 'service_identity_mismatch'
    finally:
        retained.rename(owner.endpoint_file)


def test_missing_descriptor_cannot_start_a_second_live_owner(tmp_path, monkeypatch):
    from dataclasses import asdict
    import os
    import uuid
    from atom_search.service.owner import open_service_owner
    from atom_search.service.contracts import ServiceError
    from atom_search.runtime import _windows
    owner = open_service_owner(tmp_path, tmp_path / 'missing')
    _windows.atomic_json(owner.state_file, dict(schema_version=1, state='ready',
        profile=str(tmp_path.resolve()), installation_id=str(uuid.uuid4()), generation=str(uuid.uuid4()),
        credential_ref='dpapi-v1-' + '0' * 64, process=asdict(_windows.process_identity(os.getpid()))))
    monkeypatch.setattr('atom_search.search_runtime.open_search_service',
                        lambda *a, **k: pytest.fail('bootstrapped while a live owner state exists'))
    with pytest.raises(ServiceError) as failure:
        owner.start()
    assert failure.value.code == 'service_identity_mismatch'
    owner.close()


def test_two_clients_share_one_model_pg_and_queue(owner_profile, owner_client, read_client, monkeypatch):
    from atom_search.service.contracts import SearchRequest
    owner = owner_profile
    def duplicate(*a, **k):
        pytest.fail('a client constructed another model/runtime')
    monkeypatch.setattr('atom_search.search_runtime._load_model', duplicate)
    monkeypatch.setattr('atom_search.runtime.postgres.PostgresRuntime.start', duplicate)
    before = owner.core
    first = owner_client.search(SearchRequest('drawing'))
    second = read_client.search(SearchRequest('drawing'))
    assert len(first.results) == 2 and len(second.results) == 1
    assert owner.core is before
    owner_client.close()
    assert len(read_client.search(SearchRequest('drawing')).results) == 1
    assert owner.runtime.status().state == 'ready'


def test_second_launch_attaches_matching_owner(owner_profile):
    from atom_search.service.owner import open_service_owner
    attached = open_service_owner(owner_profile.profile_dir, owner_profile.model_dir)
    try:
        assert attached.start() == owner_profile.endpoint_file
        assert attached.attached and attached.core is None
        assert attached.credential_ref == owner_profile.credential_ref
    finally:
        attached.close()
    assert owner_profile.runtime.status().state == 'ready'


def test_scoped_search_and_metadata_fetch_do_not_probe_document(owner_profile, read_client, monkeypatch):
    from atom_search.service.contracts import SearchRequest, ServiceError
    response = read_client.search(SearchRequest('drawing', limit=1))
    result = response.results[0]
    original = Path.open
    original_exists = Path.exists
    def no_document(path, *a, **k):
        if path.suffix == '.pdf':
            pytest.fail('metadata fetch opened a document')
        return original(path, *a, **k)
    monkeypatch.setattr(Path, 'open', no_document)
    def no_probe(path):
        if path.suffix == '.pdf':
            pytest.fail('metadata fetch probed a document')
        return original_exists(path)
    monkeypatch.setattr(Path, 'exists', no_probe)
    fetched = read_client.fetch(result.result_id)
    assert fetched.score is None and fetched.match_kind == 'metadata'
    assert fetched.full_path.endswith('allowed-drawing.pdf')
    assert read_client.search(SearchRequest('drawing', source_ids=())).results == ()
    with pytest.raises(ServiceError) as failure:
        read_client.fetch(result.result_id, source_ids=())
    assert failure.value.code == 'result_not_found'


def test_owner_close_drains_final_operation_once(tmp_path):
    from atom_search.service.owner import open_service_owner
    from atom_search_core.service import SearchService
    from test_storage_ports import Ports
    ports = Ports()
    core = SearchService(catalog=ports, retrieval=ports, on_close=lambda: ports.events.append('runtime stopped'))
    entered, release = threading.Event(), threading.Event()
    owner = open_service_owner(tmp_path, tmp_path / 'missing')
    owner.core = core
    # A timed-out reply must retain the operation until it actually finishes.
    def operation():
        entered.set(); release.wait(3)
    from atom_search.service.contracts import ServiceError
    with pytest.raises(ServiceError) as failure:
        owner.call(operation, timeout=.03)
    assert failure.value.code == 'request_timeout' and entered.is_set()
    closer = threading.Thread(target=owner.close)
    closer.start(); time.sleep(.05)
    assert closer.is_alive() and ports.events == []
    release.set(); closer.join(3)
    owner.close()
    assert not closer.is_alive()
    assert ports.events == ['catalog closed', 'runtime stopped']


@pytest.mark.parametrize(('publication_fails', 'state_fault'), [(False, None), (True, None),
    (True, 'generation'), (True, 'process'), (True, 'unknown')])
def test_scheduler_start_and_failed_publication_cleanup_release_startup_guard(tmp_path, monkeypatch, publication_fails, state_fault):
    from types import SimpleNamespace
    from atom_search.service.owner import open_service_owner
    from atom_search_core.service import SearchService
    from atom_search.runtime import _windows
    from test_storage_ports import Ports
    from test_source_registry import MemoryIdentities
    owner = open_service_owner(tmp_path, tmp_path / 'missing', automatic_catalog=False)
    ports = Ports()
    core = SearchService(catalog=ports, retrieval=ports)
    core.runtime = SimpleNamespace(storage_budget=owner.repository.budget,
        status=lambda: SimpleNamespace(endpoint=SimpleNamespace(installation_id=str(__import__('uuid').uuid4()))))
    core.public_ids = MemoryIdentities()
    monkeypatch.setattr('atom_search.search_runtime.open_search_service', lambda *a, **k: core)
    events, probes = [], []
    def probe():
        acquired = threading.Event()
        def run():
            with owner.repository.mutation():
                acquired.set()
        thread = threading.Thread(target=run)
        probes.append(thread)
        thread.start()
        thread.join(.5)
        return acquired.is_set()
    class Scheduler:
        def __init__(self, owner, **kwargs):
            events.append(('created', not owner.endpoint_file.exists()))
        def start(self):
            events.append(('started_outside_guard', probe()))
        def close(self):
            events.append(('closed_outside_guard', probe()))
    monkeypatch.setattr('atom_search.service.jobs.CatalogScheduler', Scheduler)
    atomic = _windows.atomic_json
    retained = []
    def publish(path, value):
        if publication_fails and path == owner.endpoint_file:
            if state_fault:
                state = json.loads(owner.state_file.read_bytes())
                if state_fault == 'generation':
                    state['generation'] = str(__import__('uuid').uuid4())
                elif state_fault == 'process':
                    state['process']['created'] += 1
                else:
                    state = {'unrecognized': 'retained'}
                atomic(owner.state_file, state)
                retained.append(owner.state_file.read_bytes())
            raise RuntimeError('controlled_endpoint_publication_failure')
        return atomic(path, value)
    monkeypatch.setattr(_windows, 'atomic_json', publish)
    try:
        if publication_fails:
            with pytest.raises(RuntimeError, match='controlled_endpoint_publication_failure'):
                owner.start()
        else:
            owner.start()
        if state_fault:
            with pytest.raises(Exception):
                owner.close()
        else:
            owner.close()
        assert ('created', True) in events
        if not publication_fails:
            assert ('started_outside_guard', True) in events, events
        else:
            assert not any(event[0].startswith('started') for event in events)
        assert events.count(('closed_outside_guard', True)) == 1, events
        assert ports.events == ['catalog closed']
        if state_fault:
            assert owner.state_file.read_bytes() == retained[0]
            assert json.loads(owner.claim_file.read_bytes())['phase'] == 'preparing'
        else:
            assert json.loads(owner.state_file.read_bytes())['state'] == 'stopped'
            recovered = open_service_owner(tmp_path, tmp_path / 'missing', automatic_catalog=False)
            recovered.close()
    finally:
        try:
            owner.close()
        except Exception:
            if not state_fault:
                raise
        for thread in probes:
            thread.join(6)
        core.close()
