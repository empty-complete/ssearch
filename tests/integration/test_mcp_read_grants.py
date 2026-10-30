"""Grant handoff over real P2 IPC/DPAPI/PG and the official SDK transport.

The reused fixture supplies an explicit synthetic consent provider/policy and
restores only its own grant records. Production issuance stays disabled.
"""
import asyncio
from dataclasses import replace
import importlib
import importlib.util
import json
import threading
import uuid

import pytest

# SDK stdio_client binds its default stderr at import. Import during collection,
# before any capsys fixture can bind that default to a stream without fileno().
from mcp import Client

from atom_search.service.client import ServiceClient
from atom_search.service.contracts import SearchRequest, ServiceError
from atom_search.service.read_grants import ReadGrantRequest

from .test_service_read_grants import grants, grant_key, issue


def load_handoff():
    name = 'atom_search.agents.grant_handoff'
    assert importlib.util.find_spec(name) is not None, 'P7 grant handoff API is missing'
    return importlib.import_module(name)


def trusted_request(request):
    # Derived from the host's independent issuance request, never its response.
    return ReadGrantRequest(**{**request, 'source_ids': tuple(request['source_ids']),
                              'capabilities': frozenset(request['capabilities'])})


def trusted_issuance(request, issued_reply):
    # Test host consumes the real authenticated owner IPC issuance reply BEFORE
    # raw input can be substituted. The production provider is still unwired.
    return load_handoff().IssuanceContext(request=trusted_request(request),
        credential_ref=issued_reply['credential_ref'], installation_id=issued_reply['installation_id'],
        generation=issued_reply['generation'], grant_id=issued_reply['grant_id'])


def assert_public_failure(failure, code, grant, owner, capsys):
    assert failure.value.code == code
    assert failure.value.details == {}
    output = capsys.readouterr()
    assert output.out == ''
    for protected in (grant['credential_ref'], owner.credential_ref, str(owner.profile_dir)):
        assert protected not in str(failure.value) and protected not in output.err
    assert 'Traceback' not in output.err


def test_four_mcp_reads_use_granted_sources_without_owner_bootstrap_authority(grants, capsys):
    handoff = load_handoff()
    from atom_search.agents.mcp_adapter import create_mcp_server
    request, grant = issue(grants)
    expected = trusted_issuance(request, grant)
    endpoint_before = grants.owner.endpoint_file.read_bytes()
    assert grant['credential_ref'] != grants.owner.credential_ref
    api = handoff.connect_granted_read_api(grants.owner.endpoint_file, grant, expected_issuance=expected)
    sources = grants.owner.repository.snapshot().settings['sources']

    async def scenario():
        async with Client(create_mcp_server(api)) as client:
            search = (await client.call_tool('search', {'query': 'drawing'})).structured_content
            assert search['error'] is None
            assert [hit['source_id'] for hit in search['data']['results']] == [sources[0]['id']]
            hit = search['data']['results'][0]
            fetched = (await client.call_tool('fetch', {'result_id': hit['result_id']})).structured_content
            assert fetched['error'] is None and fetched['data']['full_path'] == hit['full_path']
            health = (await client.call_tool('health', {})).structured_content
            settings = (await client.call_tool('settings_get', {})).structured_content
            assert health['error'] is None and health['data']['liveness'] == 'alive'
            assert settings['error'] is None
            assert [s['id'] for s in settings['data']['settings']['sources']] == [sources[0]['id']]
            denied = (await client.call_tool('search', {
                'query': 'drawing', 'source_ids': [sources[1]['id']]})).structured_content
            assert denied['error']['code'] == 'source_scope_denied'
            for value in (search, fetched, health, settings, denied):
                wire = json.dumps(value)
                assert sources[1]['id'] not in wire
                assert grant['credential_ref'] not in wire and grants.owner.credential_ref not in wire
                assert 'CONTENT MUST NEVER BE FETCHED' not in wire
    asyncio.run(scenario())
    assert grants.owner.endpoint_file.read_bytes() == endpoint_before
    assert grants.client.read_grant_get(**grant_key(request))['grant_id'] == grant['grant_id']
    assert capsys.readouterr().out == ''


