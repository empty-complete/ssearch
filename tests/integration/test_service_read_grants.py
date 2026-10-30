"""Owner read-grant admission over real P2 IPC, private DPAPI and owned PG.

These initial cases use only existing public transports, so the baseline RED
identifies missing disabled-route behavior rather than a future module import.
Policy/consent, TTL and revocation cases follow once that internal seam is agreed.
"""
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import threading
from urllib.parse import urlsplit

import pytest

from atom_search.service.contracts import SearchRequest, ServiceError
from atom_search.service.client import ServiceClient


def issue_request(owner):
    snapshot = owner.repository.snapshot()
    return dict(consumer_id=str(uuid.uuid4()), operation_id=str(uuid.uuid4()),
        consent_id=str(uuid.uuid4()), expected_settings_revision=snapshot.revision,
        source_ids=[snapshot.settings['sources'][0]['id']],
        capabilities=['search', 'fetch', 'status', 'settings_get'],
        ttl_seconds=37)  # Synthetic request, not a production TTL/default/policy.


def credential_inventory(owner):
    # References/file names only: never copy plaintext secrets or DPAPI blobs
    # into assertions or evidence. Existing owner/read credentials must survive.
    with owner._credential_lock:
        references = frozenset(owner._access)
    files = frozenset(path.name for path in owner.credentials.directory.iterdir())
    return references, files


def test_owner_issue_without_trusted_policy_is_disabled_and_does_not_mint(
        owner_profile, owner_client, read_client):
    request = issue_request(owner_profile)
    before = credential_inventory(owner_profile)
    with pytest.raises(ServiceError) as failure:
        owner_client._ipc('read_grant_issue', request)
    assert credential_inventory(owner_profile) == before
    # A request-supplied consent UUID is not authorization. With no trusted
    # host policy/provider, the named owner operation fails explicitly closed.
    assert (failure.value.code, failure.value.status_code) == ('service_unavailable', 503)
    assert owner_client.status().api_version == '1'
    assert read_client.status().api_version == '1'
    assert read_client.search(SearchRequest('drawing', source_ids=(), mode='literal')).results == ()


@pytest.mark.parametrize('method', ['read_grant_issue', 'read_grant_get', 'read_grant_revoke'])
def test_existing_reader_cannot_manage_read_grants(owner_profile, read_client, owner_client, method):
    request = issue_request(owner_profile)
    arguments = request if method == 'read_grant_issue' else {
        'consumer_id': request['consumer_id'], 'operation_id': request['operation_id']}
    before = credential_inventory(owner_profile)
    # Actual P2 authenticates this real DPAPI reader and requires owner before
    # any named grant dispatch, including get/revoke and disabled issuance.
    with pytest.raises(ServiceError) as failure:
        read_client._ipc(method, arguments)
    assert (failure.value.code, failure.value.status_code) == ('access_denied', 403)
    assert credential_inventory(owner_profile) == before
    assert owner_client.status().api_version == read_client.status().api_version == '1'


@pytest.fixture
def grants(owner_profile, owner_client, monkeypatch):
    from atom_search.service.read_grants import ReadGrantPolicy, ReadGrantRequest, ReadGrantStore
    owner = owner_profile
    before_refs, before_files = credential_inventory(owner)
    remove = owner.credentials.remove
    now, approved = [100.], []
    class Consent:
        def approve(self, request):
            return request in approved
    policy = ReadGrantPolicy(min_ttl_seconds=1, max_ttl_seconds=120,
                            allowed_capabilities=frozenset({'search', 'fetch', 'status', 'settings_get'}))
    consent = Consent()
    store = ReadGrantStore(owner, policy=policy, consent_provider=consent, clock=lambda: now[0])
    monkeypatch.setattr(owner, 'read_grants', store)
    def approve(request):
        approved.append(ReadGrantRequest(consumer_id=request['consumer_id'], operation_id=request['operation_id'],
            consent_id=request['consent_id'], expected_settings_revision=request['expected_settings_revision'],
            source_ids=tuple(request['source_ids']), capabilities=frozenset(request['capabilities']),
            ttl_seconds=request['ttl_seconds']))
    try:
        yield type('GrantFixture', (), dict(owner=owner, client=owner_client, now=now,
            store=store, policy=policy, consent=consent, approved=approved, approve=staticmethod(approve)))()
    finally:
        with owner._credential_lock:
            for reference in set(owner._access) - before_refs:
                owner._access.pop(reference, None)
        for path in owner.credentials.directory.iterdir():
            if path.name not in before_files and path.suffix == '.dpapi':
                remove(path.stem)


