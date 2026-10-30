"""Host-owned session admission and shutdown over the real offline provider."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import builtins
import importlib
import importlib.util
from pathlib import Path
import socket
import threading
import uuid

import pytest

from atom_search.service.contracts import ResultMetadata, SearchRequest, SearchResponse, ServiceError


MANIFEST = Path(__file__).resolve().parents[2] / 'examples/onboarding-demo/manifest.json'


def api():
    name = 'atom_search.onboarding.session_manager'
    assert importlib.util.find_spec(name) is not None, 'host-owned demo session manager is required'
    return importlib.import_module(name)


def manager(tmp_path, *, capabilities=(), register=None, **limits):
    return api().DemoSessionManager(manifest_path=MANIFEST, profile_dir=tmp_path / 'profile',
        build_capabilities=capabilities, register_owned_exclusion=register or (lambda path: None), **limits)


def assert_error(code, operation):
    with pytest.raises(ServiceError) as failure:
        operation()
    assert failure.value.code == code


@contextmanager
def occupied_file(path):
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL('kernel32', use_last_error=True)
    create, close = kernel.CreateFileW, kernel.CloseHandle
    create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    create.restype = w.HANDLE
    close.argtypes, close.restype = [w.HANDLE], w.BOOL
    handle = create(str(path), 0x80000000, 1, None, 3, 0x200000, None)
    assert handle and handle != c.c_void_p(-1).value, c.WinError(c.get_last_error())
    try:
        yield
    finally:
        close(handle)


def test_registration_precedes_materialization_and_uses_real_host_registry(tmp_path):
    from atom_search.service.sources import SourceRegistry
    from atom_search.settings.repository import SettingsRepository
    from atom_search_core.source import FileSystemSource, canonical_root
    from test_source_registry import MemoryIdentities

    module = api()
    repo = SettingsRepository(tmp_path / 'profile')
    snapshot = repo.load()
    before = repo.path.read_bytes()
    registry = SourceRegistry(repo, MemoryIdentities(), owner_id='trusted-host')
    owned = repo.profile_dir / 'runtime/onboarding-demo'
    registered = []

    def register(path):
        assert not owned.exists(), 'exclusion must exist before the first owned file'
        assert path == Path(canonical_root(str(owned)))
        registry.register_owned_exclusion(path, owner_id='trusted-host')
        registered.append(path)

    subject = module.DemoSessionManager(manifest_path=MANIFEST, profile_dir=repo.profile_dir / '..' / 'profile',
        build_capabilities=(), register_owned_exclusion=register)
    session = subject.open()
    try:
        response = subject.search(session.session_id, SearchRequest('Пермские моторы'))
        assert response.results and all(Path(row.full_path).is_file() for row in response.results)
        assert registered == [Path(canonical_root(str(owned)))]
        snapshots = tuple(FileSystemSource().snapshots(str(tmp_path), threading.Event(),
                                                       excluded_roots=registry.owned_exclusions()))
        assert not any('onboarding-demo' in batch.relative_path for batch in snapshots)
        scanned = {tmp_path.joinpath(*file.relative_path.split('/')).resolve()
                   for batch in snapshots for file in batch.files}
        assert all(Path(row.full_path).resolve() not in scanned for row in response.results)
        assert repo.snapshot() == snapshot and repo.path.read_bytes() == before
    finally:
        subject.close(session.session_id)


def test_failed_registration_creates_nothing_and_cannot_grant_session_authority(tmp_path):
    api()
    attempted = []

    def denied(path):
        attempted.append(path)
        raise ServiceError('access_denied', 'host registration rejected', status_code=403)

    subject = manager(tmp_path, register=denied)
    assert_error('access_denied', subject.open)
    assert len(attempted) == 1
    assert not (tmp_path / 'profile/runtime/onboarding-demo').exists()
    assert_error('demo_session_not_found', lambda: subject.search(str(uuid.uuid4()), SearchRequest('ПМ')))


def test_session_info_is_immutable_explicit_demo_and_has_no_provider_or_root(tmp_path):
    subject = manager(tmp_path)
    session = subject.open()
    try:
        assert session.mode == 'demo' and session.version == 2 and session.capabilities == ()
        assert str(uuid.UUID(session.session_id)) == session.session_id
        assert not hasattr(session, 'provider') and not hasattr(session, 'root')
        with pytest.raises(FrozenInstanceError):
            session.mode = 'work'
        response = subject.search(session.session_id, SearchRequest('Пермские моторы'))
        assert isinstance(response, SearchResponse)
        fetched = subject.fetch(session.session_id, response.results[0].result_id)
        assert isinstance(fetched, ResultMetadata) and fetched.match_kind == 'metadata'
        assert Path(fetched.full_path).is_file()
    finally:
        subject.close(session.session_id)


def test_session_capabilities_are_host_build_intersection_with_real_provider(tmp_path):
    capabilities = ('project_search', 'project_aliases', 'signed_updates', 'agent_plugin_readonly',
                    'deep_content', 'unknown')
    subject = manager(tmp_path, capabilities=capabilities)
    session = subject.open()
    try:
        assert set(session.capabilities) == {'project_search', 'project_aliases'}
        response = subject.search(session.session_id, SearchRequest('Пермские моторы зенитные фонари'))
        assert any('Чёто до' in row.relative_path for row in response.results[:4])
        assert subject.search(session.session_id, SearchRequest('ПМ')).ambiguities
        assert subject.search(session.session_id, SearchRequest('Пермские моторы', source_ids=())).results == ()
        assert_error('source_scope_denied', lambda: subject.search(session.session_id,
            SearchRequest('ПМ', source_ids=(str(uuid.uuid4()),))))
    finally:
        subject.close(session.session_id)
    literal = manager(tmp_path / 'literal')
    other = literal.open()
    try:
        assert other.capabilities == ()
        response = literal.search(other.session_id, SearchRequest('Пермские моторы'))
        assert not response.applied_aliases
        assert not any('Чёто до' in row.relative_path for row in response.results[:4])
    finally:
        literal.close(other.session_id)


def test_foreign_or_stale_ids_never_reach_global_demo_registry(tmp_path):
    first, second = manager(tmp_path / 'first'), manager(tmp_path / 'second')
    session = first.open()
    response = first.search(session.session_id, SearchRequest('спецификация'))
    selected = response.results[0]
    try:
        for operation in (lambda: second.search(session.session_id, SearchRequest('спецификация')),
                          lambda: second.fetch(session.session_id, selected.result_id),
                          lambda: second.begin_close(session.session_id),
                          lambda: second.close(session.session_id)):
            assert_error('demo_session_not_found', operation)
        assert Path(selected.full_path).is_file()
        assert first.fetch(session.session_id, selected.result_id).result_id == selected.result_id
    finally:
        first.close(session.session_id)
    for operation in (lambda: first.search(session.session_id, SearchRequest('спецификация')),
                      lambda: first.fetch(session.session_id, selected.result_id),
                      lambda: first.close(session.session_id)):
        assert_error('demo_session_not_found', operation)


def test_one_active_or_cleanup_pending_session_bounds_materialization(tmp_path):
    subject = manager(tmp_path)
    session = subject.open()
    try:
        before = set((tmp_path / 'profile/runtime/onboarding-demo').glob('v2-*'))
        assert_error('demo_session_limit', subject.open)
        assert set((tmp_path / 'profile/runtime/onboarding-demo').glob('v2-*')) == before
        selected = subject.search(session.session_id, SearchRequest('спецификация')).results[0]
        with occupied_file(Path(selected.full_path)):
            cleanup = subject.close(session.session_id)
            assert Path(selected.full_path) in cleanup.deferred
            assert_error('demo_session_closed', lambda: subject.search(session.session_id, SearchRequest('ПМ')))
            assert_error('demo_session_limit', subject.open)
        subject.close(session.session_id)
        assert not Path(selected.full_path).exists()
        replay = subject.open()
        try:
            assert replay.session_id != session.session_id
            assert_error('demo_session_not_found', lambda: subject.fetch(session.session_id, selected.result_id))
        finally:
            subject.close(replay.session_id)
    finally:
        # No global cleanup authority is used for retired ids.
        try:
            subject.close(session.session_id)
        except ServiceError as failure:
            assert failure.code == 'demo_session_not_found'


def test_close_stops_admission_then_drains_real_provider_operation_before_cleanup(tmp_path, monkeypatch):
    api()
    from atom_search.onboarding.demo_provider import DemoSearchProvider
    subject = manager(tmp_path)
    session = subject.open()
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original_search, original_begin = DemoSearchProvider.search, subject.begin_close
    paths = []

    def admitted_search(provider, search_request):
        response = original_search(provider, search_request)
        paths.extend(Path(row.full_path) for row in response.results)
        entered.set()
        assert release.wait(10), 'admitted operation was never released'
        assert all(path.is_file() for path in paths), 'cleanup raced an admitted operation'
        return response

    def observed_begin(session_id):
        original_begin(session_id)
        stopped.set()

    with monkeypatch.context() as patch:
        patch.setattr(DemoSearchProvider, 'search', admitted_search)
        patch.setattr(subject, 'begin_close', observed_begin)
        with ThreadPoolExecutor(max_workers=2) as executor:
            searching = executor.submit(subject.search, session.session_id, SearchRequest('спецификация'))
            closing = None
            try:
                assert entered.wait(10)
                closing = executor.submit(subject.close, session.session_id)
                assert stopped.wait(10), 'close did not cut off admission before waiting'
                assert not closing.done(), 'close must await the admitted operation'
                assert_error('demo_session_closed', lambda: subject.search(session.session_id, SearchRequest('ПМ')))
                assert_error('demo_session_closed', lambda: subject.fetch(session.session_id, 'unknown'))
            finally:
                release.set()
            assert searching.result(timeout=10).results
            assert closing is not None and not closing.result(timeout=10).deferred
    assert all(not path.exists() for path in paths)


def test_inflight_admission_is_bounded_and_released_after_real_response(tmp_path, monkeypatch):
    api()
    from atom_search.onboarding.demo_provider import DemoSearchProvider
    subject = manager(tmp_path, max_inflight=1)
    session = subject.open()
    entered, release = threading.Event(), threading.Event()
    original_search = DemoSearchProvider.search

    def admitted_search(provider, search_request):
        entered.set()
        assert release.wait(10)
        return original_search(provider, search_request)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(DemoSearchProvider, 'search', admitted_search)
            with ThreadPoolExecutor(max_workers=1) as executor:
                searching = executor.submit(subject.search, session.session_id, SearchRequest('ПМ'))
                try:
                    assert entered.wait(10)
                    assert_error('too_many_requests', lambda: subject.search(session.session_id, SearchRequest('ПМ')))
                    assert_error('too_many_requests', lambda: subject.fetch(session.session_id, 'unknown'))
                finally:
                    release.set()
                searching.result(timeout=10)
        assert subject.search(session.session_id, SearchRequest('спецификация')).results
    finally:
        subject.close(session.session_id)


def test_begin_close_is_host_only_two_phase_shutdown_and_keeps_files_until_close(tmp_path):
    subject = manager(tmp_path)
    session = subject.open()
    selected = subject.search(session.session_id, SearchRequest('спецификация')).results[0]
    subject.begin_close(session.session_id)
    subject.begin_close(session.session_id)
    assert Path(selected.full_path).is_file()
    assert_error('demo_session_closed', lambda: subject.fetch(session.session_id, selected.result_id))
    subject.close(session.session_id)
    assert not Path(selected.full_path).exists()


def test_session_composition_is_offline_without_owner_model_or_work_state_changes(tmp_path, monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(('psycopg', 'onnxruntime', 'PySide6', 'sqlite3',
                                    'atom_search.service.owner', 'atom_search_core.storage'))
        return original_import(name, *args, **kwargs)

    def no_network(*args, **kwargs):
        pytest.fail('demo session composition attempted network access')

    work = tmp_path / 'work-state.json'
    work.write_bytes(b'{"sources":[],"files":42,"ready":false}')
    with monkeypatch.context() as patch:
        patch.setattr(builtins, '__import__', guarded_import)
        patch.setattr(socket, 'socket', no_network)
        patch.setattr(socket, 'create_connection', no_network)
        subject = manager(tmp_path, capabilities=('project_aliases',))
        session = subject.open()
        try:
            first = subject.search(session.session_id, SearchRequest('ПМ'))
            work.write_bytes(b'{"sources":[],"files":42,"ready":true}')
            expected = work.read_bytes()
            again = subject.search(session.session_id, SearchRequest('ПМ'))
            assert first == again and session.mode == 'demo'
            assert again.ambiguities
            assert work.read_bytes() == expected
        finally:
            subject.close(session.session_id)
        assert work.read_bytes() == expected


def test_registration_must_acknowledge_the_none_return_contract_before_creating_files(tmp_path):
    subject = manager(tmp_path, register=lambda path: False)
    assert_error('access_denied', subject.open)
    assert not (tmp_path / 'profile/runtime/onboarding-demo').exists()