def test_explicit_empty_grant_scope_stays_empty(grants):
    handoff = load_handoff()
    request, grant = issue(grants, source_ids=[])
    expected = trusted_issuance(request, grant)
    api = handoff.connect_granted_read_api(grants.owner.endpoint_file, grant,
                                          expected_issuance=expected)
    result = api.search('drawing')
    assert result['error'] is None and result['data']['results'] == []
    settings = api.settings_get()
    assert settings['error'] is None and settings['data']['settings']['sources'] == []


def test_broader_grant_reference_cannot_replace_restricted_issuance(grants, capsys):
    handoff = load_handoff()
    request, restricted = issue(grants, capabilities=['search', 'status'])
    expected = trusted_issuance(request, restricted)
    sources = grants.owner.repository.snapshot().settings['sources']
    _, broader = issue(grants, source_ids=sorted(source['id'] for source in sources))
    # Both are genuine grants of the same live owner. Establish that the
    # substituted reference authenticates and really reaches the private source.
    client = ServiceClient(grants.owner.endpoint_file, broader['credential_ref'])
    try:
        hits = client.search(SearchRequest('drawing'))
        assert {hit.source_id for hit in hits.results} == {source['id'] for source in sources}
    finally:
        client.close()
    substituted = {**restricted, 'credential_ref': broader['credential_ref']}
    # All restricted request/descriptor metadata is unchanged. Its reference
    # must remain bound to that independent issuance, not any valid credential.
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(grants.owner.endpoint_file, substituted,
                                         expected_issuance=expected)
    assert_public_failure(failure, 'access_denied', substituted, grants.owner, capsys)


def test_statusless_real_grant_is_not_silently_augmented(grants, capsys):
    handoff = load_handoff()
    request, grant = issue(grants, capabilities=['search', 'fetch'])
    expected = trusted_issuance(request, grant)
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(grants.owner.endpoint_file, grant,
                                         expected_issuance=expected)
    assert_public_failure(failure, 'access_denied', grant, grants.owner, capsys)
    assert grants.client.read_grant_get(**grant_key(request))['capabilities'] == ['fetch', 'search']


def test_status_probe_does_not_prove_or_add_other_capabilities(grants):
    handoff = load_handoff()
    from atom_search.agents.mcp_adapter import create_mcp_server
    request, grant = issue(grants, capabilities=['status'])
    expected = trusted_issuance(request, grant)
    api = handoff.connect_granted_read_api(grants.owner.endpoint_file, grant,
                                          expected_issuance=expected)

    async def scenario():
        async with Client(create_mcp_server(api)) as client:
            health = await client.call_tool('health', {})
            assert health.structured_content['error'] is None
            for name, arguments in (
                ('search', {'query': 'drawing'}),
                ('fetch', {'result_id': str(uuid.uuid4())}), ('settings_get', {})):
                result = await client.call_tool(name, arguments)
                assert result.is_error
                assert result.structured_content['data'] is None
                assert result.structured_content['error']['code'] == 'access_denied'
    asyncio.run(scenario())
    assert grants.client.read_grant_get(**grant_key(request))['capabilities'] == ['status']


@pytest.mark.parametrize('ending', ['expire', 'revoke'])
def test_cached_descriptor_and_mcp_client_cannot_admit_after_grant_ends(grants, ending, capsys):
    handoff = load_handoff()
    from atom_search.agents.mcp_adapter import create_mcp_server
    request, grant = issue(grants)
    expected = trusted_issuance(request, grant)
    api = handoff.connect_granted_read_api(grants.owner.endpoint_file, grant, expected_issuance=expected)

    async def scenario():
        async with Client(create_mcp_server(api)) as client:
            assert (await client.call_tool('health', {})).structured_content['error'] is None
            if ending == 'expire':
                grants.now[0] += request['ttl_seconds']
            else:
                grants.client.read_grant_revoke(**grant_key(request))
            reply = await client.call_tool('health', {})
            assert reply.is_error
            assert reply.structured_content['error']['code'] == 'authentication_required'
            with pytest.raises(ServiceError) as failure:
                handoff.connect_granted_read_api(grants.owner.endpoint_file, grant, expected_issuance=expected)
            assert_public_failure(failure, 'authentication_required', grant, grants.owner, capsys)
    asyncio.run(scenario())
    terminal = grants.client.read_grant_get(**grant_key(request))
    assert terminal['state'] == ('expired' if ending == 'expire' else 'revoked')
    assert terminal['grant_id'] == grant['grant_id']
    assert grants.client.status().liveness == 'alive'


