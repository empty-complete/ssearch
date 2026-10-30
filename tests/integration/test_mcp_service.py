"""MCP attaches to a validated existing P2 owner without gaining authority."""
import asyncio
import importlib
import importlib.util
import json
import threading
import uuid

import pytest

from atom_search.service.contracts import ServiceError


def load_transport():
    for name in ('mcp_adapter', 'launcher'):
        assert importlib.util.find_spec('atom_search.agents.' + name) is not None, (
            'P7 public MCP ' + name + ' API is missing')
    return (importlib.import_module('atom_search.agents.mcp_adapter'),
            importlib.import_module('atom_search.agents.launcher'))


@pytest.mark.parametrize('reference', [None, '', ' ', False, 7, '../private',
    'dpapi-v1-' + 'A' * 64, 'dpapi-v1-' + '0' * 63, 'dpapi-v1-' + '0' * 64 + '\n'])
def test_invalid_reference_is_rejected_before_client_creation(monkeypatch, tmp_path, reference):
    transport = load_transport()
    from atom_search.service.client import ServiceClient
    _, launcher = transport

    def forbidden(*args, **kwargs):
        pytest.fail('invalid credential reference reached ServiceClient/owner fallback')
    monkeypatch.setattr(ServiceClient, '__init__', forbidden)
    with pytest.raises(ServiceError) as failure:
        launcher.connect_read_api(tmp_path / 'endpoint.json', reference)
    assert failure.value.code == 'authentication_required'
    if isinstance(reference, str) and reference.strip():
        assert reference not in str(failure.value)


def test_unresolved_reference_does_not_fall_back_to_owner(owner_profile, capsys):
    transport = load_transport()
    _, launcher = transport
    reference = 'dpapi-v1-' + '0' * 64
    with pytest.raises(ServiceError) as failure:
        launcher.connect_read_api(owner_profile.endpoint_file, reference)
    assert failure.value.code == 'authentication_required'
    assert reference not in str(failure.value)
    captured = capsys.readouterr()
    assert captured.out == '' and reference not in captured.err


def test_stdio_attach_failure_is_safe_and_writes_no_protocol_noise(owner_profile, capsys):
    transport = load_transport()
    adapter, _ = transport
    reference = 'dpapi-v1-' + '0' * 64
    assert adapter.serve_stdio(owner_profile.endpoint_file, reference) != 0
    captured = capsys.readouterr()
    assert captured.out == ''
    assert reference not in captured.err and 'Traceback' not in captured.err


def test_foreign_profile_endpoint_is_rejected(owner_profile, read_client, tmp_path):
    transport = load_transport()
    _, launcher = transport
    endpoint = tmp_path / 'foreign' / 'engine' / 'runtime' / 'endpoint.json'
    endpoint.parent.mkdir(parents=True)
    endpoint.write_bytes(owner_profile.endpoint_file.read_bytes())
    with pytest.raises(ServiceError) as failure:
        launcher.connect_read_api(endpoint, read_client.credential_ref)
    assert failure.value.code == 'service_identity_mismatch'


def test_two_scoped_mcp_clients_share_owner_and_keep_private_sources_hidden(owner_profile, read_client, owner_client):
    transport = load_transport()
    from mcp import Client
    adapter, launcher = transport
    before = owner_profile.endpoint_file.read_bytes()
    sources = owner_profile.repository.snapshot().settings['sources']

    async def scenario():
        first = adapter.create_mcp_server(launcher.connect_read_api(
            owner_profile.endpoint_file, read_client.credential_ref))
        second = adapter.create_mcp_server(launcher.connect_read_api(
            owner_profile.endpoint_file, read_client.credential_ref))
        async with Client(first) as client_a, Client(second) as client_b:
            result = (await client_a.call_tool('search', {'query': 'drawing', 'source_ids': None})).structured_content
            assert result['error'] is None
            assert [hit['source_id'] for hit in result['data']['results']] == [sources[0]['id']]
            hit = result['data']['results'][0]
            fetched = (await client_b.call_tool('fetch', {'result_id': hit['result_id']})).structured_content
            assert fetched['error'] is None and fetched['data']['full_path'] == hit['full_path']
            empty = (await client_b.call_tool('search', {'query': 'drawing', 'source_ids': []})).structured_content
            assert empty['error'] is None and empty['data']['results'] == []
            denied = (await client_b.call_tool('search', {'query': 'drawing', 'source_ids': [sources[1]['id']]})).structured_content
            assert denied['error']['code'] == 'source_scope_denied'
            for name in ('health', 'settings_get'):
                reply = (await client_b.call_tool(name, {})).structured_content
                assert reply['error'] is None
                encoded = json.dumps(reply)
                assert sources[1]['path'] not in encoded and sources[1]['id'] not in encoded
                assert read_client.credential_ref not in encoded and owner_profile.credential_ref not in encoded
    asyncio.run(scenario())
    assert owner_profile.endpoint_file.read_bytes() == before
    assert owner_client.status().liveness == 'alive'


def test_cancelled_request_does_not_stop_shared_service(owner_profile, read_client, owner_client, monkeypatch):
    transport = load_transport()
    from mcp import Client
    from atom_search.service.client import ServiceClient
    adapter, launcher = transport
    started, release = threading.Event(), threading.Event()
    original = ServiceClient.search

    def delayed(client, request):
        started.set()
        assert release.wait(10), 'test did not release controlled search'
        return original(client, request)
    monkeypatch.setattr(ServiceClient, 'search', delayed)

    async def scenario():
        servers = [adapter.create_mcp_server(launcher.connect_read_api(
            owner_profile.endpoint_file, read_client.credential_ref)) for _ in range(2)]
        async with Client(servers[0]) as client_a, Client(servers[1]) as client_b:
            call = asyncio.create_task(client_a.call_tool('search', {'query': 'drawing'}))
            try:
                assert await asyncio.to_thread(started.wait, 5), 'MCP search did not begin'
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call
                # The first sync HTTP read is still occupied. Other clients must
                # remain responsive, and cancellation must not stop the owner.
                health = await asyncio.wait_for(client_b.call_tool('health', {}), 5)
                assert health.structured_content['data']['liveness'] == 'alive'
            finally:
                release.set()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert owner_client.status().liveness == 'alive'


@pytest.mark.parametrize('field', ['installation_id', 'generation'])
def test_endpoint_identity_is_rechecked_after_attach(owner_profile, read_client, field):
    transport = load_transport()
    from mcp import Client
    from atom_search.runtime import _windows
    adapter, launcher = transport
    api = launcher.connect_read_api(owner_profile.endpoint_file, read_client.credential_ref)
    path = owner_profile.endpoint_file
    original = json.loads(path.read_bytes())

    async def scenario():
        async with Client(adapter.create_mcp_server(api)) as client:
            assert (await client.call_tool('health', {})).structured_content['error'] is None
            changed = dict(original, **{field: str(uuid.uuid4())})
            try:
                _windows.atomic_json(path, changed)
                reply = (await client.call_tool('health', {})).structured_content
                assert reply['data'] is None
                assert reply['error']['code'] == 'service_identity_mismatch'
                with pytest.raises(ServiceError) as failure:
                    launcher.connect_read_api(path, read_client.credential_ref)
                assert failure.value.code == 'service_identity_mismatch'
            finally:
                _windows.atomic_json(path, original)
            assert (await client.call_tool('health', {})).structured_content['error'] is None
    asyncio.run(scenario())
