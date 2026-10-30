"""Immutable server-owned planning values over the existing P2 authority.

These types do not authenticate a caller. Only the owner may construct an
AgentPrincipal from its authenticated AccessContext and current policy revision.
No transport accepts these values as a grant or an executable request.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import math
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from atom_search.service.contracts import (
    AccessContext, FieldError, HealthSnapshot, ServiceError, SettingChange, SettingsValidation,
)


def repair_error(code: str, status: int = 400) -> ServiceError:
    messages = {
        'invalid_argument': 'Invalid repair argument.',
        'access_denied': 'Repair policy does not permit this request.',
        'unsupported_repair': 'Repair is not supported by this planner.',
        'stale_plan': 'Repair conditions changed; request a new plan.',
        'revision_conflict': 'Settings revision changed.',
    }
    return ServiceError(code, messages[code], status_code=status)


def valid_integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= 9223372036854775807


def valid_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class AgentPrincipal:
    access: AccessContext
    policy_revision: int

    def __post_init__(self):
        access = self.access
        if (type(access) is not AccessContext or not valid_integer(self.policy_revision)
                or type(access.subject_id) is not str or not 1 <= len(access.subject_id) <= 128
                or not access.subject_id.strip() or '\0' in access.subject_id
                or type(access.capabilities) is not frozenset
                or any(type(item) is not str for item in access.capabilities)
                or type(access.source_ids) is not tuple or len(access.source_ids) > 64
                or any(not valid_uuid(item) for item in access.source_ids)
                or len(set(access.source_ids)) != len(access.source_ids)):
            raise repair_error('invalid_argument')


@dataclass(frozen=True)
class NumericSettingLimit:
    path: str
    minimum: int
    maximum: int


@dataclass(frozen=True)
class AgentReason:
    code: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class RepairAction:
    id: str
    kind: str
    timeout_seconds: int
    max_attempts: int
    reversible: bool


@dataclass(frozen=True)
class RepairPlan:
    id: str
    principal_id: str
    capability_revision: int
    config_revision: int
    index_generation: int
    health_fact_hash: str
    expires_at: datetime
    code: str
    source_id: str | None
    actions: tuple[RepairAction, ...]
    peak_extra_bytes: int
    rollback_mode: str
    idempotency_key: str | None = None


@dataclass(frozen=True)
class AgentSettingsValidation:
    """Safe patch projection; normalized is never an owner apply candidate."""

    valid: bool
    expected_revision: int
    normalized: dict | None
    diff: tuple[SettingChange, ...]
    warnings: tuple[str, ...]
    errors: tuple[FieldError, ...]
    idempotency_key: str | None = None


class SettingsValidatePort(Protocol):
    """The existing owner MutationCoordinator.validate, never an apply hook."""

    def __call__(self, candidate: dict, expected_revision: int) -> SettingsValidation: ...


def _freeze_incident_value(value, depth=0):
    """Copy runtime dictionaries into immutable, bounded-depth record values.

    Field authority and redaction are checked by IncidentBuilder. No arbitrary
    object's conversion/repr or serialization hooks run at this boundary.
    """
    if depth > 8:
        raise repair_error('invalid_argument')
    if type(value) in (dict, MappingProxyType):
        if any(type(key) is not str for key in value):
            raise repair_error('invalid_argument')
        return MappingProxyType({key: _freeze_incident_value(item, depth + 1)
                                 for key, item in value.items()})
    if type(value) in (tuple, list):
        return tuple(_freeze_incident_value(item, depth + 1) for item in value)
    if type(value) in (type(None), str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise repair_error('invalid_argument')


@dataclass(frozen=True)
class AttemptIntent:
    """Trusted owner observation; never accepted as agent-supplied JSON."""

    operation_id: str
    incident_id: str
    principal_id: str
    kind: str
    code: str
    plan_id: str | None
    source_id: str | None
    started_at: datetime
    config_revision: int
    index_generation: int
    health_before: HealthSnapshot
    reason: AgentReason


@dataclass(frozen=True)
class AttemptRecord:
    intent: AttemptIntent
    finished_at: datetime
    actions: tuple[dict, ...]
    health_after: HealthSnapshot
    verification: dict
    rollback: dict
    result: str
    config_diff: tuple[dict, ...]

    def __post_init__(self):
        for field in ('actions', 'verification', 'rollback', 'config_diff'):
            object.__setattr__(self, field, _freeze_incident_value(getattr(self, field)))


@dataclass(frozen=True)
class IncidentPayload:
    """Final canonical report; delivery metadata belongs to a later boundary."""

    id: str
    canonical_bytes: bytes
    sha256: str

    def __post_init__(self):
        if (not valid_uuid(self.id) or type(self.canonical_bytes) is not bytes
                or not 0 < len(self.canonical_bytes) <= 262144
                or type(self.sha256) is not str
                or self.sha256 != hashlib.sha256(self.canonical_bytes).hexdigest()):
            raise repair_error('invalid_argument')
