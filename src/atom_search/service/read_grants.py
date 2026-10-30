"""Owner-issued runtime read grants. Trusted policy/consent are disabled by default."""
from copy import deepcopy
from dataclasses import dataclass
import time
from typing import Callable, Protocol
import uuid

from atom_search.http.security import error, public_uuid
from atom_search.runtime import _windows
from .contracts import AccessContext, ServiceError


READ_CAPABILITIES = frozenset({'search', 'fetch', 'status', 'settings_get'})


@dataclass(frozen=True)
class ReadGrantRequest:
    consumer_id: str
    operation_id: str
    consent_id: str
    expected_settings_revision: int
    source_ids: tuple[str, ...]
    capabilities: frozenset[str]
    ttl_seconds: int


@dataclass(frozen=True)
class ReadGrantPolicy:
    min_ttl_seconds: int
    max_ttl_seconds: int
    allowed_capabilities: frozenset[str]

    def __post_init__(self):
        if (type(self.min_ttl_seconds) is not int or type(self.max_ttl_seconds) is not int
                or not 0 < self.min_ttl_seconds <= self.max_ttl_seconds
                or type(self.allowed_capabilities) is not frozenset
                or not self.allowed_capabilities or not self.allowed_capabilities <= READ_CAPABILITIES):
            raise ValueError('invalid_read_grant_policy')


class ConsentProvider(Protocol):
    def approve(self, request: ReadGrantRequest) -> bool:
        """Confirm an already trusted decision for this EXACT immutable request."""
        ...


@dataclass
class _Grant:
    request: ReadGrantRequest
    descriptor: dict
    expires_at: float


