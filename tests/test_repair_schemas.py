"""Real Draft 2020-12 validation of builder output and local delivery metadata.

The pinned CI test environment must provide jsonschema and its RFC3339 format
provider. Missing dependencies or unregistered formats are failures, never skips.
Cross-document binding and chronological ordering remain runtime contracts.
"""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import FormatError, ValidationError
from referencing import Registry

from atom_search.service.contracts import ServiceError
from test_repair_incidents import (
    A, AGENT_TEXT, INCIDENT, KEY, NEW_PATH, NOW, PRIVATE_PATH, RAW_LOG,
    action, attempt, builder, fixed_attempt,
)


SCHEMAS = Path(__file__).resolve().parents[1] / 'docs/contracts/agent-operations'
BAD_TIMES = ('2026-02-30T08:00:00Z', '2026-10-30T08:00:00',
             '2026-10-30T08:00:00+25:00', '2026-10-30')


@pytest.fixture(scope='module')
def formats():
    checker = FormatChecker()
    for name in ('uuid', 'date-time'):
        assert name in checker.checkers, f'CI must install a real {name} format provider'
    checker.check(INCIDENT, 'uuid')
    checker.check('2026-10-30T08:00:00Z', 'date-time')
    with pytest.raises(FormatError):
        checker.check('not-a-uuid', 'uuid')
    for value in BAD_TIMES:
        with pytest.raises(FormatError):
            checker.check(value, 'date-time')
    return checker


def validator(name, formats):
    schema = json.loads((SCHEMAS / name).read_bytes())
    Draft202012Validator.check_schema(schema)
    def no_remote_resource(uri):
        pytest.fail(f'Schemas must resolve local definitions without fetching {uri}')
    return Draft202012Validator(schema, format_checker=formats,
        registry=Registry(retrieve=no_remote_resource))


def report(record=None, **options):
    payload = builder(**options).build(record if record is not None else attempt())
    return payload, json.loads(payload.canonical_bytes)


def delivery(payload):
    # This is a schema witness, not an implemented store or delivery writer.
    return dict(schema_version=1, id=payload.id, sha256=payload.sha256,
        status='queued', attempts=0, next_attempt_at=None, last_error=None, sent_at=None)


def at_path(value, path):
    for segment in path:
        value = value[segment]
    return value


@pytest.mark.parametrize('name', ['incident.schema.json', 'delivery.schema.json'])
def test_contract_is_a_real_draft202012_schema_with_registered_formats(name, formats):
    validator(name, formats)


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
def test_actual_fixed_builder_payload_matches_kind_code_method_schema(kind, code, method, formats):
    payload, data = report(fixed_attempt(kind, code, method))
    validator('incident.schema.json', formats).validate(data)
    assert payload.sha256 == hashlib.sha256(payload.canonical_bytes).hexdigest()


@pytest.mark.parametrize('kind, code, method', [
    ('repair', 'SOURCE_UNREACHABLE', 'settings_applied'),
    ('repair', 'SOURCE_UNREACHABLE', 'storage_budget_verified'),
    ('repair', 'MODEL_MISSING', 'bounded_directory_probe'),
    ('repair', 'MODEL_CORRUPT', 'owned_database_verified'),
    ('repair', 'DATABASE_UNAVAILABLE', 'local_model_verified'),
    ('repair', 'STORAGE_LIMIT', 'owned_database_verified'),
    ('repair', 'CONFIG_INVALID', 'bounded_directory_probe'),
    ('repair', 'JOB_STALLED', 'owned_database_verified'),
    ('repair', 'DB_CORRUPT', 'owned_database_verified'),
    ('repair', 'DATABASE_VERSION_UNSUPPORTED', 'owned_database_verified'),
    ('repair', 'DATABASE_EXTENSION_MISSING', 'owned_database_verified'),
    ('settings', 'CONFIG_INVALID', 'bounded_directory_probe'),
    ('settings', 'SOURCE_UNREACHABLE', 'settings_applied'),
    ('settings', 'MODEL_MISSING', 'settings_applied'),
    ('settings', 'DATABASE_UNAVAILABLE', 'settings_applied'),
    ('settings', 'STORAGE_LIMIT', 'settings_applied'),
])
def test_schema_allof_rejects_cross_fault_fixed_claims(kind, code, method, formats):
    _, data = report(fixed_attempt('repair', 'SOURCE_UNREACHABLE', 'bounded_directory_probe'))
    data.update(kind=kind, code=code)
    data['agent_reason']['code'] = code
    data['verification']['method'] = method
    with pytest.raises(ValidationError):
        validator('incident.schema.json', formats).validate(data)


