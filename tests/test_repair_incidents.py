"""Pure incidents redact by closed fields and preserve audited outcomes in bounds."""
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket

import pytest

from atom_search.service.contracts import (
    ConfigVersion, DatabaseHealth, HealthComponents, HealthSnapshot, JobSnapshot,
    ModelHealth, OutboxHealth, ServiceError, SourceHealth,
)
from atom_search.service.health import storage_health
from atom_search.repair.contracts import (
    AgentReason, AttemptIntent, AttemptRecord, IncidentPayload,
)
from atom_search.repair.incidents import IncidentBuilder


A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
OPERATION = '33333333-3333-4333-8333-333333333333'
INCIDENT = '44444444-4444-4444-8444-444444444444'
PLAN = '55555555-5555-4555-8555-555555555555'
NOW = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
KEY = b'host-injected-local-alias-key-32-bytes-minimum'
OTHER_KEY = b'another-host-local-alias-key-32-bytes-minimum'
PRIVATE_PATH = r'C:\SecretClient\SentinelDocument.docx'
NEW_PATH = r'\\private-server\PrivateShare\SentinelDocument.docx'
TOKEN = 'TOKEN-DO-NOT-SERIALIZE-f2c309be'
DOCUMENT = 'DOCUMENT-CONTENT-DO-NOT-SERIALIZE-9e734c'
RAW_LOG = 'RAW-LOG-DO-NOT-SERIALIZE-0a554c'
AGENT_TEXT = 'AGENT-TEXT-DO-NOT-SERIALIZE-986abd'


def health():
    source = SourceHealth(A, 'unreachable', NOW.isoformat(), None, False,
        'source_unavailable', 10, 0, NOW.isoformat(), False)
    return HealthSnapshot('alive', 'degraded', '0.2.0', '0.2.0', '1', NOW.isoformat(),
        ConfigVersion(3, 7), 19, HealthComponents((source,),
            ModelHealth('missing', 'bundled-model', NOW.isoformat(), False, True),
            DatabaseHealth('postgresql', '18.6', '0.8.7', 'ready', NOW.isoformat(), False, None),
            storage_health({}, checked_at=NOW), (), OutboxHealth(0, 0, None, False)),
        ('search', 'fetch', 'status', 'settings_get'))


def action():
    return dict(id='retry_source_probe', type='retry_source_probe', stage='completed',
        timeout_seconds=5, error_code='SOURCE_UNREACHABLE')


def attempt(**changes):
    before = health()
    intent = AttemptIntent(OPERATION, INCIDENT, 'trusted-owner-principal', 'repair',
        'SOURCE_UNREACHABLE', PLAN, A, NOW, 7, 19, before,
        AgentReason('SOURCE_UNREACHABLE', ('health:' + 'a' * 64,)))
    values = dict(intent=intent, finished_at=NOW + timedelta(seconds=30),
        actions=(action(),), health_after=replace(before, observed_at=(NOW + timedelta(seconds=30)).isoformat()),
        verification=dict(method='bounded_directory_probe', checked_at=(NOW + timedelta(seconds=30)).isoformat(),
            passed=False, error_code='SOURCE_UNREACHABLE'),
        rollback=dict(required=False, performed=False, reason='no_changes'),
        result='not_fixed', config_diff=())
    values.update(changes)
    return AttemptRecord(**values)


def builder(**options):
    return IncidentBuilder(alias_key=KEY, **options)


def document(payload):
    return json.loads(payload.canonical_bytes)


def assert_invalid(callback):
    with pytest.raises(ServiceError) as caught:
        callback()
    assert caught.value.code == 'invalid_argument'
    assert caught.value.retryable is False
    assert TOKEN not in str(caught.value) and PRIVATE_PATH not in str(caught.value)


