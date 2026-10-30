"""Pure cached source-fault plans and preflight checks, with no apply path.

Plans are server-owned values. A later coordinator must retain issued plans and
durable keys; accepting a client-constructed RepairPlan is never an apply API.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Callable
from uuid import uuid4

from atom_search.service.contracts import HealthSnapshot, ServiceError

from .catalog import source_repair
from .contracts import (AgentPrincipal, AgentReason, RepairPlan, repair_error,
    RepairAction, valid_integer, valid_uuid)
from .policy import RepairPolicy


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise repair_error('invalid_argument')
    return value.astimezone(timezone.utc)


class RepairPlanner:
    def __init__(self, policy: RepairPolicy, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if type(policy) is not RepairPolicy:
            raise repair_error('invalid_argument')
        self.policy = policy
        self._clock = clock

    def _fact(self, code, *, source_id, expected_revision, principal, health, now):
        entry = source_repair(code)
        if not valid_integer(expected_revision, 1) or not valid_uuid(source_id):
            raise repair_error('invalid_argument')
        self.policy.authorize(principal, action=entry.action.kind, source_id=source_id)
        if type(health) is not HealthSnapshot:
            raise repair_error('invalid_argument')
        if (health.config_version.revision != expected_revision
                or not valid_integer(health.index_generation)):
            raise repair_error('stale_plan', 409)
        sources = tuple(item for item in health.components.sources if item.source_id == source_id)
        if len(sources) != 1:
            raise repair_error('stale_plan', 409)
        source = sources[0]
        if (source.state != entry.state or source.error_code != entry.error_code
                or source.stale is not False or type(source.checked_at) is not str):
            raise repair_error('stale_plan', 409)
        try:
            checked = _utc(datetime.fromisoformat(source.checked_at))
        except (ValueError, TypeError, ServiceError):
            raise repair_error('stale_plan', 409) from None
        if not 0 <= (now - checked).total_seconds() <= 300:
            raise repair_error('stale_plan', 409)
        # observed_at describes response assembly, not the cached fault.
        material = dict(code=code, source=asdict(source),
            config_version=asdict(health.config_version), index_generation=health.index_generation,
            app_version=health.app_version, service_version=health.service_version,
            api_version=health.api_version)
        digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
        return entry, digest

    def plan(self, code: str, *, source_id: str | None, expected_revision: int,
             principal: AgentPrincipal, health: HealthSnapshot) -> RepairPlan:
        now = _utc(self._clock())
        entry, digest = self._fact(code, source_id=source_id, expected_revision=expected_revision,
            principal=principal, health=health, now=now)
        return RepairPlan(str(uuid4()), principal.access.subject_id, self.policy.revision,
            expected_revision, health.index_generation, digest, now + timedelta(seconds=600),
            code, source_id, (entry.action,), 0, 'not_required')

    def validate_plan(self, plan: RepairPlan, *, expected_revision: int,
                      principal: AgentPrincipal, health: HealthSnapshot, reason: AgentReason) -> None:
        """Pure preflight, not authorization to execute or a durable reservation."""
        now = _utc(self._clock())
        if type(plan) is not RepairPlan or type(principal) is not AgentPrincipal:
            raise repair_error('invalid_argument')
        try:
            expires = _utc(plan.expires_at)
        except ServiceError:
            raise repair_error('stale_plan', 409) from None
        if (plan.principal_id != principal.access.subject_id
                or not valid_integer(plan.capability_revision)
                or not valid_integer(plan.config_revision, 1)
                or not valid_integer(plan.index_generation)
                or principal.policy_revision != plan.capability_revision
                or self.policy.revision != plan.capability_revision
                or not self.policy.enabled or not valid_integer(expected_revision, 1)
                or expected_revision != plan.config_revision
                or type(health) is not HealthSnapshot
                or health.config_version.revision != plan.config_revision
                or health.index_generation != plan.index_generation
                or now >= expires or expires > now + timedelta(seconds=600)):
            raise repair_error('stale_plan', 409)
        try:
            entry, digest = self._fact(plan.code, source_id=plan.source_id,
                expected_revision=expected_revision, principal=principal, health=health, now=now)
        except ServiceError:
            raise repair_error('stale_plan', 409) from None
        if (digest != plan.health_fact_hash or not valid_uuid(plan.id)
                or type(plan.actions) is not tuple or len(plan.actions) != 1
                or type(plan.actions[0]) is not RepairAction
                or type(plan.actions[0].timeout_seconds) is not int
                or type(plan.actions[0].max_attempts) is not int
                or type(plan.actions[0].reversible) is not bool
                or plan.actions != (entry.action,)
                or plan.peak_extra_bytes != 0 or type(plan.peak_extra_bytes) is not int
                or plan.rollback_mode != 'not_required' or plan.idempotency_key is not None):
            raise repair_error('stale_plan', 409)
        if (type(reason) is not AgentReason or reason.code != plan.code
                or type(reason.evidence_refs) is not tuple
                or reason.evidence_refs != ('health:' + digest,)):
            raise repair_error('invalid_argument')
