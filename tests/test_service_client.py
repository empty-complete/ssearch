"""Authenticated identity, private credential references and read projections."""
import json
import uuid

import pytest


@pytest.mark.parametrize('field', ['generation', 'installation_id', 'credential_ref'])
def test_stale_descriptor_cannot_adopt_foreign_owner(owner_profile, tmp_path, field):
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import ServiceError
    # Even a real live port and valid token cannot override generation identity.
    path = owner_profile.endpoint_file
    original = path.read_bytes()
    descriptor = json.loads(original)
    descriptor[field] = 'dpapi-v1-' + '0' * 64 if field == 'credential_ref' else str(uuid.uuid4())
    try:
        from atom_search.runtime import _windows
        _windows.atomic_json(path, descriptor)
        with pytest.raises(ServiceError) as failure:
            ServiceClient(path, owner_profile.credential_ref).status()
        assert failure.value.code == 'service_identity_mismatch'
    finally:
        _windows.atomic_json(path, json.loads(original))


def test_client_rejects_foreign_profile_descriptor(owner_profile, tmp_path):
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import ServiceError
    path = tmp_path / 'endpoint.json'
    path.write_bytes(owner_profile.endpoint_file.read_bytes())
    with pytest.raises(ServiceError) as failure:
        ServiceClient(path, owner_profile.credential_ref).status()
    assert failure.value.code == 'service_identity_mismatch'


def test_descriptor_and_logs_never_contain_plaintext_tokens(owner_profile):
    owner = owner_profile
    secret = owner.credentials.resolve(owner.credential_ref).encode()
    for path in owner.profile_dir.rglob('*'):
        if path.is_file() and path.suffix in {'.json', '.log', '.out'}:
            assert secret not in path.read_bytes(), str(path)
    descriptor = json.loads(owner.endpoint_file.read_bytes())
    assert set(descriptor) == {'schema_version', 'pid', 'installation_id', 'generation', 'base_url', 'api_version', 'credential_ref'}


def test_settings_projection_omits_private_sections_and_paths(owner_profile, owner_client, read_client):
    owner = owner_client.settings_get()
    read = read_client.settings_get()
    assert owner.mutable_fields and read.mutable_fields == ()
    assert len(owner.settings['sources']) == 2 and len(read.settings['sources']) == 1
    assert read.settings['sources'][0]['label'] == 'allowed'
    assert not {'agent', 'diagnostics', 'updates', 'database', 'storage'} & read.settings.keys()
    assert 'credential_ref' not in json.dumps(read.settings)


def test_read_token_cannot_start_cancel_or_apply(read_client):
    from atom_search.service.contracts import ServiceError
    for operation in (lambda: read_client.start_catalog(),
                      lambda: read_client.cancel_job(str(uuid.uuid4())),
                      lambda: read_client.settings_validate({}, 1),
                      lambda: read_client.settings_apply({}, 1),
                      lambda: read_client._ipc('desktop_status', {}),
                      lambda: read_client._ipc('desktop_scan', {})):
        with pytest.raises(ServiceError) as failure:
            operation()
        assert failure.value.code == 'access_denied' and failure.value.status_code == 403


def test_owner_settings_apply_uses_ipc_and_preserves_cas(owner_client, monkeypatch):
    from atom_search.service.contracts import ServiceError
    snapshot = owner_client.settings_get()
    candidate = snapshot.settings
    candidate['ui']['theme'] = 'dark'
    # HTTP has no settings mutation endpoint: these calls must use protected IPC.
    import http.client
    monkeypatch.setattr(http.client.HTTPConnection, 'request', lambda *a, **k: pytest.fail('settings mutation used HTTP'))
    assert owner_client.settings_validate(candidate, snapshot.revision).valid
    current = owner_client.settings_apply(candidate, snapshot.revision)
    assert current.revision == snapshot.revision + 1
    with pytest.raises(ServiceError) as failure:
        owner_client.settings_apply(candidate, snapshot.revision)
    assert failure.value.code == 'revision_conflict'


def test_malformed_ipc_returns_the_same_error_envelope(owner_profile):
    from atom_search.service.ipc import connect
    from atom_search.http.serialization import decode
    descriptor = json.loads(owner_profile.endpoint_file.read_bytes())
    with connect(descriptor) as connection:
        connection.send_bytes(b'{')
        assert connection.poll(5)
        response = decode(connection.recv_bytes(65536))
    assert set(response) == {'schema_version', 'request_id', 'data', 'error'}
    assert response['schema_version'] == 1 and response['data'] is None
    assert response['error']['code'] == 'invalid_request'
    assert type(response['error']['retryable']) is bool


def test_remote_desktop_uses_the_owner_for_search_status_and_manual_cancel(owner_profile, owner_client, monkeypatch, catalog_wave):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from atom_search_core.source import canonical_root
    from atom_search.service.desktop import RemoteDesktopView
    owner = owner_profile
    remote = RemoteDesktopView(owner_client)
    source = owner.repository.snapshot().settings['sources'][0]['path']
    assert len(remote.search('drawing', root=source)) == 1
    assert remote.status(root=source)['files'] == 1
    target = owner.repository.snapshot().settings['sources'][0]['id']
    neighbor_states_before = {job.job_id: job.state for job in owner.scheduler.snapshot()
                              if job.source_id != target}
    entered, cancel = threading.Event(), threading.Event()
    original = owner.core.source.snapshots
    def blocked(root, cancellation, *, excluded_roots=()):
        if canonical_root(root) == canonical_root(source):
            entered.set()
            assert cancellation.wait(10)
        return original(root, cancellation, excluded_roots=excluded_roots)
    monkeypatch.setattr(owner.core.source, 'snapshots', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(remote.scan, source, cancel=cancel)
        try:
            # The real queue persists its schedule and admits begin_root before
            # enumerating. Use the native queue QA entry cap, independent of the
            # unchanged 10-second cancellation delivery/reply checks below.
            assert entered.wait(90)
        finally:
            cancel.set()
        assert future.result(timeout=10)['status'] == 'cancelled'
    assert owner.runtime.status().state == 'ready'
    neighbors = [job for job in owner.scheduler.snapshot() if job.source_id != target]
    # Retained terminal history may already contain cancelled jobs from a prior
    # wave. This cancellation must leave real new neighbors and any previously
    # uncancelled retained neighbors untouched.
    new_neighbors = [job for job in neighbors if job.job_id not in neighbor_states_before]
    assert new_neighbors and all(job.state != 'cancelled' for job in new_neighbors)
    assert all(job.state != 'cancelled' for job in neighbors
               if neighbor_states_before.get(job.job_id) not in (None, 'cancelled'))


def test_catalog_cancel_returns_typed_job_and_unknown_job_is_404(owner_profile, owner_client, catalog_wave):
    from atom_search.service.contracts import JobSnapshot, ServiceError
    source = owner_profile.repository.snapshot().settings['sources'][0]['id']
    jobs = owner_client.start_catalog((source,))
    assert len(jobs) == 1 and str(uuid.UUID(jobs[0])) == jobs[0]
    result = owner_client.cancel_job(jobs[0])
    assert isinstance(result, JobSnapshot) and result.job_id == jobs[0]
    assert result.processed == result.succeeded + result.skipped + result.errors
    with pytest.raises(ServiceError) as failure:
        owner_client.cancel_job(str(uuid.uuid4()))
    assert failure.value.code == 'job_not_found' and failure.value.status_code == 404