@pytest.mark.parametrize('field', ['installation_id', 'generation'])
def test_grant_identity_must_match_the_same_verified_live_client(grants, field, capsys):
    handoff = load_handoff()
    request, grant = issue(grants)
    expected = trusted_issuance(request, grant)
    identity = str(uuid.uuid4())
    changed = {**grant, field: identity}
    # Deliberately matching synthetic claims pass the raw/context binding check;
    # the authenticated live owner must still disagree after the handshake.
    changed_context = replace(expected, **{field: identity})
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(grants.owner.endpoint_file, changed,
                                         expected_issuance=changed_context)
    assert_public_failure(failure, 'service_identity_mismatch', grant, grants.owner, capsys)
    assert grants.client.status().liveness == 'alive'


def test_handoff_rejects_wrong_live_nonce_proof(grants, monkeypatch, capsys):
    handoff = load_handoff()
    request, grant = issue(grants)
    expected = trusted_issuance(request, grant)
    # Real server/DPAPI/HTTP remain in use; only the peer's proof is corrupted.
    monkeypatch.setattr('atom_search.http.server.proof', lambda *args: '0' * 64)
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(grants.owner.endpoint_file, grant,
                                         expected_issuance=expected)
    assert_public_failure(failure, 'service_identity_mismatch', grant, grants.owner, capsys)


def test_two_granted_mcp_clients_cancel_without_revoking_or_stopping_owner(grants, monkeypatch):
    handoff = load_handoff()
    from atom_search.agents.mcp_adapter import create_mcp_server
    request, grant = issue(grants)
    expected = trusted_issuance(request, grant)
    apis = [handoff.connect_granted_read_api(grants.owner.endpoint_file, grant,
        expected_issuance=expected) for _ in range(2)]
    started, release = threading.Event(), threading.Event()
    original = ServiceClient.search

    def delayed(client, query):
        started.set()
        assert release.wait(10), 'test did not release granted search'
        return original(client, query)
    monkeypatch.setattr(ServiceClient, 'search', delayed)

    async def scenario():
        async with Client(create_mcp_server(apis[0])) as first, Client(create_mcp_server(apis[1])) as second:
            call = asyncio.create_task(first.call_tool('search', {'query': 'drawing'}))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call
                reply = await asyncio.wait_for(second.call_tool('health', {}), 5)
                assert reply.structured_content['data']['liveness'] == 'alive'
            finally:
                release.set()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
    current = grants.client.read_grant_get(**grant_key(request))
    assert current['state'] == 'active' and current['grant_id'] == grant['grant_id']
    assert grants.client.status().liveness == 'alive'


def test_owner_restart_rejects_old_grant_handoff_and_cached_api(tmp_path):
    handoff = load_handoff()
    from atom_search.service.owner import open_service_owner
    from atom_search.service.read_grants import ReadGrantPolicy
    approved = []
    class Consent:
        def approve(self, request):
            return request in approved
    policy = ReadGrantPolicy(1, 120, frozenset({'status'}), 8)
    options = dict(automatic_catalog=False, read_grant_policy=policy,
                   read_grant_consent_provider=Consent())
    profile = tmp_path / 'grant-handoff-restart'
    owner = open_service_owner(profile, tmp_path / 'missing-model', **options)
    restarted = None
    try:
        owner.start()
        selected_request = ReadGrantRequest(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()),
            owner.repository.snapshot().revision, (), frozenset({'status'}), 37)
        approved.append(selected_request)
        host = ServiceClient(owner.endpoint_file, owner.credential_ref)
        request = {**vars(selected_request), 'source_ids': [], 'capabilities': ['status']}
        grant = host.read_grant_issue(**request)
        expected = trusted_issuance(request, grant)
        api = handoff.connect_granted_read_api(owner.endpoint_file, grant, expected_issuance=expected)
        assert api.health()['error'] is None
        owner.close()
        restarted = open_service_owner(profile, tmp_path / 'missing-model', **options)
        restarted.start()
        assert api.health()['error']['code'] == 'service_identity_mismatch'
        with pytest.raises(ServiceError) as failure:
            handoff.connect_granted_read_api(restarted.endpoint_file, grant, expected_issuance=expected)
        assert failure.value.code == 'authentication_required'
        assert ServiceClient(restarted.endpoint_file, restarted.credential_ref).status().liveness == 'alive'
    finally:
        if restarted is not None:
            restarted.close()
        owner.close()
