"""An owner descriptor cannot supply its own trusted request or live authority."""
from dataclasses import FrozenInstanceError, replace
import importlib
import importlib.util
from pathlib import Path
from types import MappingProxyType

import pytest

from atom_search.service.contracts import ServiceError
from atom_search.service.read_grants import ReadGrantRequest


CONSUMER = 'a1000000-0000-4000-8000-000000000001'
OPERATION = 'b2000000-0000-4000-8000-000000000002'
SOURCE_A = 'c3000000-0000-4000-8000-000000000003'
SOURCE_B = 'd4000000-0000-4000-8000-000000000004'
GENERATION = 'e5000000-0000-4000-8000-000000000005'
INSTALLATION = 'f6000000-0000-4000-8000-000000000006'
GRANT = '71000000-0000-4000-8000-000000000007'
CONSENT = '82000000-0000-4000-8000-000000000008'
REFERENCE = 'dpapi-v1-' + 'a' * 64
SECRET = 'PRIVATE_SENTINEL-token-password'
ENDPOINT = Path('C:/PRIVATE_SENTINEL/engine/runtime/endpoint.json')
MANY_SOURCES = tuple(f'{index:08x}-0000-4000-8000-000000000001' for index in range(65))


def load_handoff():
    name = 'atom_search.agents.grant_handoff'
    assert importlib.util.find_spec(name) is not None, 'P7 grant handoff API is missing'
    return importlib.import_module(name)


def expected_request():
    return ReadGrantRequest(**dict(consumer_id=CONSUMER, operation_id=OPERATION,
        consent_id=CONSENT, expected_settings_revision=7, source_ids=(SOURCE_A, SOURCE_B),
        capabilities=frozenset({'search', 'status'}), ttl_seconds=37))


def expected_issuance(request=None):
    # Independent synthetic host constants, not fields read from wire_grant().
    # Real authenticated owner issuance is exercised in the integration tests.
    return load_handoff().IssuanceContext(
        request=expected_request() if request is None else request,
        credential_ref=REFERENCE, installation_id=INSTALLATION,
        generation=GENERATION, grant_id=GRANT)


def wire_grant():
    # Deliberately reverse source/cap order: their authority is an explicit set,
    # and P2 canonicalizes source ordering rather than assigning it meaning.
    return dict(grant_id=GRANT, consumer_id=CONSUMER, operation_id=OPERATION,
        credential_ref=REFERENCE, state='active', installation_id=INSTALLATION,
        generation=GENERATION, expected_settings_revision=7,
        source_ids=[SOURCE_B, SOURCE_A], capabilities=['status', 'search'],
        ttl_seconds=37, cleanup_pending=False)


@pytest.fixture
def forbid_client(monkeypatch):
    from atom_search.service.client import ServiceClient
    def forbidden(*args, **kwargs):
        pytest.fail('unvalidated grant/request reached ServiceClient')
    monkeypatch.setattr(ServiceClient, '__init__', forbidden)


def assert_safe_error(failure, code, capsys):
    assert failure.value.code == code
    text = str(failure.value)
    assert SECRET not in text and REFERENCE not in text and 'PRIVATE_SENTINEL' not in text
    assert failure.value.details == {}
    output = capsys.readouterr()
    assert output.out == ''
    assert REFERENCE not in output.err and SECRET not in output.err
    assert 'PRIVATE_SENTINEL' not in output.err and 'Traceback' not in output.err


@pytest.mark.parametrize('missing', list(wire_grant()))
def test_missing_wire_field_is_rejected_before_connect(missing, forbid_client, capsys):
    handoff = load_handoff()
    grant = wire_grant()
    del grant[missing]
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, grant, expected_issuance=expected_issuance())
    assert_safe_error(failure, 'invalid_request', capsys)


@pytest.mark.parametrize('shape', ['none', 'list', 'readonly_mapping', 'extra', 'consent'])
def test_wire_is_exact_dict_without_added_authority_fields(shape, forbid_client, capsys):
    handoff = load_handoff()
    grant = wire_grant()
    grant = (None if shape == 'none' else list(grant.items()) if shape == 'list'
             else MappingProxyType(grant) if shape == 'readonly_mapping'
             else {**grant, ('consent_id' if shape == 'consent' else 'token'): SECRET})
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, grant, expected_issuance=expected_issuance())
    assert_safe_error(failure, 'invalid_request', capsys)