def test_incident_is_canonical_immutable_and_reuses_server_identity_without_delivery_state():
    record = attempt()
    first = builder().build(record)
    second = builder().build(record)
    assert type(first) is IncidentPayload
    assert first.id == INCIDENT and first == second
    assert type(first.canonical_bytes) is bytes
    assert first.sha256 == hashlib.sha256(first.canonical_bytes).hexdigest()
    data = document(first)
    expected = json.dumps(data, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode('utf-8')
    assert first.canonical_bytes == expected
    assert (data['id'], data['operation_id'], data['plan_id']) == (INCIDENT, OPERATION, PLAN)
    assert data['schema_version'] == 1 and data['code'] == 'SOURCE_UNREACHABLE'
    assert data['app_version'] == '0.2.0' and data['api_version'] == '1'
    assert data['config_version'] == {'schema_version': 3, 'revision': 7}
    assert data['source_id'] == A and data['result'] == 'not_fixed'
    assert data['verification']['passed'] is False
    assert data['details_truncated'] is False
    assert not {'delivery', 'destination', 'sent_at', 'principal_id'} & data.keys()
    with pytest.raises(FrozenInstanceError):
        first.sha256 = '0' * 64
    with pytest.raises(FrozenInstanceError):
        record.result = 'fixed'


def test_incident_redacts_paths_secrets_documents_raw_logs_and_agent_text():
    snapshot = health()
    source = replace(snapshot.components.sources[0], error_code=PRIVATE_PATH)
    job = JobSnapshot(OPERATION, A, 'failed', 'catalog', NOW.isoformat(), NOW.isoformat(),
        1, 1, 0, 0, 1, None, RAW_LOG)
    components = replace(snapshot.components, sources=(source,), jobs=(job,),
        model=replace(snapshot.components.model, model_id=DOCUMENT),
        database=replace(snapshot.components.database, error_code=TOKEN),
        outbox=replace(snapshot.components.outbox, last_error=AGENT_TEXT))
    snapshot = replace(snapshot, components=components, capabilities=(TOKEN,))
    record = attempt(health_after=snapshot, actions=(dict(action(), error_code=RAW_LOG),),
        config_diff=(dict(path='ui.theme', before=TOKEN, after=DOCUMENT),
                     dict(path='sources.path', source_id=A, before=PRIVATE_PATH, after=NEW_PATH)))
    record = replace(record, intent=replace(record.intent, principal_id=AGENT_TEXT, health_before=snapshot))
    payload = builder().build(record)
    for sentinel in (TOKEN, DOCUMENT, PRIVATE_PATH, NEW_PATH, RAW_LOG, AGENT_TEXT):
        assert sentinel.encode('utf-8') not in payload.canonical_bytes
    assert KEY not in payload.canonical_bytes and KEY.hex().encode() not in payload.canonical_bytes
    data = document(payload)
    changes = data['config_diff']['changes']
    assert changes[0]['before'] == changes[0]['after'] == '[redacted]'
    assert changes[1]['source_id'] == A
    assert changes[1]['before'] == changes[1]['after'] == '[redacted]'
    assert changes[1]['before_alias'].startswith('path:')
    assert changes[1]['after_alias'].startswith('path:')
    assert data['agent_reason'] == {'code': 'SOURCE_UNREACHABLE', 'evidence_refs': ['health:' + 'a' * 64]}
    assert data['symptoms'] and data['agent_explanation']
    assert data['details_truncated'] is False  # Redaction does not mean size truncation.


def test_path_alias_is_local_keyed_source_bound_and_never_a_path_or_key():
    changes = (dict(path='sources.path', source_id=A, before=PRIVATE_PATH, after=PRIVATE_PATH),)
    record = attempt(config_diff=changes)
    first = document(builder().build(record))['config_diff']['changes'][0]
    same = document(builder().build(record))['config_diff']['changes'][0]
    other = document(IncidentBuilder(alias_key=OTHER_KEY).build(record))['config_diff']['changes'][0]
    assert first['before_alias'] == first['after_alias'] == same['before_alias']
    assert first['before_alias'] != other['before_alias']
    assert len(first['before_alias']) == 69
    assert int(first['before_alias'][5:], 16) >= 0
    assert first['before_alias'] != 'path:' + hashlib.sha256(PRIVATE_PATH.encode()).hexdigest()
    assert PRIVATE_PATH not in repr(first) and KEY.decode() not in repr(first)
    other_snapshot = health()
    other_snapshot = replace(other_snapshot, components=replace(other_snapshot.components,
        sources=(replace(other_snapshot.components.sources[0], source_id=B),)))
    other_record = replace(record, intent=replace(record.intent, source_id=B, health_before=other_snapshot),
        health_after=other_snapshot,
        config_diff=(dict(path='sources.path', source_id=B, before=PRIVATE_PATH, after=PRIVATE_PATH),))
    other_source = document(builder().build(other_record))['config_diff']['changes'][0]
    assert first['before_alias'] != other_source['before_alias']


@pytest.mark.parametrize('key', [None, '', 'plaintext-key', b'', b'short', bytearray(KEY)])
def test_alias_key_must_be_explicit_immutable_host_material(key):
    assert_invalid(lambda: IncidentBuilder(alias_key=key))


def test_missing_alias_key_cannot_silently_generate_a_new_identity():
    with pytest.raises(TypeError):
        IncidentBuilder()


@pytest.mark.parametrize('entry', [
    dict(path='sources.path', source_id=B, before=PRIVATE_PATH, after=NEW_PATH),
    dict(path='sources.path', before=PRIVATE_PATH, after=NEW_PATH),
    dict(path='sources.path', source_id=A, before='relative/document', after=NEW_PATH),
    dict(path='database.password', before=TOKEN, after=TOKEN),
    dict(path='ui.theme', before='dark', after='light', password=TOKEN),
])
def test_path_alias_and_config_diff_reject_foreign_scope_unknown_fields_and_paths(entry):
    assert_invalid(lambda: builder().build(attempt(config_diff=(entry,))))


def test_safe_diff_values_follow_field_types_and_enum_allowlists():
    changes = (dict(path='indexing.interval_seconds', before=86400, after=172800),
        dict(path='ui.theme', before='dark', after='system'),
        dict(path='indexing.metadata_enabled', before=True, after=True),
        dict(path='ui.theme', before={'secret': TOKEN}, after=[DOCUMENT]))
    data = document(builder().build(attempt(config_diff=changes)))
    values = data['config_diff']['changes']
    assert [(item['before'], item['after']) for item in values] == [
        (86400, 172800), ('dark', 'system'), (True, True), ('[redacted]', '[redacted]')]


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), True, '172800'])
def test_numeric_diff_never_serializes_nonfinite_or_coerced_values(value):
    assert_invalid(lambda: builder().build(attempt(config_diff=(
        dict(path='indexing.interval_seconds', before=86400, after=value),))))


