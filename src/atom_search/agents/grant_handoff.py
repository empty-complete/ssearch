"""Validate an owner grant response against an independent trusted host request.

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


def parse_read_grant(grant: dict, *,
                     expected_request: ReadGrantRequest | None = None) -> ReadGrantDescriptor:
    """Return a strict immutable snapshot bound to the host's explicit request.

    consent_id exists only in that trusted request/provider flow; this response
    does not prove consent. Revision and TTL are issuance bindings, not a local
    expiry clock or a policy to revoke grants on later settings changes.
    """
    request = _expected_request(expected_request)
    if type(grant) is not dict:
        raise error('invalid_request')
    raw = grant.copy()
    if any(type(key) is not str for key in raw) or set(raw) != _GRANT_FIELDS:
        raise error('invalid_request')
    reference = raw['credential_ref']
    if (type(reference) is not str or re.fullmatch(r'dpapi-v1-[0-9a-f]{64}', reference) is None
            or type(raw['state']) is not str or type(raw['cleanup_pending']) is not bool):
        raise error('invalid_request')

    descriptor = ReadGrantDescriptor(
        public_uuid(raw['grant_id']), public_uuid(raw['consumer_id']), public_uuid(raw['operation_id']),
        reference, raw['state'], public_uuid(raw['installation_id']), public_uuid(raw['generation']),
        _positive_integer(raw['expected_settings_revision']), _sources(raw['source_ids'], list),
        _capabilities(raw['capabilities'], list), _positive_integer(raw['ttl_seconds']), raw['cleanup_pending'],
    )
    if descriptor.state != 'active' or 'status' not in descriptor.capabilities:
        raise error('access_denied', 403)
    if (descriptor.consumer_id != request.consumer_id
            or descriptor.operation_id != request.operation_id
            or descriptor.expected_settings_revision != request.expected_settings_revision
            or descriptor.source_ids != request.source_ids
            or descriptor.capabilities != request.capabilities
            or descriptor.ttl_seconds != request.ttl_seconds):
        raise error('access_denied', 403)
    return descriptor


def connect_granted_read_api(endpoint_file: Path, grant: dict, *,
                             expected_request: ReadGrantRequest | None = None) -> AgentReadAPI:
    """Attach only after request bindings and the existing live owner are checked."""
    descriptor = parse_read_grant(grant, expected_request=expected_request)
    client = _connect_read_client(endpoint_file, descriptor.credential_ref)
    # This exact client has just validated endpoint/state/process identity and
    # authenticated a fresh nonce/status exchange. Its identity is not authority
    # supplied by the grant, and its endpoint owner reference is not this grant.
    if (descriptor.installation_id != client.descriptor['installation_id']
            or descriptor.generation != client.descriptor['generation']):
        client.close()
        raise error('service_identity_mismatch', 409)
    return AgentReadAPI(client)
