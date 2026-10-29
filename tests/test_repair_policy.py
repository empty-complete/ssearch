"""Owner grants and cached facts bound pure plans; nothing here applies repair."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from uuid import UUID

import pytest

from atom_search.service.contracts import (
    AccessContext, ConfigVersion, DatabaseHealth, HealthComponents, HealthSnapshot,
    ModelHealth, OutboxHealth, ServiceError, SettingsSnapshot, SourceHealth,
)
from atom_search.service.health import storage_health
from atom_search.service.mutations import MutationCoordinator
from atom_search.settings.migration import default_settings
from atom_search.repair.contracts import AgentPrincipal, AgentReason, NumericSettingLimit
from atom_search.repair.policy import RepairPolicy
from atom_search.repair.planner import RepairPlanner


A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
NOW = datetime(2026, 10, 29, 7, tzinfo=timezone.utc)


def principal(*, subject='agent-a', revision=3, sources=(A,), capabilities=None):
    return AgentPrincipal(AccessContext(subject, frozenset(capabilities if capabilities is not None else
        {'search', 'fetch', 'status', 'settings_get', 'agent_repair', 'owner'}), sources), revision)


def policy(**overrides):
    values = dict(revision=3, enabled=True, allowed_actions=frozenset({'retry_source_probe'}),
        setting_limits=(NumericSettingLimit('indexing.interval_seconds', 86400, 604800),))
    values.update(overrides)
    return RepairPolicy(**values)


def health():
    source = SourceHealth(A, 'unreachable', NOW.isoformat(), None, False,
        'source_unavailable', None, None, None, True)
    return HealthSnapshot('alive', 'degraded', '0.2.0', '0.2.0', '1', NOW.isoformat(),
        ConfigVersion(3, 7), 19, HealthComponents((source,),
            ModelHealth('ready', 'local-model', NOW.isoformat(), False, True),
            DatabaseHealth('postgresql', '18.6', '0.8.7', 'ready', NOW.isoformat(), False, None),
            storage_health({}, checked_at=NOW), (), OutboxHealth(0, 0, None, False)),
        ('search', 'fetch', 'status', 'settings_get'))


def planned(*, selected_policy=None):
    planner = RepairPlanner(selected_policy or policy(), clock=lambda: NOW)
    plan = planner.plan('SOURCE_UNREACHABLE', source_id=A, expected_revision=7,
                        principal=principal(), health=health())
    return planner, plan


def assert_error(code, callback, status=None):
    with pytest.raises(ServiceError) as caught:
        callback()
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status_code == status
    assert not caught.value.retryable


def test_default_policy_denies_mutations_but_keeps_search():
    disabled = RepairPolicy()
    assert disabled.enabled is False and disabled.allowed_actions == frozenset()
    caller = principal(revision=0)
    for action in ('retry_source_probe', 'settings_validate'):
        assert_error('access_denied', lambda: disabled.authorize(
            caller, action=action, source_id=A), 403)
    # The grant layer does not alter the existing read capability projection.
    before = health()
    assert before.capabilities == ('search', 'fetch', 'status', 'settings_get')
    assert caller.access.capabilities >= {'search', 'status'}


@pytest.mark.parametrize('caller, action, source', [
    (principal(capabilities={'search'}), 'retry_source_probe', A),
    (principal(capabilities={'owner'}), 'retry_source_probe', A),
    (principal(sources=()), 'retry_source_probe', A),
    (principal(sources=(B,)), 'retry_source_probe', A),
    (principal(revision=2), 'retry_source_probe', A),
    (principal(), 'pause_source_job', A),
    (principal(), 'shell', None),
])
def test_owner_policy_intersects_trusted_capabilities_revision_and_source(caller, action, source):
    assert_error('access_denied', lambda: policy().authorize(caller, action=action, source_id=source), 403)


def test_grants_and_principal_are_immutable_and_reject_mutable_authority():
    grant = policy()
    caller = principal()
    with pytest.raises(FrozenInstanceError):
        grant.enabled = False
    with pytest.raises(FrozenInstanceError):
        caller.policy_revision = 9
    assert_error('invalid_argument', lambda: AgentPrincipal(
        AccessContext('agent', {'agent_repair'}, [A]), 3))


def current_settings():
    candidate = default_settings()
    candidate['revision'] = 7
    candidate['sources'] = [dict(id=A, path='C:/private', label='', enabled=True)]
    candidate['updates']['source']['credential_ref'] = 'private-credential-reference'
    return SettingsSnapshot(3, 7, candidate, ())


def validator_for(current):
    # Exercise the existing schema/diff coordinator without a writer or disk.
    def check_revision(settings, expected):
        if settings['revision'] != expected:
            raise ServiceError('revision_conflict', 'Revision conflict', status_code=409)
    repository = SimpleNamespace(snapshot=lambda: deepcopy(current),
        _check_revision=check_revision, _mutation_lock=None)
    return MutationCoordinator(repository).validate


def validate(patch, *, selected_policy=None, caller=None, current=None, expected=7):
    current = current or current_settings()
    return (selected_policy or policy()).settings_validate(patch, expected_revision=expected,
        principal=caller or principal(), current=current, validator=validator_for(current))


@pytest.mark.parametrize('patch', [
    {'sources': []}, {'database': {'password': 'sentinel-secret'}},
    {'diagnostics': {'remote_directory': '//new/destination'}},
    {'diagnostics': {'remote_credential_ref': 'new-credential'}},
    {'agent': {'repair_policy': {'enabled': True}}},
    {'storage': {'quota_bytes': 99999999999}},
    {'indexing': {'deep_profile': 'deep'}}, {'updates': {'enabled': False}},
])
def test_agent_cannot_edit_sources_credentials_remote_or_own_policy(patch):
    assert_error('invalid_argument', lambda: validate(patch))


@pytest.mark.parametrize('patch', [None, [], True, {}, {'example': True},
    {'indexing': True}, {'indexing': {}}, {'indexing': {'interval_seconds': True}},
    {'indexing': {'interval_seconds': '86400'}},
    {'indexing': {'interval_seconds': 604801}},
    {'indexing': {'interval_seconds': 86399}},
    {'indexing': {'interval_seconds': 86400, 'unknown': 1}},
])
def test_agent_settings_reject_invalid_types_unknown_fields_examples_and_owner_range(patch):
    assert_error('invalid_argument', lambda: validate(patch))


def test_validate_has_no_disk_network_or_settings_side_effects(tmp_path, monkeypatch):
    current = current_settings()
    path = tmp_path / 'settings.json'
    before = json.dumps(current.settings, sort_keys=True).encode()
    path.write_bytes(before)
    validate_port = validator_for(current)
    def forbidden(*args, **kwargs):
        pytest.fail('pure validation attempted disk/network I/O')
    with monkeypatch.context() as blocked:
        blocked.setattr(Path, 'open', forbidden)
        blocked.setattr('builtins.open', forbidden)
        blocked.setattr(socket, 'create_connection', forbidden)
        result = policy().settings_validate({'indexing': {'interval_seconds': 172800}},
            expected_revision=7, principal=principal(), current=current, validator=validate_port)
    assert path.read_bytes() == before
    assert current.revision == current.settings['revision'] == 7
    assert current.settings['indexing']['interval_seconds'] == 86400
    assert result.valid and result.expected_revision == 7
    assert [(change.path, change.before, change.after) for change in result.diff] == [
        ('indexing.interval_seconds', 86400, 172800)]
    assert result.normalized == {'indexing': {'interval_seconds': 172800}}
    assert result.idempotency_key is None
    assert 'private-credential-reference' not in repr(result)
    assert 'C:/private' not in repr(result)


def test_settings_validate_requires_current_grant_and_revision_before_port():
    current = current_settings()
    def forbidden(*args):
        pytest.fail('rejected request reached validation port')
    for caller in (principal(capabilities={'agent_repair'}), principal(revision=2)):
        assert_error('access_denied', lambda: policy().settings_validate(
            {'indexing': {'interval_seconds': 172800}}, expected_revision=7,
            principal=caller, current=current, validator=forbidden), 403)
    assert_error('revision_conflict', lambda: policy().settings_validate(
        {'indexing': {'interval_seconds': 172800}}, expected_revision=6,
        principal=principal(), current=current, validator=forbidden), 409)


@pytest.mark.parametrize('sources', [(), (B,)])
def test_global_interval_validation_requires_explicit_coverage_of_enabled_sources(sources):
    assert_error('access_denied', lambda: validate({'indexing': {'interval_seconds': 172800}},
        caller=principal(sources=sources)), 403)


@pytest.mark.parametrize('limit', [NumericSettingLimit('storage.quota_bytes', 1, 99999999999),
    NumericSettingLimit('indexing.interval_seconds', True, 604800),
    NumericSettingLimit('indexing.interval_seconds', 1, 604800),
    NumericSettingLimit('indexing.interval_seconds', 86400, 31536001)])
def test_owner_numeric_limits_cannot_widen_active_schema_or_fields(limit):
    assert_error('invalid_argument', lambda: policy(setting_limits=(limit,)))


def test_plan_binds_uuid_source_principal_policy_revision_generation_fact_and_ttl():
    planner, plan = planned()
    assert str(UUID(plan.id)) == plan.id
    assert (plan.principal_id, plan.capability_revision, plan.config_revision,
            plan.index_generation, plan.source_id) == ('agent-a', 3, 7, 19, A)
    assert plan.expires_at - NOW == timedelta(seconds=600)
    assert len(plan.health_fact_hash) == 64 and int(plan.health_fact_hash, 16) >= 0
    assert plan.code == 'SOURCE_UNREACHABLE' and plan.idempotency_key is None
    assert [action.kind for action in plan.actions] == ['retry_source_probe']
    assert all(0 < action.timeout_seconds <= 5 and action.max_attempts == 1 for action in plan.actions)
    assert plan.peak_extra_bytes >= 0 and plan.rollback_mode == 'not_required'
    with pytest.raises(FrozenInstanceError):
        plan.source_id = B
    with pytest.raises(FrozenInstanceError):
        plan.actions[0].max_attempts = 100
    planner.validate_plan(plan, expected_revision=7, principal=principal(), health=health(),
        reason=AgentReason(plan.code, ('health:' + plan.health_fact_hash,)))


@pytest.mark.parametrize('changed', ['principal', 'policy', 'config', 'generation', 'fact', 'expired'])
def test_stale_plan_is_rejected_without_action(changed):
    planner, plan = planned()
    caller, snapshot = principal(), health()
    if changed == 'principal':
        caller = principal(subject='agent-b')
    elif changed == 'policy':
        caller = principal(revision=4)
    elif changed == 'config':
        snapshot = replace(snapshot, config_version=ConfigVersion(3, 8))
    elif changed == 'generation':
        snapshot = replace(snapshot, index_generation=20)
    elif changed == 'fact':
        source = replace(snapshot.components.sources[0], state='available', error_code=None)
        snapshot = replace(snapshot, components=replace(snapshot.components, sources=(source,)))
    else:
        planner = RepairPlanner(policy(), clock=lambda: NOW + timedelta(seconds=600))
    assert_error('stale_plan', lambda: planner.validate_plan(plan, expected_revision=7,
        principal=caller, health=snapshot,
        reason=AgentReason(plan.code, ('health:' + plan.health_fact_hash,))), 409)


@pytest.mark.parametrize('reason', [AgentReason('SOURCE_ACCESS_DENIED', ('health:' + '0' * 64,)),
    AgentReason('SOURCE_UNREACHABLE', ('health:' + '0' * 64,)),
    AgentReason('SOURCE_UNREACHABLE', ('C:/private/log.txt',)),
    AgentReason('SOURCE_UNREACHABLE', ()),
])
def test_foreign_health_ref_or_free_text_reason_is_rejected_without_action(reason):
    planner, plan = planned()
    assert_error('invalid_argument', lambda: planner.validate_plan(plan, expected_revision=7,
        principal=principal(), health=health(), reason=reason))


@pytest.mark.parametrize('changes', [
    {'actions': ()},
    {'peak_extra_bytes': 1}, {'peak_extra_bytes': False},
    {'rollback_mode': 'automatic'}, {'code': 'SOURCE_ACCESS_DENIED'},
    {'source_id': B},
    {'expires_at': NOW + timedelta(seconds=601)},
    {'idempotency_key': 'agent-chosen-key'},
])
def test_preflight_rejects_tampered_plan_descriptors(changes):
    planner, plan = planned()
    assert_error('stale_plan', lambda: planner.validate_plan(replace(plan, **changes),
        expected_revision=7, principal=principal(), health=health(),
        reason=AgentReason(plan.code, ('health:' + plan.health_fact_hash,))), 409)


@pytest.mark.parametrize('changes', [
    {'kind': 'shell'}, {'timeout_seconds': 6}, {'max_attempts': 2},
    {'max_attempts': True}, {'reversible': True}, {'reversible': 0},
])
def test_preflight_rejects_tampered_action_limits(changes):
    planner, plan = planned()
    forged = replace(plan, actions=(replace(plan.actions[0], **changes),))
    assert_error('stale_plan', lambda: planner.validate_plan(forged, expected_revision=7,
        principal=principal(), health=health(),
        reason=AgentReason(plan.code, ('health:' + plan.health_fact_hash,))), 409)


def test_access_denied_plan_uses_only_scoped_pause_descriptor():
    selected_policy = policy(allowed_actions=frozenset({'pause_source_job'}))
    planner = RepairPlanner(selected_policy, clock=lambda: NOW)
    snapshot = health()
    source = replace(snapshot.components.sources[0], state='access_denied', error_code='source_access_denied')
    snapshot = replace(snapshot, components=replace(snapshot.components, sources=(source,)))
    plan = planner.plan('SOURCE_ACCESS_DENIED', source_id=A, expected_revision=7,
        principal=principal(), health=snapshot)
    assert [action.kind for action in plan.actions] == ['pause_source_job']
    assert plan.source_id == A and plan.idempotency_key is None


@pytest.mark.parametrize('code', ['DATABASE_UNAVAILABLE', 'DATABASE_PORT_CONFLICT',
    'DB_BUSY', 'DB_CORRUPT', 'MODEL_MISSING', 'CONFIG_INVALID', 'STORAGE_LIMIT', 'JOB_STALLED'])
def test_global_runtime_repairs_stay_unsupported_without_owner_integration(code):
    planner = RepairPlanner(policy(), clock=lambda: NOW)
    assert_error('unsupported_repair', lambda: planner.plan(code, source_id=None,
        expected_revision=7, principal=principal(), health=health()))


@pytest.mark.parametrize('source', [1, True, 'C:/private', B, None])
def test_planning_rejects_non_uuid_foreign_or_missing_source(source):
    planner = RepairPlanner(policy(), clock=lambda: NOW)
    assert_error('invalid_argument' if source in (1, True, 'C:/private', None) else 'access_denied',
        lambda: planner.plan('SOURCE_UNREACHABLE', source_id=source,
            expected_revision=7, principal=principal(), health=health()))


def test_unknown_code_and_unobserved_or_stale_fault_are_not_repaired():
    planner = RepairPlanner(policy(), clock=lambda: NOW)
    assert_error('unsupported_repair', lambda: planner.plan('RUN_SQL', source_id=A,
        expected_revision=7, principal=principal(), health=health()))
    for source in (replace(health().components.sources[0], state='available', error_code=None),
                   replace(health().components.sources[0], stale=True),
                   replace(health().components.sources[0], checked_at=None)):
        snapshot = replace(health(), components=replace(health().components, sources=(source,)))
        assert_error('stale_plan', lambda: planner.plan('SOURCE_UNREACHABLE', source_id=A,
            expected_revision=7, principal=principal(), health=snapshot), 409)


def test_cached_fact_freshness_is_checked_at_planning_time_not_only_stale_flag():
    snapshot = health()
    RepairPlanner(policy(), clock=lambda: NOW + timedelta(seconds=300)).plan(
        'SOURCE_UNREACHABLE', source_id=A, expected_revision=7,
        principal=principal(), health=snapshot)
    assert_error('stale_plan', lambda: RepairPlanner(policy(),
        clock=lambda: NOW + timedelta(seconds=301)).plan('SOURCE_UNREACHABLE',
            source_id=A, expected_revision=7, principal=principal(), health=snapshot), 409)


@pytest.mark.parametrize('revision', [True, '7', -1, None])
def test_invalid_revision_does_not_coerce_to_owner_revision(revision):
    planner = RepairPlanner(policy(), clock=lambda: NOW)
    assert_error('invalid_argument', lambda: planner.plan('SOURCE_UNREACHABLE',
        source_id=A, expected_revision=revision, principal=principal(), health=health()))


def test_planning_and_preflight_have_no_io_and_ignore_response_timestamp(monkeypatch):
    snapshot = health()
    before = asdict(snapshot)
    def forbidden(*args, **kwargs):
        pytest.fail('planning attempted I/O or action execution')
    monkeypatch.setattr('builtins.open', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    planner, plan = planned()
    planner.validate_plan(plan, expected_revision=7, principal=principal(),
        health=replace(snapshot, observed_at=(NOW + timedelta(seconds=1)).isoformat()),
        reason=AgentReason(plan.code, ('health:' + plan.health_fact_hash,)))
    assert asdict(snapshot) == before
    assert 'agent_repair' not in snapshot.capabilities