@pytest.mark.parametrize(('field', 'value'), [
    ('grant_id', GRANT + '?scope=all'), ('consumer_id', CONSUMER.upper()),
    ('operation_id', False), ('installation_id', '../private'), ('generation', None),
    ('credential_ref', None), ('credential_ref', ''), ('credential_ref', 'dpapi-v1-' + 'A' * 64),
    ('credential_ref', REFERENCE + '\n'), ('state', True),
    ('expected_settings_revision', True), ('expected_settings_revision', 0),
    ('expected_settings_revision', 7.0), ('ttl_seconds', True), ('ttl_seconds', 0),
    ('ttl_seconds', '37'), ('cleanup_pending', 0),
    ('source_ids', None), ('source_ids', '[]'), ('source_ids', (SOURCE_A,)),
    ('source_ids', [False]), ('source_ids', [SOURCE_A, SOURCE_A]),
    ('source_ids', list(MANY_SOURCES)), ('source_ids', [SOURCE_A.upper()]),
    ('capabilities', None), ('capabilities', ('status', 'search')),
    ('capabilities', []), ('capabilities', ['status', 'status']),
    ('capabilities', ['status', 'owner']), ('capabilities', ['status', False]),
])
def test_wire_types_and_read_bounds_are_strict(field, value, forbid_client, capsys):
    handoff = load_handoff()
    grant = {**wire_grant(), field: value}
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, grant, expected_issuance=expected_issuance())
    assert_safe_error(failure, 'invalid_request', capsys)


@pytest.mark.parametrize(('field', 'value'), [
    ('consumer_id', CONSUMER.upper()), ('operation_id', None), ('consent_id', '../consent'),
    ('expected_settings_revision', True), ('expected_settings_revision', 0),
    ('ttl_seconds', True), ('ttl_seconds', -1),
    ('source_ids', [SOURCE_A, SOURCE_B]), ('source_ids', frozenset({SOURCE_A, SOURCE_B})),
    ('source_ids', (SOURCE_A, SOURCE_A)), ('source_ids', MANY_SOURCES),
    ('source_ids', (SOURCE_A, False)), ('capabilities', {'search', 'status'}),
    ('capabilities', ('search', 'status')), ('capabilities', frozenset()),
    ('capabilities', frozenset({'status', 'owner'})),
])
def test_direct_request_construction_does_not_bypass_validation(field, value, forbid_client, capsys):
    handoff = load_handoff()
    request = replace(expected_request(), **{field: value})
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, wire_grant(), expected_issuance=expected_issuance(request))
    assert_safe_error(failure, 'invalid_request', capsys)


@pytest.mark.parametrize('context', ['omitted', 'none', 'wire', 'mutated_request', 'request_object', 'request_only'])
def test_missing_or_descriptor_derived_context_cannot_authorize(context, forbid_client, capsys):
    handoff = load_handoff()
    arguments = {}
    if context == 'none':
        arguments['expected_issuance'] = None
    elif context == 'wire':
        arguments['expected_issuance'] = wire_grant()
    elif context == 'mutated_request':
        request = expected_request()
        object.__setattr__(request, 'source_ids', [SOURCE_A, SOURCE_B])
        arguments['expected_issuance'] = expected_issuance(request)
    elif context == 'request_object':
        arguments['expected_issuance'] = expected_request()
    elif context == 'request_only':
        arguments['expected_request'] = expected_request()
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, wire_grant(), **arguments)
    assert_safe_error(failure, 'invalid_request', capsys)


@pytest.mark.parametrize(('field', 'value'), [
    ('consumer_id', CONSENT), ('operation_id', CONSENT),
    ('expected_settings_revision', 8), ('source_ids', []),
    ('source_ids', [SOURCE_A]), ('capabilities', ['status']), ('ttl_seconds', 38),
    ('credential_ref', 'dpapi-v1-' + 'b' * 64), ('grant_id', CONSENT),
    ('installation_id', CONSENT), ('generation', CONSENT),
])
def test_descriptor_cannot_replace_independent_request_bindings(field, value, forbid_client, capsys):
    handoff = load_handoff()
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, {**wire_grant(), field: value},
                                         expected_issuance=expected_issuance())
    assert_safe_error(failure, 'access_denied', capsys)