@pytest.mark.parametrize('result', ['not_fixed', 'partial', 'rolled_back', 'rollback_failed', 'interrupted'])
def test_actual_nonfixed_outcomes_match_schema_without_changing_result(result, formats):
    _, data = report(attempt(result=result))
    validator('incident.schema.json', formats).validate(data)
    assert data['result'] == result


def test_actual_keyed_aliases_and_utf8_truncated_report_match_schema(formats):
    record = attempt(config_diff=(dict(path='sources.path', source_id=A,
        before=PRIVATE_PATH, after=NEW_PATH),), actions=tuple(action() for _ in range(64)))
    full, complete = report(record)
    shortened, bounded = report(record, max_report_bytes=len(full.canonical_bytes) - 1)
    selected = validator('incident.schema.json', formats)
    selected.validate(complete)
    selected.validate(bounded)
    assert len(shortened.canonical_bytes) < len(full.canonical_bytes)
    assert bounded['details_truncated'] is True
    assert PRIVATE_PATH not in full.canonical_bytes.decode() and KEY not in full.canonical_bytes


@pytest.mark.parametrize('path', [(), ('verification',), ('rollback',), ('agent_reason',),
    ('actions', 0), ('health_before',), ('health_after', 'database'),
    ('health_after', 'storage'), ('config_diff',), ('config_diff', 'changes', 0)])
def test_incident_schema_closes_actual_report_objects(path, formats):
    _, data = report(attempt(config_diff=(dict(path='ui.theme', before='dark', after='light'),)))
    at_path(data, path)['unrecognized_secret'] = 'must never be accepted'
    with pytest.raises(ValidationError):
        validator('incident.schema.json', formats).validate(data)


@pytest.mark.parametrize('path', [('symptoms', 0), ('agent_explanation',), ('rollback', 'explanation')])
@pytest.mark.parametrize('untrusted_text', [AGENT_TEXT, RAW_LOG])
def test_incident_server_template_channels_reject_agent_text_and_raw_logs(path, untrusted_text, formats):
    # These public report channels are server templates, not free-text inputs.
    # Start with genuine output; no particular wording is asserted here.
    _, data = report()
    at_path(data, path[:-1])[path[-1]] = untrusted_text
    with pytest.raises(ValidationError):
        validator('incident.schema.json', formats).validate(data)


@pytest.mark.parametrize('path', [('id',), ('operation_id',), ('plan_id',), ('source_id',),
    ('health_before', 'sources', 0, 'source_id')])
def test_incident_schema_asserts_uuid_formats_in_actual_output(path, formats):
    _, data = report()
    at_path(data, path[:-1])[path[-1]] = 'not-a-uuid'
    with pytest.raises(ValidationError):
        validator('incident.schema.json', formats).validate(data)


@pytest.mark.parametrize('path, value', [
    (('api_version',), '1\n'),
    (('app_version',), '0.2.0\n'),
    (('agent_reason', 'evidence_refs', 0), 'health:' + 'a' * 64 + '\n'),
    (('config_diff', 'changes', 0, 'before_alias'), 'path:' + 'a' * 64 + '\n'),
])
def test_incident_grammar_rejects_trailing_newlines_in_closed_identifiers(path, value, formats):
    _, data = report(attempt(config_diff=(dict(path='sources.path', source_id=A,
        before=PRIVATE_PATH, after=NEW_PATH),)))
    at_path(data, path[:-1])[path[-1]] = value
    with pytest.raises(ValidationError):
        validator('incident.schema.json', formats).validate(data)


@pytest.mark.parametrize('path', [('started_at',), ('finished_at',), ('verification', 'checked_at'),
    ('health_before', 'observed_at'), ('health_after', 'model', 'checked_at'),
    ('health_after', 'sources', 0, 'checked_at')])