def grant_key(request):
    return {name: request[name] for name in ('consumer_id', 'operation_id')}


def limit_operations(grants, limit):
    from atom_search.service.read_grants import ReadGrantPolicy
    # A real immutable policy subtype supplies the future explicit bound. The
    # current store accepts it but ignores the bound: RED is an actual bad mint,
    # not an absent-field/constructor error.
    @dataclass(frozen=True)
    class LimitedPolicy(ReadGrantPolicy):
        max_operations: int
    grants.store.policy = LimitedPolicy(min_ttl_seconds=1, max_ttl_seconds=120,
        allowed_capabilities=grants.policy.allowed_capabilities, max_operations=limit)


def issue(grants, **changes):
    request = issue_request(grants.owner)
    request.update(changes)
    grants.approve(request)
    return request, grants.client.read_grant_issue(**request)


def raw_read(owner, *, token=None, reference=None):
    address = urlsplit(owner.base_url)
    headers = {'X-Atom-Installation': owner.installation_id, 'X-Atom-Generation': owner.generation}
    if reference is not None:
        path = '/v1/handshake'
        headers.update({'X-Atom-Credential': reference, 'X-Atom-Nonce': 'a' * 64})
        body, method = None, 'GET'
    else:
        path, method = '/v1/search', 'POST'
        headers['Authorization'] = 'Bearer ' + token
        body = json.dumps({'query': 'drawing', 'mode': 'literal'})
    with http.client.HTTPConnection(address.hostname, address.port, timeout=5) as connection:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        response.read()
        return response.status


def test_scoped_grant_real_nonce_reads_no_status_escalation_and_no_mutations(grants):
    request, grant = issue(grants, capabilities=['search', 'fetch'])
    assert grant['consumer_id'] == request['consumer_id']
    assert grant['source_ids'] == request['source_ids'] and grant['capabilities'] == ['fetch', 'search']
    assert grant['installation_id'] == grants.owner.installation_id and grant['generation'] == grants.owner.generation
    assert not {'secret', 'token', 'password'} & grant.keys()
    client = ServiceClient(grants.owner.endpoint_file, grant['credential_ref'])
    rows = client.search(SearchRequest('drawing', mode='literal')).results
    assert len(rows) == 1 and rows[0].source_id == request['source_ids'][0]
    assert client.fetch(rows[0].result_id).result_id == rows[0].result_id
    for operation in (client.status, lambda: client.start_catalog([]),
                      lambda: client.settings_apply({}, request['expected_settings_revision']),
                      lambda: client._ipc('read_grant_get', grant_key(request))):
        with pytest.raises(ServiceError) as failure:
            operation()
        assert failure.value.code == 'access_denied'
    with pytest.raises(ServiceError) as scope:
        client.search(SearchRequest('drawing', source_ids=(grants.owner.repository.snapshot().settings['sources'][1]['id'],)))
    assert scope.value.code == 'source_scope_denied'
    _, empty = issue(grants, source_ids=[], capabilities=['search'])
    assert ServiceClient(grants.owner.endpoint_file, empty['credential_ref']).search(SearchRequest('drawing')).results == ()


@pytest.mark.parametrize('change,code', [({'consumer_id': str(uuid.UUID(int=1))}, 'access_denied'),
    ({'source_ids': []}, 'access_denied'), ({'capabilities': ['status']}, 'access_denied'),
    ({'ttl_seconds': 38}, 'access_denied'), ({'consent_id': str(uuid.UUID(int=2))}, 'access_denied'),
    ({'expected_settings_revision': 1}, 'revision_conflict')])