@pytest.mark.parametrize('state', ['expired', 'revoked'])
def test_inactive_descriptor_is_refused_before_connect(state, forbid_client, capsys):
    handoff = load_handoff()
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, {**wire_grant(), 'state': state},
                                         expected_issuance=expected_issuance())
    assert_safe_error(failure, 'access_denied', capsys)


def test_status_is_required_explicitly_without_adding_it(forbid_client, capsys):
    handoff = load_handoff()
    request = replace(expected_request(), capabilities=frozenset({'search'}))
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, {**wire_grant(), 'capabilities': ['search']},
                                         expected_issuance=expected_issuance(request))
    assert_safe_error(failure, 'access_denied', capsys)


@pytest.mark.parametrize('empty', [False, True])
def test_projection_is_immutable_normalized_and_hides_reference_in_repr(empty):
    handoff = load_handoff()
    grant = wire_grant()
    request = expected_request()
    if empty:
        grant.update(source_ids=[], capabilities=['status'])
        request = replace(request, source_ids=(), capabilities=frozenset({'status'}))
    context = expected_issuance(request)
    assert REFERENCE not in repr(context)
    with pytest.raises(FrozenInstanceError):
        context.credential_ref = 'dpapi-v1-' + 'b' * 64
    snapshot = handoff.parse_read_grant(grant, expected_issuance=context)
    assert isinstance(snapshot, handoff.ReadGrantDescriptor)
    assert snapshot.source_ids == (() if empty else (SOURCE_A, SOURCE_B))
    assert type(snapshot.source_ids) is tuple
    assert snapshot.capabilities == (frozenset({'status'}) if empty else frozenset({'search', 'status'}))
    assert type(snapshot.capabilities) is frozenset
    assert snapshot.credential_ref == REFERENCE
    assert REFERENCE not in repr(snapshot)
    original_sources, original_caps = snapshot.source_ids, snapshot.capabilities
    grant['source_ids'].clear()
    grant['capabilities'].append('owner')
    assert snapshot.source_ids == original_sources and snapshot.capabilities == original_caps
    with pytest.raises(FrozenInstanceError):
        snapshot.consumer_id = CONSENT


def test_positive_ttl_is_matched_without_inventing_host_policy_bounds():
    handoff = load_handoff()
    ttl = 10 ** 12
    snapshot = handoff.parse_read_grant({**wire_grant(), 'ttl_seconds': ttl},
        expected_issuance=expected_issuance(replace(expected_request(), ttl_seconds=ttl)))
    assert snapshot.ttl_seconds == ttl


@pytest.mark.parametrize(('field', 'value'), [
    ('request', None), ('request', {'consumer_id': CONSUMER}),
    ('credential_ref', None), ('credential_ref', ''), ('credential_ref', False),
    ('credential_ref', 'dpapi-v1-' + 'A' * 64), ('credential_ref', REFERENCE + '\n'),
    ('installation_id', INSTALLATION.upper()), ('installation_id', None),
    ('generation', False), ('generation', '../private'), ('grant_id', GRANT + '?scope=all'),
])
def test_direct_issuance_construction_does_not_bypass_validation(field, value, forbid_client, capsys):
    handoff = load_handoff()
    context = replace(expected_issuance(), **{field: value})
    with pytest.raises(ServiceError) as failure:
        handoff.connect_granted_read_api(ENDPOINT, wire_grant(), expected_issuance=context)
    assert_safe_error(failure, 'invalid_request', capsys)


def test_request_only_parser_cannot_authorize(forbid_client, capsys):
    handoff = load_handoff()
    with pytest.raises(ServiceError) as failure:
        handoff.parse_read_grant(wire_grant(), expected_request=expected_request())
    assert_safe_error(failure, 'invalid_request', capsys)