@pytest.mark.parametrize('field, entry', [
    ('actions', dict(action(), command='powershell ' + TOKEN)),
    ('actions', dict(action(), log=RAW_LOG)),
    ('actions', dict(action(), timeout_seconds=True)),
    ('actions', dict(action(), stage=TOKEN)),
    ('actions', dict(action(), type='shell')),
    ('verification', dict(method='bounded_directory_probe', passed=False, checked_at=NOW.isoformat(),
        error_code=None, document=DOCUMENT)),
    ('verification', dict(method='custom-agent-code', passed=False, checked_at=NOW.isoformat(), error_code=None)),
    ('verification', dict(method='bounded_directory_probe', passed='false', checked_at=NOW.isoformat(), error_code=None)),
    ('rollback', dict(required=False, performed=False, reason=AGENT_TEXT)),
    ('rollback', dict(required=False, performed=False, reason='no_changes', text=AGENT_TEXT)),
])
def test_unknown_structure_and_unbounded_actions_are_rejected(field, entry):
    values = {field: (entry,) if field == 'actions' else entry}
    assert_invalid(lambda: builder().build(attempt(**values)))


@pytest.mark.parametrize('changes', [
    {'app_version': TOKEN}, {'service_version': PRIVATE_PATH}, {'api_version': AGENT_TEXT},
    {'observed_at': RAW_LOG}, {'config_version': ConfigVersion(True, 7)},
])
def test_health_identity_and_timestamp_channels_cannot_publish_free_text(changes):
    assert_invalid(lambda: builder().build(attempt(health_after=replace(health(), **changes))))


@pytest.mark.parametrize('change', [
    {'incident_id': TOKEN}, {'operation_id': PRIVATE_PATH}, {'source_id': B},
    {'code': AGENT_TEXT}, {'kind': 'shell'}, {'config_revision': True},
    {'reason': AgentReason(AGENT_TEXT, ('health:' + 'a' * 64,))},
    {'reason': AgentReason('SOURCE_UNREACHABLE', (PRIVATE_PATH,))},
])
def test_invalid_intent_cannot_inject_identity_reason_or_scope(change):
    record = attempt()
    assert_invalid(lambda: builder().build(replace(record, intent=replace(record.intent, **change))))


@pytest.mark.parametrize('verification', [
    dict(method='bounded_directory_probe', checked_at=NOW.isoformat(), passed=False, error_code='SOURCE_UNREACHABLE'),
    dict(method='not_performed', checked_at=NOW.isoformat(), passed=True, error_code=None),
    dict(method='bounded_directory_probe', checked_at=NOW.isoformat(), passed=True, error_code='SOURCE_UNREACHABLE'),
])
def test_fixed_requires_specific_successful_postcondition(verification):
    assert_invalid(lambda: builder().build(attempt(result='fixed', verification=verification)))


