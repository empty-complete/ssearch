"""Bind a grant response to independent trusted host issuance context.

A future trusted owner/host provider must build IssuanceContext from its
independently selected request and the exact authenticated P2 issue/get reply.
Constructing the dataclass is NOT authentication. Do not supply context copied
from an untrusted grant to this in-process boundary. This adapter neither builds
trusted context from raw input nor invokes owner issue/get/revoke operations;
production provider wiring remains disabled.

The descriptor is metadata, not authority. Authentication, live grant expiry,
revocation and every read's scope remain enforced by the existing P2 service.
"""
from dataclasses import dataclass, field
from pathlib import Path
import re

from atom_search.http.security import error, public_uuid
from atom_search.service.read_grants import READ_CAPABILITIES, ReadGrantRequest

from .launcher import _connect_read_client
from .read_api import AgentReadAPI


_GRANT_FIELDS = frozenset({
    'grant_id', 'consumer_id', 'operation_id', 'credential_ref', 'state',
    'installation_id', 'generation', 'expected_settings_revision', 'source_ids',
    'capabilities', 'ttl_seconds', 'cleanup_pending',
})


@dataclass(frozen=True, slots=True)
class ReadGrantDescriptor:
    grant_id: str
    consumer_id: str
    operation_id: str
    credential_ref: str = field(repr=False)
    state: str
    installation_id: str
    generation: str
    expected_settings_revision: int
    source_ids: tuple[str, ...]
    capabilities: frozenset[str]
    ttl_seconds: int
    cleanup_pending: bool


@dataclass(frozen=True, slots=True)
class IssuanceContext:
    """Trusted host input; construction does not authenticate its provenance.

    Only a future trusted owner/host provider may assemble this from an
    independently selected request and an authenticated P2 issue/get reply.
    Caller-controlled copies of the raw descriptor are not trusted context.
    """
    request: ReadGrantRequest
    credential_ref: str = field(repr=False)
    installation_id: str
    generation: str
    grant_id: str


def _reference(value):
    if type(value) is not str or re.fullmatch(r'dpapi-v1-[0-9a-f]{64}', value) is None:
        raise error('invalid_request')
    return value


def _positive_integer(value):
    if type(value) is not int or value < 1:
        raise error('invalid_request')
    return value


def _sources(value, container):
    if type(value) is not container or len(value) > 64:
        raise error('invalid_request')
    sources = tuple(public_uuid(item) for item in value)
    if len(set(sources)) != len(sources):
        raise error('invalid_request')
    return tuple(sorted(sources))


def _capabilities(value, container):
    if (type(value) is not container or not value
            or any(type(item) is not str for item in value)):
        raise error('invalid_request')
    capabilities = frozenset(value)
    if len(capabilities) != len(value) or not capabilities <= READ_CAPABILITIES:
        raise error('invalid_request')
    return capabilities


def _expected_request(value):
    # A frozen dataclass's constructor does not validate annotations. Snapshot
    # and check the trusted host's fields independently of the wire response.
    if type(value) is not ReadGrantRequest:
        raise error('invalid_request')
    try:
        request = ReadGrantRequest(value.consumer_id, value.operation_id, value.consent_id,
            value.expected_settings_revision, value.source_ids, value.capabilities, value.ttl_seconds)
    except AttributeError:
        raise error('invalid_request') from None
    return ReadGrantRequest(
        public_uuid(request.consumer_id), public_uuid(request.operation_id),
        public_uuid(request.consent_id), _positive_integer(request.expected_settings_revision),
        _sources(request.source_ids, tuple), _capabilities(request.capabilities, frozenset),
        _positive_integer(request.ttl_seconds),
    )


def _expected_issuance(value):
    if type(value) is not IssuanceContext:
        raise error('invalid_request')
    try:
        snapshot = IssuanceContext(value.request, value.credential_ref,
            value.installation_id, value.generation, value.grant_id)
    except AttributeError:
        raise error('invalid_request') from None
    return IssuanceContext(_expected_request(snapshot.request), _reference(snapshot.credential_ref),
        public_uuid(snapshot.installation_id), public_uuid(snapshot.generation), public_uuid(snapshot.grant_id))


def _parse_read_grant(grant, issuance):
    request = issuance.request
    if type(grant) is not dict:
        raise error('invalid_request')
    raw = grant.copy()
    if any(type(key) is not str for key in raw) or set(raw) != _GRANT_FIELDS:
        raise error('invalid_request')
    reference = _reference(raw['credential_ref'])
    if type(raw['state']) is not str or type(raw['cleanup_pending']) is not bool:
        raise error('invalid_request')

    descriptor = ReadGrantDescriptor(
        public_uuid(raw['grant_id']), public_uuid(raw['consumer_id']), public_uuid(raw['operation_id']),
        reference, raw['state'], public_uuid(raw['installation_id']), public_uuid(raw['generation']),
        _positive_integer(raw['expected_settings_revision']), _sources(raw['source_ids'], list),
        _capabilities(raw['capabilities'], list), _positive_integer(raw['ttl_seconds']), raw['cleanup_pending'],
    )
    if descriptor.state != 'active' or 'status' not in descriptor.capabilities:
        raise error('access_denied', 403)
    if (descriptor.credential_ref != issuance.credential_ref
            or descriptor.grant_id != issuance.grant_id
            or descriptor.installation_id != issuance.installation_id
            or descriptor.generation != issuance.generation
            or descriptor.consumer_id != request.consumer_id
            or descriptor.operation_id != request.operation_id
            or descriptor.expected_settings_revision != request.expected_settings_revision
            or descriptor.source_ids != request.source_ids
            or descriptor.capabilities != request.capabilities
            or descriptor.ttl_seconds != request.ttl_seconds):
        raise error('access_denied', 403)
    return descriptor


def parse_read_grant(grant: dict, *, expected_issuance: IssuanceContext | None = None,
                     expected_request: ReadGrantRequest | None = None) -> ReadGrantDescriptor:
    """Validate against independent host issuance, never context made from raw input.

    Constructing IssuanceContext is not authentication: a future trusted host
    must supply it from its selected request and authenticated owner issue/get
    reply. No owner operation or provider is wired here. Request-only admission
    is refused; the old keyword is retained solely for that safe refusal.
    consent_id is not response proof. TTL/revision remain issuance bindings,
    not a local expiry clock or automatic revocation on later settings changes.
    """
    if expected_request is not None:
        raise error('invalid_request')
    return _parse_read_grant(grant, _expected_issuance(expected_issuance))


def connect_granted_read_api(endpoint_file: Path, grant: dict, *,
                             expected_issuance: IssuanceContext | None = None,
                             expected_request: ReadGrantRequest | None = None) -> AgentReadAPI:
    """Attach using independent trusted host issuance and the verified live owner.

    The future trusted host/provider must assemble context from its selected
    request and authenticated owner issue/get reply before handling raw input.
    The context constructor is not authentication; untrusted copied context is
    forbidden here. This adapter supplies no provider or owner operations.
    """
    if expected_request is not None:
        raise error('invalid_request')
    issuance = _expected_issuance(expected_issuance)
    _parse_read_grant(grant, issuance)
    client = _connect_read_client(endpoint_file, issuance.credential_ref)
    # This exact client has just validated endpoint/state/process identity and
    # authenticated a fresh nonce/status exchange. Its identity is not authority
    # supplied by the grant, and its endpoint owner reference is not this grant.
    if (issuance.installation_id != client.descriptor['installation_id']
            or issuance.generation != client.descriptor['generation']):
        client.close()
        raise error('service_identity_mismatch', 409)
    return AgentReadAPI(client)
