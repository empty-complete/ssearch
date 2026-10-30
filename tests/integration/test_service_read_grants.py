"""Owner read-grant admission over real P2 IPC, private DPAPI and owned PG.

These initial cases use only existing public transports, so the baseline RED
identifies missing disabled-route behavior rather than a future module import.
Policy/consent, TTL and revocation cases follow once that internal seam is agreed.
"""
import uuid

import pytest

from atom_search.service.contracts import SearchRequest, ServiceError


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