def test_verified_fixed_outcome_is_reported_from_postcondition_not_action_exit():
    record = attempt(result='fixed', verification=dict(method='bounded_directory_probe',
        checked_at=(NOW + timedelta(seconds=30)).isoformat(), passed=True, error_code=None))
    data = document(builder().build(record))
    assert data['result'] == 'fixed' and data['verification']['passed'] is True


def fixed_attempt(kind, code, method):
    record = attempt(result='fixed', verification=dict(method=method,
        checked_at=(NOW + timedelta(seconds=30)).isoformat(), passed=True, error_code=None))
    return replace(record, intent=replace(record.intent, kind=kind, code=code,
        reason=AgentReason(code, ('health:' + 'a' * 64,))))


@pytest.mark.parametrize('kind, code, method', [
    ('repair', 'SOURCE_UNREACHABLE', 'settings_applied'),
    ('repair', 'SOURCE_UNREACHABLE', 'storage_budget_verified'),
    ('repair', 'SOURCE_ACCESS_DENIED', 'local_model_verified'),
    ('repair', 'MODEL_MISSING', 'bounded_directory_probe'),
    ('repair', 'MODEL_CORRUPT', 'owned_database_verified'),
    ('repair', 'DATABASE_UNAVAILABLE', 'local_model_verified'),
    ('repair', 'DB_BUSY', 'storage_budget_verified'),
    ('repair', 'DATABASE_PORT_CONFLICT', 'settings_applied'),
    ('repair', 'STORAGE_LIMIT', 'owned_database_verified'),
    ('repair', 'CONFIG_INVALID', 'bounded_directory_probe'),
    ('repair', 'JOB_STALLED', 'owned_database_verified'),
    ('repair', 'DB_CORRUPT', 'owned_database_verified'),
    ('repair', 'DATABASE_VERSION_UNSUPPORTED', 'owned_database_verified'),
    ('repair', 'DATABASE_EXTENSION_MISSING', 'owned_database_verified'),
    ('settings', 'CONFIG_INVALID', 'bounded_directory_probe'),
    ('settings', 'CONFIG_INVALID', 'owned_database_verified'),
    ('settings', 'SOURCE_UNREACHABLE', 'settings_applied'),
    ('settings', 'MODEL_MISSING', 'settings_applied'),
    ('settings', 'DATABASE_UNAVAILABLE', 'settings_applied'),
    ('settings', 'STORAGE_LIMIT', 'settings_applied'),
])
def test_fixed_rejects_postconditions_for_another_kind_or_fault(kind, code, method):
    # A successful check of a different component cannot prove this fault fixed.
    assert_invalid(lambda: builder().build(fixed_attempt(kind, code, method)))


@pytest.mark.parametrize('kind, code, method', [
    ('repair', 'SOURCE_UNREACHABLE', 'bounded_directory_probe'),
    ('repair', 'SOURCE_ACCESS_DENIED', 'source_job_paused'),
    ('repair', 'MODEL_MISSING', 'local_model_verified'),
    ('repair', 'MODEL_CORRUPT', 'local_model_verified'),
    ('repair', 'DB_BUSY', 'owned_database_verified'),
    ('repair', 'DATABASE_UNAVAILABLE', 'owned_database_verified'),
    ('repair', 'DATABASE_PORT_CONFLICT', 'owned_database_verified'),
    ('repair', 'STORAGE_LIMIT', 'storage_budget_verified'),
    ('repair', 'CONFIG_INVALID', 'settings_applied'),
    ('settings', 'CONFIG_INVALID', 'settings_applied'),
])
def test_fixed_keeps_specific_kind_and_fault_postconditions(kind, code, method):
    data = document(builder().build(fixed_attempt(kind, code, method)))
    assert (data['kind'], data['code'], data['verification']['method']) == (kind, code, method)
    assert data['result'] == 'fixed' and data['verification']['passed'] is True


