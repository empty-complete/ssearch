"""Immutable server-owned planning values over the existing P2 authority.

These types do not authenticate a caller. Only the owner may construct an
AgentPrincipal from its authenticated AccessContext and current policy revision.
No transport accepts these values as a grant or an executable request.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from atom_search.service.contracts import (
    AccessContext, FieldError, ServiceError, SettingChange, SettingsValidation,
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
