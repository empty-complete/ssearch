"""Exercise the actual bound loopback server, never an in-process router fake."""
import http.client
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest


def request(owner, method, path, body=None, *, auth=True, origin=None, host=None, token=None):
    endpoint = json.loads(owner.endpoint_file.read_bytes())
    parsed = urlsplit(endpoint['base_url'])
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=8)
    headers = {}
    if auth:
        headers['Authorization'] = 'Bearer ' + (token or owner.credentials.resolve(owner.credential_ref))
    headers['X-Atom-Installation'] = endpoint['installation_id']
    headers['X-Atom-Generation'] = endpoint['generation']
    if origin is not None:
        headers['Origin'] = origin
    if host is not None:
        headers['Host'] = host
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    connection.request(method, path, data, headers)
    response = connection.getresponse()
    result = (response.status, dict(response.getheaders()), json.loads(response.read()))
    connection.close()
    return result


@pytest.mark.parametrize('origin', ['null', 'https://evil.example', 'http://127.0.0.1:1'])
def test_http_rejects_untrusted_origin_even_with_owner_token(owner_profile, origin):
    assert request(owner_profile, 'GET', '/v1/status', origin=origin)[0] == 403


def test_http_auth_origin_scope_and_limits(owner_profile, read_client):
    owner = owner_profile
    assert request(owner, 'GET', '/v1/status', auth=False)[0] == 401
    assert request(owner, 'GET', '/v1/status', host='evil.example')[0] == 403
    assert request(owner, 'GET', '/v1/status', origin=owner.base_url)[0] == 200
    for body in ({'query': ''}, {'query': 'x' * 513}, {'query': 'x', 'limit': 101},
                 {'query': 'x', 'limit': True}, {'query': 'x', 'mode': 'deep'}):
        assert request(owner, 'POST', '/v1/search', body)[0] == 400
    assert request(owner, 'POST', '/v1/search', b'x' * 65537)[0] == 413
    from atom_search.service.contracts import SearchRequest, ServiceError
    private = owner.repository.snapshot().settings['sources'][1]['id']
    with pytest.raises(ServiceError) as failure:
        read_client.search(SearchRequest('drawing', source_ids=(private,)))
    assert failure.value.code == 'source_scope_denied' and failure.value.details == {}
    allowed = read_client.search(SearchRequest('drawing')).results[0]
    with pytest.raises(ServiceError) as failure:
        read_client.fetch(allowed.result_id, source_ids=(private,))
    assert failure.value.code == 'source_scope_denied' and failure.value.status_code == 403
    with pytest.raises(ServiceError) as failure:
        read_client.search(SearchRequest('drawing', project_id=str(uuid.uuid4())))
    assert failure.value.code == 'project_scope_denied'


def test_busy_credential_creation_keeps_authenticated_reads_available(owner_profile, owner_client, monkeypatch):
    from contextlib import contextmanager
    from atom_search.service.contracts import ServiceError, SearchRequest
    owner = owner_profile
    waiting = threading.Event()
    mutation = owner.repository.mutation
    writer = threading.current_thread()
    @contextmanager
    def observed_mutation():
        if threading.current_thread() is not writer:
            waiting.set()
        with mutation():
            yield
    monkeypatch.setattr(owner.repository, 'mutation', observed_mutation)
    with ThreadPoolExecutor(max_workers=3) as pool:
        # A legitimate writer owns the real profile guard. Credential creation
        # must retain its five-second admission bound without stalling tokens
        # already used by the HTTP status and search paths.
        with mutation():
            issuance = pool.submit(owner.issue_credential, owner.owner_access)
            assert waiting.wait(1)
            assert pool.submit(owner_client.status).result(timeout=1).liveness == 'alive'
            search = pool.submit(owner_client.search, SearchRequest('drawing'))
            with pytest.raises(ServiceError) as failure:
                issuance.result(timeout=6)
            assert (failure.value.code, failure.value.status_code, failure.value.retryable) == (
                'service_unavailable', 503, True)
            # The real SQL/identity reads have no one-second product SLO. They
            # must finish while the writer is still held, by its bounded busy
            # reply; a credential-map lock spanning admission fails this check.
            assert search.done(), 'authenticated search waited for the busy writer'
            assert search.result(timeout=0).results
        # A later retry uses the unchanged real DPAPI/admission path.
        reference = owner.issue_credential(owner.owner_access)
        assert owner.credential(reference)[1] == owner.owner_access


def test_http_envelope_and_metadata_only_fetch(owner_profile, read_client):
    from atom_search.service.contracts import SearchRequest
    result = read_client.search(SearchRequest('drawing')).results[0]
    status, _, envelope = request(owner_profile, 'GET', '/v1/results/' + result.result_id)
    assert status == 200 and envelope['schema_version'] == 1 and envelope['error'] is None
    assert str(uuid.UUID(envelope['request_id'])) == envelope['request_id']
    data = envelope['data']
    assert data['full_path'].endswith('allowed-drawing.pdf') and 'modified_ns' in data
    assert not {'content', 'snippets', 'text'} & data.keys()
    status, _, denied = request(owner_profile, 'GET', '/v1/results/' + result.result_id + '?scope=none')
    assert status == 404 and type(denied['error']['retryable']) is bool