@pytest.mark.parametrize('value', BAD_TIMES)
def test_incident_schema_asserts_calendar_and_timezone_formats(path, value, formats):
    _, data = report()
    at_path(data, path[:-1])[path[-1]] = value
    with pytest.raises(ValidationError):
        validator('incident.schema.json', formats).validate(data)


def test_builder_owns_time_order_while_schema_checks_rfc3339_shape(formats):
    _, data = report()
    data['finished_at'] = (NOW - timedelta(seconds=1)).isoformat()
    # JSON Schema format checks cannot compare separate timestamps.
    validator('incident.schema.json', formats).validate(data)
    with pytest.raises(ServiceError) as caught:
        builder().build(attempt(finished_at=NOW - timedelta(seconds=1)))
    assert caught.value.code == 'invalid_argument'


def test_builder_normalizes_accepted_aware_input_to_schema_rfc3339(formats):
    record = attempt()
    raw_after = replace(record.health_after, observed_at='2026-10-30 08:00:00+00:00')
    _, data = report(replace(record, health_after=raw_after))
    validator('incident.schema.json', formats).validate(data)
    assert data['health_after']['observed_at'] == '2026-10-30T08:00:00+00:00'


def test_delivery_schema_validates_metadata_separate_from_immutable_report(formats):
    payload, data = report()
    original = payload.canonical_bytes
    local = delivery(payload)
    validator('incident.schema.json', formats).validate(data)
    validator('delivery.schema.json', formats).validate(local)
    assert (local['id'], local['sha256']) == (payload.id, hashlib.sha256(original).hexdigest())
    assert 'delivery' not in data and 'status' not in data
    assert payload.canonical_bytes == original


@pytest.mark.parametrize('status, options', [
    ('queued', {}),
    ('queued', {'attempts': 1, 'last_error': 'offline', 'next_attempt_at': '2026-10-30T08:10:00Z'}),
    ('queued', {'last_error': 'destination_not_configured'}),
    ('queued', {'last_error': 'delivery_conflict'}),
    ('sent', {'attempts': 1, 'sent_at': '2026-10-30T08:05:00Z'}),
])
def test_delivery_closed_states_and_retry_metadata(status, options, formats):
    payload, _ = report()
    local = dict(delivery(payload), status=status, **options)
    validator('delivery.schema.json', formats).validate(local)


@pytest.mark.parametrize('changes', [
    {'destination': PRIVATE_PATH}, {'remote_directory': PRIVATE_PATH},
    {'id': 'not-a-uuid'}, {'sha256': '0' * 63}, {'sha256': '0' * 64 + '\n'},
    {'sha256': 'G' * 64}, {'status': 'uploading'}, {'attempts': True},
    {'attempts': -1}, {'attempts': 9223372036854775808},
    {'last_error': 'raw exception with credentials'},
    {'next_attempt_at': '2026-02-30T08:00:00Z'}, {'next_attempt_at': '2026-10-30T08:00:00'},
    {'status': 'sent'},
    {'status': 'sent', 'sent_at': '2026-10-30T08:05:00Z', 'next_attempt_at': '2026-10-30T08:10:00Z'},
    {'status': 'sent', 'sent_at': '2026-10-30T08:05:00Z', 'last_error': 'offline'},
    {'sent_at': '2026-10-30T08:05:00Z'},
])
def test_delivery_rejects_unknown_fields_bad_identity_types_and_state_conflicts(changes, formats):
    payload, _ = report()
    local = dict(delivery(payload), **changes)
    with pytest.raises(ValidationError):
        validator('delivery.schema.json', formats).validate(local)


def test_schema_cannot_establish_cross_document_identity_binding(formats):
    payload, _ = report()
    unrelated = dict(delivery(payload), id='66666666-6666-4666-8666-666666666666', sha256='0' * 64)
    # Both documents have valid shapes. The future store must compare the
    # actual immutable report's UUID/hash, not trust shape validation alone.
    validator('delivery.schema.json', formats).validate(unrelated)
    assert unrelated['id'] != payload.id and unrelated['sha256'] != payload.sha256