class ReadGrantStore:
    def __init__(self, owner, *, policy: ReadGrantPolicy | None = None,
                 consent_provider: ConsentProvider | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.owner, self.policy, self.consent_provider, self.clock = owner, policy, consent_provider, clock
        # Protected by the existing owner's credential lock, not a second auth service.
        self._operations: dict[str, _Grant] = {}
        self._references: dict[str, _Grant] = {}

    def _request(self, arguments):
        fields = set(ReadGrantRequest.__dataclass_fields__)
        if set(arguments) != fields:
            raise error('invalid_request')
        for key in ('consumer_id', 'operation_id', 'consent_id'):
            public_uuid(arguments[key])
        sources, caps, ttl, revision = (arguments[key] for key in
            ('source_ids', 'capabilities', 'ttl_seconds', 'expected_settings_revision'))
        if (type(sources) is not list or len(sources) > 64
                or type(caps) is not list or not caps or any(type(cap) is not str for cap in caps)
                or type(ttl) is not int or not self.policy.min_ttl_seconds <= ttl <= self.policy.max_ttl_seconds
                or type(revision) is not int or revision < 1):
            raise error('invalid_request')
        sources = tuple(public_uuid(source) for source in sources)
        if len(set(sources)) != len(sources) or len(set(caps)) != len(caps) or not set(caps) <= self.policy.allowed_capabilities:
            raise error('invalid_request')
        return ReadGrantRequest(arguments['consumer_id'], arguments['operation_id'], arguments['consent_id'],
                                revision, tuple(sorted(sources)), frozenset(caps), ttl)

    def _scope_revision(self, request):
        snapshot = self.owner.repository.snapshot()
        if snapshot.revision != request.expected_settings_revision:
            raise error('revision_conflict', 409)
        enabled = {source['id'] for source in snapshot.settings['sources'] if source['enabled']}
        if not set(request.source_ids) <= enabled:
            raise error('source_scope_denied', 403)

    def active(self, reference):
        """Called only under owner._credential_lock; never does IO or waits."""
        grant = self._references.get(reference)
        if grant is None:
            return True  # Preserve all existing non-grant credential semantics.
        value = grant.descriptor
        if (value['state'] == 'active' and (self.clock() >= grant.expires_at
                or value['generation'] != self.owner.generation
                or value['installation_id'] != self.owner.installation_id)):
            value['state'] = 'expired'
        return value['state'] == 'active'

    def issue(self, **arguments):
        if self.policy is None or self.consent_provider is None or self.owner.attached or self.owner.credentials is None:
            raise error('service_unavailable', 503)
        request = self._request(arguments)
        reference = created_path = created_identity = directory_identity = None
        try:
            # Real profile admission serializes duplicate issuance. Never hold the
            # credential-map lock while awaiting guard/budget/provider/DPAPI IO.
            with self.owner.repository.mutation():
                with self.owner._credential_lock:
                    existing = self._operations.get(request.operation_id)
                    if existing is not None:
                        if existing.request != request:
                            raise error('revision_conflict', 409)
                        self.active(existing.descriptor['credential_ref'])
                        return deepcopy(existing.descriptor)
                    if len(self.owner._access) >= 256:
                        raise error('too_many_requests', 429)
                self._scope_revision(request)
                if self.consent_provider.approve(request) is not True:
                    raise error('access_denied', 403)
                self._scope_revision(request)
                with self.owner.repository.budget.reserve(16384, 'service_credential'):
                    reference = self.owner.credentials.create(self.owner.generation)
                    created_path = self.owner.credentials.directory / (reference + '.dpapi')
                    directory_identity = _windows._file_identity(self.owner.credentials.directory)
                    created_identity = _windows._file_identity(created_path)
                    secret = self.owner.credentials.resolve(reference)
                    self._scope_revision(request)
                    access = AccessContext('read-grant:' + request.consumer_id, request.capabilities, request.source_ids)
                    grant = _Grant(request, dict(grant_id=str(uuid.uuid4()), consumer_id=request.consumer_id,
                        operation_id=request.operation_id, credential_ref=reference, state='active',
                        installation_id=self.owner.installation_id, generation=self.owner.generation,
                        expected_settings_revision=request.expected_settings_revision,
                        source_ids=list(request.source_ids), capabilities=sorted(request.capabilities),
                        ttl_seconds=request.ttl_seconds, cleanup_pending=False), self.clock() + request.ttl_seconds)
                    with self.owner._credential_lock:
                        self._operations[request.operation_id] = grant
                        self._references[reference] = grant
                        self.owner._access[reference] = (secret, access)
                    return deepcopy(grant.descriptor)
        except BaseException as failure:
            if reference is not None:
                with self.owner._credential_lock:
                    self.owner._access.pop(reference, None)
                    self._references.pop(reference, None)
                    self._operations.pop(request.operation_id, None)
                try:
                    if (created_identity is not None
                            and _windows._file_identity(self.owner.credentials.directory) == directory_identity
                            and _windows._file_identity(created_path) == created_identity):
                        self.owner.credentials.remove(reference)
                except Exception:
                    pass  # An unpublished orphan can never authenticate.
            if isinstance(failure, ServiceError):
                raise
            if not isinstance(failure, Exception):
                raise
            raise error('service_unavailable', 503, retryable=True) from None

    def _find(self, consumer_id, operation_id):
        public_uuid(consumer_id)
        public_uuid(operation_id)
        grant = self._operations.get(operation_id)
        if grant is None or grant.request.consumer_id != consumer_id:
            raise error('result_not_found', 404)
        self.active(grant.descriptor['credential_ref'])
        return grant

    def get(self, *, consumer_id, operation_id):
        with self.owner._credential_lock:
            return deepcopy(self._find(consumer_id, operation_id).descriptor)

    def revoke(self, *, consumer_id, operation_id):
        with self.owner._credential_lock:
            grant = self._find(consumer_id, operation_id)
            reference = grant.descriptor['credential_ref']
            grant.descriptor['state'] = 'revoked'
            self.owner._access.pop(reference, None)  # Block NEW admissions before any file cleanup/ACK.
        pending = False
        try:
            self.owner.credentials.remove(reference)
        except Exception:
            pending = True
        with self.owner._credential_lock:
            grant.descriptor['cleanup_pending'] = pending
            return deepcopy(grant.descriptor)