def test_http_concurrency_and_deadline_do_not_block_health(owner_profile, owner_client, monkeypatch):
    from contextlib import contextmanager
    from atom_search.service.application import AtomSearchService
    from atom_search.service.contracts import QueryInterpretation, SearchResponse

    owner = owner_profile
    release = threading.Event()
    entered = threading.Condition()
    count = 0
    original = owner.operation
    assert owner.core.project_search_supported is True
    @contextmanager
    def blocked(cancel=None):
        nonlocal count
        with original(cancel):
            with entered:
                count += 1; entered.notify_all()
            release.wait(7)
            yield
    monkeypatch.setattr(owner, 'operation', blocked)
    # This artificial operation measures admission/deadline/drain, without
    # adding the cost of sixteen real metadata queries after its release.
    monkeypatch.setattr(AtomSearchService, 'search', lambda self, request: SearchResponse(
        (), QueryInterpretation(request.query, request.query, None, ()), (), (), 0, ()))
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(request, owner, 'POST', '/v1/search', {'query': 'slow'}) for _ in range(16)]
        try:
            with entered:
                assert entered.wait_for(lambda: count == 16, timeout=4), count
            assert request(owner, 'POST', '/v1/search', {'query': 'excess'})[0] == 429
            from atom_search.service.contracts import ServiceError
            with pytest.raises(ServiceError) as failure:
                owner_client.settings_validate({}, 1)
            assert failure.value.status_code == 429, 'IPC bypassed the same operation admission'
            # Timed-out operations retain admission until they really finish.
            assert futures[0].result(timeout=7)[0] == 504
            assert request(owner, 'GET', '/v1/status')[0] == 429
        finally:
            release.set()
            for future in futures:
                future.result(timeout=8)
    monkeypatch.setattr(owner, 'operation', original)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if request(owner, 'GET', '/v1/status')[0] == 200:
            break
        time.sleep(.01)
    else:
        pytest.fail('admission did not drain')


def test_health_remains_available_during_manual_catalog(owner_profile, owner_client, monkeypatch, catalog_wave):
    owner = owner_profile
    entered, release = threading.Event(), threading.Event()
    original = owner.core.source.snapshots
    def blocked(root, cancel, *, excluded_roots=()):
        entered.set()
        release.wait(7)
        return original(root, cancel, excluded_roots=excluded_roots)
    monkeypatch.setattr(owner.core.source, 'snapshots', blocked)
    source = owner.repository.snapshot().settings['sources'][0]['path']
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner.desktop_view().scan, source)
        try:
            assert entered.wait(15)
            assert owner_client.status().liveness == 'alive'
            assert not future.done()
        finally:
            release.set()
            # One manual refresh now owns the entire A/B metadata wave.
            # Actual Task5 admitted reconciliation measured ~4.3s per write.
            assert future.result(timeout=90)['status'] == 'complete'


def test_health_http_client_real_storage_and_nullable_reservations(owner_profile, owner_client, monkeypatch, tmp_path):
    from dataclasses import asdict
    from datetime import datetime, timezone
    from atom_search.service.health import storage_health
    owner = owner_profile
    owner.probes.request('storage')
    # Two seconds bounds one cooperative chunk, not the complete domain walk.
    # Keep the whole-snapshot wait bounded by the native queue QA cap.
    until = time.monotonic() + 90
    while time.monotonic() < until:
        storage = owner_client.status().components.storage
        if storage.checked_at is not None:
            break
        time.sleep(.02)
    else:
        pytest.fail('a fresh owned profile never produced bounded background physical storage facts')
    for name in ('physical_owned_bytes', 'pgdata_bytes', 'wal_bytes', 'free_bytes', 'quota_bytes', 'reserved_bytes'):
        assert getattr(storage, name) is not None, (name, storage)
    assert storage.physical_owned_bytes >= storage.pgdata_bytes >= storage.wal_bytes > 0
    assert storage.free_bytes > 0 and storage.quota_bytes > storage.physical_owned_bytes
    (tmp_path / 'storage-health.json').write_text(json.dumps(asdict(storage)), encoding='utf-8')
    # Keep the actual server/auth/client path; prevent an unrelated timed
    # storage refresh from racing these three public serialization cases.
    monkeypatch.setitem(owner.probes.probes, 'storage', lambda deadline, stop: None)
    with owner.probes._condition:
        assert owner.probes._condition.wait_for(lambda: 'storage' not in owner.probes._running, timeout=3)
    for reserved in (None, 0, 500):
        owner.health.record('storage', storage_health({'reserved_bytes': reserved}, checked_at=None),
            checked_at=datetime.now(timezone.utc))
        wire = request(owner, 'GET', '/v1/status')[2]['data']['components']['storage']
        assert wire['reserved_bytes'] == reserved and wire['physical_owned_bytes'] is None
        typed = owner_client.status().components.storage
        assert typed.reserved_bytes == reserved and typed.physical_owned_bytes is None


def test_real_owner_rejects_large_and_slow_oversized_requests(owner_profile):
    from test_service_http_limits import assert_large_body_rejected, assert_slow_oversized_sender_gets_early_reply
    owner = owner_profile
    endpoint = urlsplit(owner.base_url)
    headers = {'Authorization': 'Bearer ' + owner.credentials.resolve(owner.credential_ref),
               'X-Atom-Installation': owner.installation_id, 'X-Atom-Generation': owner.generation}
    address = endpoint.hostname, endpoint.port
    assert_large_body_rejected(address, headers)
    assert_slow_oversized_sender_gets_early_reply(address, headers)