def test_consent_exact_match_and_settings_cas_before_mint(grants, change, code):
    request = issue_request(grants.owner)
    grants.approve(request)
    request.update(change)
    before = credential_inventory(grants.owner)
    with pytest.raises(ServiceError) as failure:
        grants.client.read_grant_issue(**request)
    assert failure.value.code == code
    assert credential_inventory(grants.owner) == before


@pytest.mark.parametrize('change', [{'source_ids': None}, {'source_ids': ['not-a-uuid']},
    {'capabilities': ['owner']}, {'capabilities': ['settings_apply']}, {'capabilities': ['health']},
    {'ttl_seconds': True}, {'ttl_seconds': 0}, {'ttl_seconds': 121}])
def test_invalid_grant_policy_bounds_and_read_subset_never_mint(grants, change):
    request = issue_request(grants.owner)
    request.update(change)
    before = credential_inventory(grants.owner)
    with pytest.raises(ServiceError) as failure:
        grants.client.read_grant_issue(**request)
    assert failure.value.code == 'invalid_request'
    assert credential_inventory(grants.owner) == before


def test_issue_idempotency_lost_reply_get_and_no_renewal(grants):
    request, first = issue(grants)
    before = credential_inventory(grants.owner)
    grants.now[0] += 3
    assert grants.client.read_grant_issue(**request) == first
    assert grants.client.read_grant_get(**grant_key(request)) == first
    assert credential_inventory(grants.owner) == before
    changed = {**request, 'ttl_seconds': 38}
    grants.approve(changed)
    with pytest.raises(ServiceError) as failure:
        grants.client.read_grant_issue(**changed)
    assert failure.value.code == 'revision_conflict'
    assert credential_inventory(grants.owner) == before


def test_concurrent_duplicate_issue_mints_one_dpapi_record(grants):
    request = issue_request(grants.owner)
    grants.approve(request)
    before = credential_inventory(grants.owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(grants.client.read_grant_issue, **request) for _ in range(2)]
        replies = [call.result(timeout=5) for call in calls]
    assert replies[0] == replies[1]
    after = credential_inventory(grants.owner)
    assert len(after[0] - before[0]) == len(after[1] - before[1]) == 1


@pytest.mark.parametrize('missing', ['policy', 'consent_provider'])
def test_either_missing_trusted_provider_or_policy_disables_mint(grants, missing):
    from atom_search.service.read_grants import ReadGrantStore
    options = dict(policy=grants.policy, consent_provider=grants.consent)
    options[missing] = None
    grants.owner.read_grants = ReadGrantStore(grants.owner, **options)
    before = credential_inventory(grants.owner)
    with pytest.raises(ServiceError) as failure:
        grants.client.read_grant_issue(**issue_request(grants.owner))
    assert failure.value.code == 'service_unavailable'
    assert credential_inventory(grants.owner) == before


@pytest.mark.parametrize('ending', ['expire', 'revoke'])
def test_cached_bearer_and_fresh_nonce_stop_after_expiry_or_revocation(grants, ending):
    request, grant = issue(grants)
    reference = grant['credential_ref']
    token = grants.owner.credentials.resolve(reference)
    client = ServiceClient(grants.owner.endpoint_file, reference)
    assert client.status().api_version == '1'
    assert raw_read(grants.owner, token=token) == raw_read(grants.owner, reference=reference) == 200
    if ending == 'expire':
        grants.now[0] += request['ttl_seconds']
    else:
        assert grants.client.read_grant_revoke(**grant_key(request))['state'] == 'revoked'
        assert grants.client.read_grant_revoke(**grant_key(request))['state'] == 'revoked'
    assert raw_read(grants.owner, token=token) == raw_read(grants.owner, reference=reference) == 401
    with pytest.raises(ServiceError) as failure:
        client.status()
    assert failure.value.code == 'authentication_required'
    assert grants.client.status().api_version == '1'


def test_revoke_cleanup_failure_cannot_revive_token(grants, monkeypatch):
    from atom_search.runtime.credentials import CredentialError
    request, grant = issue(grants)
    reference = grant['credential_ref']
    token = grants.owner.credentials.resolve(reference)
    monkeypatch.setattr(grants.owner.credentials, 'remove', lambda _: (_ for _ in ()).throw(CredentialError('controlled')))
    assert grants.client.read_grant_revoke(**grant_key(request))['state'] == 'revoked'
    assert raw_read(grants.owner, token=token) == raw_read(grants.owner, reference=reference) == 401
    assert grants.client.read_grant_get(**grant_key(request))['state'] == 'revoked'