def test_real_server_exited_runtime_state_is_reported_without_raw_error_text():
    from atom_search.runtime.postgres import RuntimeState
    actual = RuntimeState('degraded', None, 'server_exited')
    snapshot = health()
    snapshot = replace(snapshot, readiness='blocked', components=replace(snapshot.components,
        database=DatabaseHealth('postgresql', None, None, actual.state, NOW.isoformat(), False, actual.error_code)))
    record = attempt(health_after=snapshot, verification=dict(method='owned_database_verified',
        checked_at=NOW.isoformat(), passed=False, error_code='DATABASE_UNAVAILABLE'))
    record = replace(record, intent=replace(record.intent, code='DATABASE_UNAVAILABLE',
        reason=AgentReason('DATABASE_UNAVAILABLE', ('health:' + 'a' * 64,))))
    payload = builder().build(record)
    observed = document(payload)['health_after']
    assert observed['readiness'] == 'blocked'
    assert observed['database']['runtime_state'] == 'degraded'
    assert observed['database']['error_code'] == 'server_exited'


@pytest.mark.parametrize('result', ['not_fixed', 'partial', 'rolled_back', 'rollback_failed', 'interrupted'])
def test_unsuccessful_and_interrupted_results_remain_distinct(result):
    data = document(builder().build(attempt(result=result)))
    assert data['result'] == result
    assert data['verification']['passed'] is False


@pytest.mark.parametrize('changes', [
    {'result': 'success'}, {'finished_at': NOW - timedelta(seconds=1)},
    {'finished_at': NOW.replace(tzinfo=None)},
])
def test_report_rejects_unknown_outcome_and_invalid_time_order(changes):
    assert_invalid(lambda: builder().build(attempt(**changes)))


def test_attempt_snapshots_copy_and_freeze_nested_action_and_diff_input():
    original_action = action()
    original_diff = dict(path='ui.theme', before='light', after='dark')
    record = attempt(actions=(original_action,), config_diff=(original_diff,))
    original_action['stage'] = 'failed'
    original_diff['after'] = TOKEN
    data = document(builder().build(record))
    assert data['actions'][0]['stage'] == 'completed'
    assert data['config_diff']['changes'][0]['after'] == 'dark'
    with pytest.raises(TypeError):
        record.actions[0]['stage'] = 'failed'
    with pytest.raises(TypeError):
        record.verification['passed'] = True


def test_exact_utf8_host_budget_boundary_and_one_byte_less_mark_real_detail_loss():
    record = attempt(actions=tuple(action() for _ in range(64)))
    full = builder().build(record)
    full_bytes = len(full.canonical_bytes)
    # Server-owned localized templates exercise UTF-8 byte accounting.
    assert full_bytes > len(full.canonical_bytes.decode('utf-8'))
    exact = builder(max_report_bytes=full_bytes).build(record)
    assert exact.canonical_bytes == full.canonical_bytes
    assert document(exact)['details_truncated'] is False
    smaller = builder(max_report_bytes=full_bytes - 1).build(record)
    data = document(smaller)
    assert len(smaller.canonical_bytes) <= full_bytes - 1
    assert data['details_truncated'] is True
    assert len(data['actions']) < len(document(full)['actions'])
    assert data['result'] == 'not_fixed' and data['verification'] == document(full)['verification']
    assert (data['id'], data['operation_id'], data['code'], data['source_id']) == (
        INCIDENT, OPERATION, 'SOURCE_UNREACHABLE', A)


def test_oversize_report_preserves_required_result_verification_and_identity_under_256kib():
    record = attempt(actions=tuple(action() for _ in range(4096)),
        config_diff=tuple(dict(path='ui.theme', before='dark', after='system') for _ in range(4096)))
    payload = builder().build(record)
    data = document(payload)
    assert len(payload.canonical_bytes) <= 262144
    assert data['details_truncated'] is True
    assert data['result'] == 'not_fixed' and data['verification']['passed'] is False
    assert data['id'] == INCIDENT and data['agent_reason']['code'] == 'SOURCE_UNREACHABLE'
    assert len(data['actions']) < 4096 or len(data['config_diff']['changes']) < 4096


@pytest.mark.parametrize('budget', [True, 0, 4095, 262145, '262144'])
def test_host_budget_cannot_disable_required_envelope_or_increase_hard_ceiling(budget):
    assert_invalid(lambda: builder(max_report_bytes=budget))


def test_builder_uses_no_disk_network_or_settings_side_effects(monkeypatch):
    record = attempt()
    snapshot = asdict(health())
    def forbidden(*args, **kwargs):
        pytest.fail('incident construction attempted I/O')
    monkeypatch.setattr('builtins.open', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    payload = builder().build(record)
    assert payload.id == INCIDENT
    assert asdict(record.health_after) == dict(snapshot, observed_at=(NOW + timedelta(seconds=30)).isoformat())
    assert record.intent.config_revision == 7