def test_slow_grant_create_does_not_block_existing_authentication(grants, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = grants.owner.credentials.create
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return original(*args, **kwargs)
    monkeypatch.setattr(grants.owner.credentials, 'create', slow)
    request = issue_request(grants.owner)
    grants.approve(request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(grants.client.read_grant_issue, **request)
        try:
            assert entered.wait(2)
            assert pool.submit(grants.client.status).result(timeout=1).api_version == '1'
        finally:
            release.set()
        assert pending.result(timeout=5)['state'] == 'active'


def test_revoke_ack_blocks_new_auth_but_captured_inflight_scope_can_finish(grants, monkeypatch):
    request, grant = issue(grants, capabilities=['search'])
    client = ServiceClient(grants.owner.endpoint_file, grant['credential_ref'])
    entered, release = threading.Event(), threading.Event()
    original = grants.owner.core.search_project
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return original(*args, **kwargs)
    monkeypatch.setattr(grants.owner.core, 'search_project', held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.search, SearchRequest('drawing', mode='literal'))
        try:
            assert entered.wait(2)
            grants.client.read_grant_revoke(**grant_key(request))
            assert raw_read(grants.owner, reference=grant['credential_ref']) == 401
        finally:
            release.set()
        rows = pending.result(timeout=5).results
    assert len(rows) == 1 and rows[0].source_id == request['source_ids'][0]


def test_budget_and_post_create_failure_leave_no_grant_or_orphan(grants, monkeypatch):
    from atom_search.runtime.credentials import CredentialError
    from atom_search.runtime.storage import StorageBudgetError
    limit_operations(grants, 1)  # Failed reservations/creation must release capacity.
    request = issue_request(grants.owner)
    grants.approve(request)
    before = credential_inventory(grants.owner)
    @contextmanager
    def full(*args):
        raise StorageBudgetError('controlled')
        yield
    with monkeypatch.context() as patch:
        patch.setattr(grants.owner.repository.budget, 'reserve', full)
        with pytest.raises(ServiceError):
            grants.client.read_grant_issue(**request)
    assert credential_inventory(grants.owner) == before
    original = grants.owner.credentials.resolve
    calls = []
    def failure(reference):
        # CredentialStore.create does its own readback first; fail the manager's
        # subsequent resolve after the new reference has actually been returned.
        if reference not in before[0]:
            calls.append(reference)
            if len(calls) == 2:
                raise CredentialError('controlled')
        return original(reference)
    with monkeypatch.context() as patch:
        patch.setattr(grants.owner.credentials, 'resolve', failure)
        with pytest.raises(ServiceError):
            grants.client.read_grant_issue(**request)
    assert len(calls) == 2 and credential_inventory(grants.owner) == before
    assert grants.client.read_grant_issue(**request)['state'] == 'active'


@pytest.mark.parametrize('terminal', ['revoke', 'expire'])
def test_operation_limit_retains_terminal_tombstones_before_any_allocation(grants, monkeypatch, terminal):
    limit_operations(grants, 1)
    request, first = issue(grants)
    if terminal == 'revoke':
        grants.client.read_grant_revoke(**grant_key(request))
        state = 'revoked'
    else:
        grants.now[0] += request['ttl_seconds']
        state = 'expired'
    # Existing operations remain queryable/replayable without renewal at the limit.
    assert grants.client.read_grant_issue(**request)['state'] == state
    assert grants.client.read_grant_get(**grant_key(request))['grant_id'] == first['grant_id']
    grants.client.read_grant_revoke(**grant_key(request))
    before = credential_inventory(grants.owner)
    calls = []
    original_provider = grants.consent.approve
    original_create = grants.owner.credentials.create
    original_reserve = grants.owner.repository.budget.reserve
    def approve(value):
        calls.append('provider')
        return original_provider(value)
    def create(*args, **kwargs):
        calls.append('create')
        return original_create(*args, **kwargs)
    @contextmanager
    def reserve(*args, **kwargs):
        calls.append('budget')
        with original_reserve(*args, **kwargs) as value:
            yield value
    monkeypatch.setattr(grants.consent, 'approve', approve)
    monkeypatch.setattr(grants.owner.credentials, 'create', create)
    monkeypatch.setattr(grants.owner.repository.budget, 'reserve', reserve)
    second = issue_request(grants.owner)
    grants.approve(second)
    with pytest.raises(ServiceError) as failure:
        grants.client.read_grant_issue(**second)
    assert (failure.value.code, failure.value.status_code) == ('too_many_requests', 429)
    assert not calls and credential_inventory(grants.owner) == before


def test_operation_limit_concurrent_boundary_admits_only_one_distinct_operation(grants):
    limit_operations(grants, 1)
    requests = [issue_request(grants.owner) for _ in range(2)]
    for request in requests:
        grants.approve(request)
    def attempt(request):
        try:
            return grants.client.read_grant_issue(**request)
        except ServiceError as failure:
            return failure.code
    before = credential_inventory(grants.owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(attempt, requests))
    assert replies.count('too_many_requests') == 1
    admitted = [reply for reply in replies if isinstance(reply, dict)]
    assert len(admitted) == 1
    after = credential_inventory(grants.owner)
    assert len(after[0] - before[0]) == len(after[1] - before[1]) == 1
    key = grant_key(requests[replies.index(admitted[0])])
    assert grants.client.read_grant_get(**key)['grant_id'] == admitted[0]['grant_id']
    assert grants.client.read_grant_revoke(**key)['state'] == 'revoked'


def test_post_create_replacement_before_manager_identity_capture_is_not_deleted(grants, monkeypatch):
    original = grants.owner.credentials.create
    replacements = []
    def replace_after_create(*args, **kwargs):
        reference = original(*args, **kwargs)  # Genuine DPAPI create/readback.
        path = grants.owner.credentials.directory / (reference + '.dpapi')
        other = grants.owner.credentials.directory / 'foreign-replacement'
        other.write_bytes(b'foreign replacement must remain')
        other.replace(path)
        replacements.append(path)
        return reference
    monkeypatch.setattr(grants.owner.credentials, 'create', replace_after_create)
    request = issue_request(grants.owner)
    grants.approve(request)
    before_refs = credential_inventory(grants.owner)[0]
    with pytest.raises(ServiceError):
        grants.client.read_grant_issue(**request)  # Real resolve rejects replaced bytes.
    assert len(replacements) == 1 and replacements[0].exists()
    assert replacements[0].read_bytes() == b'foreign replacement must remain'
    assert credential_inventory(grants.owner)[0] == before_refs
    with pytest.raises(ServiceError) as missing:
        grants.client.read_grant_get(**grant_key(request))
    assert missing.value.code == 'result_not_found'


@pytest.mark.parametrize('prior', ['revoked_grant', 'legacy_access'])
def test_reused_reference_never_overwrites_prior_grant_or_legacy_access(grants, monkeypatch, prior):
    if prior == 'revoked_grant':
        original_request, original_grant = issue(grants)
        reference = original_grant['credential_ref']
        token = grants.owner.credentials.resolve(reference)
        grants.client.read_grant_revoke(**grant_key(original_request))
        expected_auth = 401
    else:
        from atom_search.service.contracts import AccessContext
        reference = grants.owner.issue_credential(AccessContext('legacy-reader', frozenset({'search'}), ()))
        token = grants.owner.credentials.resolve(reference)
        # A live cache entry must not be overwritten even when its file is gone.
        grants.owner.credentials.remove(reference)
        expected_auth = 200
    path = grants.owner.credentials.directory / (reference + '.dpapi')
    assert not path.exists()
    request = issue_request(grants.owner)
    grants.approve(request)
    before_refs = credential_inventory(grants.owner)[0]
    failure = None
    with monkeypatch.context() as patch:
        patch.setattr('atom_search.runtime.credentials.secrets.token_hex',
                      lambda _: reference.removeprefix('dpapi-v1-'))
        try:
            grants.client.read_grant_issue(**request)
        except ServiceError as caught:
            failure = caught
    # Fresh reference proof cannot become the new grant's authority; the old
    # token/cache semantics and old operation tombstone must remain unchanged.
    assert raw_read(grants.owner, reference=reference) == expected_auth
    assert raw_read(grants.owner, token=token) == expected_auth
    assert failure is not None and not path.exists()
    assert credential_inventory(grants.owner)[0] == before_refs
    if prior == 'revoked_grant':
        assert grants.client.read_grant_issue(**original_request)['state'] == 'revoked'
        assert grants.client.read_grant_get(**grant_key(original_request))['grant_id'] == original_grant['grant_id']
        assert grants.client.read_grant_revoke(**grant_key(original_request))['state'] == 'revoked'
    assert grants.client.status().api_version == '1'


@pytest.mark.parametrize('failure_kind', ['readback', 'collision', 'replacement'])
def test_credential_create_failure_removes_only_its_own_record(tmp_path, monkeypatch, failure_kind):
    from atom_search.runtime.credentials import CredentialStore, CredentialError
    store = CredentialStore(tmp_path / 'credential-rollback', 'fixture')
    reference = store.create('fixture')
    original = store.resolve
    before = {path.name for path in store.directory.iterdir()}
    if failure_kind == 'collision':
        monkeypatch.setattr('atom_search.runtime.credentials.secrets.token_hex', lambda _: reference.removeprefix('dpapi-v1-'))
    else:
        def broken(new_reference):
            if failure_kind == 'replacement':
                path = store.directory / (new_reference + '.dpapi')
                replacement = store.directory / 'replacement'
                replacement.write_bytes(b'foreign replacement must remain')
                replacement.replace(path)
            raise CredentialError('controlled readback failure')
        monkeypatch.setattr(store, 'resolve', broken)
    with pytest.raises(CredentialError):
        store.create('fixture')
    assert original(reference)
    after = {path.name for path in store.directory.iterdir()}
    if failure_kind == 'replacement':
        assert len(after - before) == 1
        assert next(path for path in store.directory.iterdir() if path.name not in before).read_bytes() == b'foreign replacement must remain'
    else:
        assert after == before


def test_owner_restart_rejects_old_grant_reference_and_cached_bearer(tmp_path):
    from atom_search.service.owner import open_service_owner
    from atom_search.service.read_grants import ReadGrantPolicy, ReadGrantRequest
    policy = ReadGrantPolicy(min_ttl_seconds=1, max_ttl_seconds=120,
                            allowed_capabilities=frozenset({'search', 'status'}))
    approved = []
    class Consent:
        def approve(self, request):
            return request in approved
    options = dict(automatic_catalog=False, read_grant_policy=policy,
                   read_grant_consent_provider=Consent())
    owner = open_service_owner(tmp_path / 'restart', tmp_path / 'missing-model', **options)
    restarted = None
    try:
        owner.start()
        client = ServiceClient(owner.endpoint_file, owner.credential_ref)
        request = dict(consumer_id=str(uuid.uuid4()), operation_id=str(uuid.uuid4()), consent_id=str(uuid.uuid4()),
            expected_settings_revision=owner.repository.snapshot().revision, source_ids=[], capabilities=['status'], ttl_seconds=37)
        approved.append(ReadGrantRequest(**{**request, 'source_ids': (), 'capabilities': frozenset({'status'})}))
        grant = client.read_grant_issue(**request)
        cached = owner.credentials.resolve(grant['credential_ref'])
        assert ServiceClient(owner.endpoint_file, grant['credential_ref']).status().api_version == '1'
        old_generation = owner.generation
        owner.close()
        restarted = open_service_owner(tmp_path / 'restart', tmp_path / 'missing-model', **options)
        restarted.start()
        assert restarted.generation != old_generation
        assert raw_read(restarted, token=cached) == raw_read(restarted, reference=grant['credential_ref']) == 401
        with pytest.raises(ServiceError) as failure:
            ServiceClient(restarted.endpoint_file, grant['credential_ref']).status()
        assert failure.value.code == 'authentication_required'
        assert ServiceClient(restarted.endpoint_file, None).status().api_version == '1'
    finally:
        if restarted is not None:
            restarted.close()
        owner.close()
