"""Pure, bounded, redacted incident payloads from trusted owner records.

No files, random IDs, credentials, delivery or application settings are read.
The host must inject its protected alias key; this module neither obtains nor
publishes key material. Record dictionaries are closed structured values, not
agent log text. Building a report grants no repair or mutation authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import ntpath
import re
from types import MappingProxyType

from atom_search.service.contracts import (
    ConfigVersion, DatabaseHealth, HealthComponents, HealthSnapshot, JobSnapshot,
    ModelHealth, OutboxHealth, ServiceError, SourceHealth, StorageHealth,
)

from .catalog import POLICY_ACTIONS
from .contracts import (AgentReason, AttemptIntent, AttemptRecord, IncidentPayload,
    repair_error, valid_integer, valid_uuid)


_RESULTS = frozenset({'fixed', 'not_fixed', 'partial', 'rolled_back', 'rollback_failed', 'interrupted'})
_SYMPTOMS = {
    'SOURCE_UNREACHABLE': 'Последняя проверка не смогла связаться с источником.',
    'SOURCE_ACCESS_DENIED': 'Последняя проверка получила отказ доступа к источнику.',
    'MODEL_MISSING': 'Проверенная локальная модель недоступна.',
    'MODEL_CORRUPT': 'Проверка локальной модели не пройдена.',
    'DB_BUSY': 'Собственная база данных занята.',
    'DATABASE_UNAVAILABLE': 'Собственная база данных недоступна.',
    'DATABASE_PORT_CONFLICT': 'Порт собственного runtime занят.',
    'DATABASE_VERSION_UNSUPPORTED': 'Версия базы данных не поддерживается.',
    'DATABASE_EXTENSION_MISSING': 'Необходимое расширение базы данных недоступно.',
    'DB_CORRUPT': 'Проверка базы данных не пройдена.',
    'STORAGE_LIMIT': 'Достигнут предел выделенного места.',
    'CONFIG_INVALID': 'Проверка конфигурации не пройдена.',
    'JOB_STALLED': 'Не подтверждён прогресс задания.',
}
_EXPLANATIONS = {
    'fixed': 'Проверка конкретного условия после попытки завершилась успешно.',
    'not_fixed': 'Попытка не восстановила требуемое состояние; требуется решение владельца.',
    'partial': 'Подтверждено только частичное восстановление.',
    'rolled_back': 'Изменения отменены; исходная проблема остаётся видимой.',
    'rollback_failed': 'Восстановление предыдущего состояния не подтверждено.',
    'interrupted': 'Попытка была прервана; успешное восстановление не подтверждено.',
}
_ROLLBACK_REASONS = {
    'no_changes': 'Изменений конфигурации или индекса не было.',
    'not_required': 'Откат не требовался.',
    'restored': 'Предыдущее состояние восстановлено.',
    'restore_failed': 'Восстановление предыдущего состояния не подтверждено.',
    'not_reversible': 'Действие не допускает автоматического отката.',
    'interrupted': 'Проверка отката была прервана.',
}
_METHODS = frozenset({'bounded_directory_probe', 'source_job_paused', 'settings_applied',
    'local_model_verified', 'owned_database_verified', 'storage_budget_verified', 'not_performed'})
_FIXED_METHODS = {
    ('repair', 'SOURCE_UNREACHABLE'): 'bounded_directory_probe',
    ('repair', 'SOURCE_ACCESS_DENIED'): 'source_job_paused',
    ('repair', 'MODEL_MISSING'): 'local_model_verified',
    ('repair', 'MODEL_CORRUPT'): 'local_model_verified',
    ('repair', 'DB_BUSY'): 'owned_database_verified',
    ('repair', 'DATABASE_UNAVAILABLE'): 'owned_database_verified',
    ('repair', 'DATABASE_PORT_CONFLICT'): 'owned_database_verified',
    ('repair', 'STORAGE_LIMIT'): 'storage_budget_verified',
    ('repair', 'CONFIG_INVALID'): 'settings_applied',
    ('settings', 'CONFIG_INVALID'): 'settings_applied',
}
_ERRORS = frozenset(_SYMPTOMS) | frozenset({
    'source_unavailable', 'source_access_denied', 'settings_invalid', 'settings_busy',
    'settings_apply_failed', 'settings_recovery_required', 'cancelled', 'timeout',
    'stale_plan', 'revision_conflict', 'interrupted', 'unknown_error', 'server_exited',
})
_SOURCE_STATES = frozenset({'unknown', 'available', 'unreachable', 'access_denied', 'partial'})
_MODEL_STATES = frozenset({'unknown', 'ready', 'missing', 'corrupt', 'error', 'unavailable'})
_DATABASE_STATES = frozenset({'unknown', 'ready', 'starting', 'stopping', 'stopped', 'blocked', 'degraded',
    'unavailable', 'error', 'failed', 'busy', 'port_conflict', 'version_unsupported', 'extension_missing'})
_STAGES = frozenset({'started', 'completed', 'failed', 'cancelled', 'timed_out', 'skipped'})
_VERSION = re.compile(r'[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}\Z')
_API_VERSION = re.compile(r'[0-9]{1,5}(?:\.[0-9]{1,5}){0,2}\Z')
_EVIDENCE = re.compile(r'health:[0-9a-f]{64}\Z')
_DIFF_FIELDS = frozenset({'sources.path', 'indexing.interval_seconds', 'indexing.metadata_enabled', 'ui.theme'})


def _invalid():
    raise repair_error('invalid_argument')


def _closed(value, required, optional=()):
    if type(value) not in (dict, MappingProxyType) or not set(required) <= value.keys():
        _invalid()
    if value.keys() - set(required) - set(optional):
        _invalid()


def _choice(value, allowed):
    if type(value) is not str or value not in allowed:
        _invalid()
    return value


def _integer(value, minimum=0, maximum=9223372036854775807):
    if not valid_integer(value, minimum) or value > maximum:
        _invalid()
    return value


def _boolean(value):
    if type(value) is not bool:
        _invalid()
    return value


def _date(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(timezone.utc)


def _stamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not str or len(value) > 40:
        _invalid()
    try:
        parsed = _date(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        raise repair_error('invalid_argument') from None
    return parsed.isoformat()


def _error_code(value):
    if value is None:
        return None
    if type(value) is not str:
        _invalid()
    return value if value in _ERRORS else 'unknown_error'


def _version(value, *, api=False):
    if type(value) is not str or not (_API_VERSION if api else _VERSION).fullmatch(value):
        _invalid()
    return value


def _config(value):
    if type(value) is not ConfigVersion:
        _invalid()
    return dict(schema_version=_integer(value.schema_version, 1), revision=_integer(value.revision, 1))


def _maybe_integer(value):
    return None if value is None else _integer(value)


def _health(snapshot, source_id):
    """Project known scalar facts; discard IDs, model names and raw messages."""
    if type(snapshot) is not HealthSnapshot or type(snapshot.components) is not HealthComponents:
        _invalid()
    _version(snapshot.app_version)
    _version(snapshot.service_version)
    _version(snapshot.api_version, api=True)
    components = snapshot.components
    if type(components.sources) is not tuple or len(components.sources) > 64:
        _invalid()
    selected = []
    seen = set()
    for source in components.sources:
        if type(source) is not SourceHealth or not valid_uuid(source.source_id) or source.source_id in seen:
            _invalid()
        seen.add(source.source_id)
        if source_id is not None and source.source_id != source_id:
            continue
        selected.append(dict(source_id=source.source_id, state=_choice(source.state, _SOURCE_STATES),
            checked_at=_stamp(source.checked_at, nullable=True),
            last_success_at=_stamp(source.last_success_at, nullable=True), stale=_boolean(source.stale),
            error_code=_error_code(source.error_code), catalogued_files=_maybe_integer(source.catalogued_files),
            metadata_embedded_files=_maybe_integer(source.metadata_embedded_files),
            catalog_counts_checked_at=_stamp(source.catalog_counts_checked_at, nullable=True),
            catalog_counts_stale=_boolean(source.catalog_counts_stale)))
    if source_id is not None and source_id not in seen:
        _invalid()
    model, database, storage, outbox = components.model, components.database, components.storage, components.outbox
    if (type(model) is not ModelHealth or type(database) is not DatabaseHealth
            or type(storage) is not StorageHealth or type(outbox) is not OutboxHealth):
        _invalid()
    if database.engine != 'postgresql':
        _invalid()
    for version in (database.version, database.extension_version):
        if version is not None:
            _version(version)
    if type(components.jobs) is not tuple or len(components.jobs) > 4096:
        _invalid()
    jobs = dict(active=0, completed=0, failed=0, other=0)
    for job in components.jobs:
        if type(job) is not JobSnapshot or (job.source_id is not None and not valid_uuid(job.source_id)):
            _invalid()
        if source_id is not None and job.source_id != source_id:
            continue
        # No raw job errors, IDs or phase names are published.
        category = ('active' if job.state in ('running', 'queued', 'stopping') else
                    'completed' if job.state in ('completed', 'succeeded') else
                    'failed' if job.state == 'failed' else 'other')
        jobs[category] += 1
    bytes_fields = ('physical_owned_bytes', 'pgdata_bytes', 'wal_bytes', 'temp_bytes', 'cache_bytes',
        'outbox_bytes', 'backup_bytes', 'staging_bytes', 'database_logical_bytes', 'quota_bytes',
        'free_bytes', 'reserved_bytes')
    storage_view = {name: _maybe_integer(getattr(storage, name)) for name in bytes_fields}
    storage_view.update(checked_at=_stamp(storage.checked_at, nullable=True), stale=_boolean(storage.stale),
        maintenance_required=_boolean(storage.maintenance_required))
    return dict(observed_at=_stamp(snapshot.observed_at),
        liveness=_choice(snapshot.liveness, {'alive', 'stopping'}),
        readiness=_choice(snapshot.readiness, {'warming', 'ready', 'degraded', 'blocked'}),
        config_version=_config(snapshot.config_version), index_generation=_integer(snapshot.index_generation),
        sources=selected,
        model=dict(state=_choice(model.state, _MODEL_STATES), checked_at=_stamp(model.checked_at, nullable=True),
            stale=_boolean(model.stale), lexical_available=_boolean(model.lexical_available)),
        database=dict(engine='postgresql', version=database.version, extension_version=database.extension_version,
            runtime_state=_choice(database.runtime_state, _DATABASE_STATES),
            checked_at=_stamp(database.checked_at, nullable=True), stale=_boolean(database.stale),
            error_code=_error_code(database.error_code)), storage=storage_view, jobs=jobs,
        outbox=dict(queued=_integer(outbox.queued), sent=_integer(outbox.sent),
            last_error=_error_code(outbox.last_error), repair_blocked=_boolean(outbox.repair_blocked)))


def _action(value):
    _closed(value, {'id', 'type', 'stage', 'timeout_seconds', 'error_code'})
    kind = _choice(value['type'], POLICY_ACTIONS)
    if value['id'] != kind or type(value['id']) is not str:
        _invalid()
    return dict(id=kind, type=kind, stage=_choice(value['stage'], _STAGES),
        timeout_seconds=_integer(value['timeout_seconds'], 1, 30), error_code=_error_code(value['error_code']))


def _verification(value):
    _closed(value, {'method', 'checked_at', 'passed', 'error_code'})
    return dict(method=_choice(value['method'], _METHODS), checked_at=_stamp(value['checked_at']),
        passed=_boolean(value['passed']), error_code=_error_code(value['error_code']))


def _rollback(value):
    _closed(value, {'required', 'performed', 'reason'})
    reason = _choice(value['reason'], _ROLLBACK_REASONS)
    return dict(required=_boolean(value['required']), performed=_boolean(value['performed']),
        reason=reason, explanation=_ROLLBACK_REASONS[reason])


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode('utf-8')


def _bounded_canonical(report, budget):
    encoded = _canonical(report)
    if len(encoded) <= budget:
        return encoded
    actions, changes = report['actions'], report['config_diff']['changes']
    total = len(actions) + len(changes)
    if total == 0:
        raise ServiceError('incident_too_large', 'Required incident fields exceed the report budget.')
    # Retain a deterministic prefix: actions first, then configuration changes.
    # The upper bound omits at least one item: changing false to true alone
    # saves one byte but would not truthfully describe a loss of details.
    report['details_truncated'] = True
    report['actions'] = []
    report['config_diff']['changes'] = []
    best = _canonical(report)
    if len(best) > budget:
        raise ServiceError('incident_too_large', 'Required incident fields exceed the report budget.')
    low, high = 0, total - 1
    while low <= high:
        keep = (low + high) // 2
        report['actions'] = actions[:min(keep, len(actions))]
        report['config_diff']['changes'] = changes[:max(0, keep - len(actions))]
        candidate = _canonical(report)
        if len(candidate) <= budget:
            best = candidate
            low = keep + 1
        else:
            high = keep - 1
    return best


class IncidentBuilder:
    def __init__(self, *, alias_key: bytes, max_report_bytes: int = 262144):
        if type(alias_key) is not bytes or not 32 <= len(alias_key) <= 4096:
            _invalid()
        self._alias_key = alias_key
        self._max_report_bytes = _integer(max_report_bytes, 4096, 262144)

    def _alias(self, path, source_id):
        if (type(path) is not str or not 1 <= len(path) <= 32767 or '\0' in path
                or not ntpath.isabs(path) or not ntpath.splitdrive(path)[0]):
            _invalid()
        normalized = ntpath.normcase(ntpath.normpath(path))
        material = _canonical(['ssearch-incident-source-path-v1', source_id, normalized])
        return 'path:' + hmac.new(self._alias_key, material, hashlib.sha256).hexdigest()

    def _change(self, value, source_id):
        _closed(value, {'path', 'before', 'after'}, {'source_id'})
        field = _choice(value['path'], _DIFF_FIELDS)
        if field == 'sources.path':
            if source_id is None or value.get('source_id') != source_id:
                _invalid()
            return dict(path=field, source_id=source_id, before='[redacted]', after='[redacted]',
                before_alias=self._alias(value['before'], source_id), after_alias=self._alias(value['after'], source_id))
        if 'source_id' in value:
            _invalid()
        def safe(item):
            if field == 'indexing.interval_seconds':
                return _integer(item, 86400, 31536000)
            if field == 'indexing.metadata_enabled':
                return _boolean(item)
            return item if type(item) is str and item in {'light', 'dark', 'system'} else '[redacted]'
        return dict(path=field, before=safe(value['before']), after=safe(value['after']))

    def build(self, attempt: AttemptRecord) -> IncidentPayload:
        if type(attempt) is not AttemptRecord or type(attempt.intent) is not AttemptIntent:
            _invalid()
        intent = attempt.intent
        if (not valid_uuid(intent.operation_id) or not valid_uuid(intent.incident_id)
                or (intent.plan_id is not None and not valid_uuid(intent.plan_id))
                or (intent.source_id is not None and not valid_uuid(intent.source_id))):
            _invalid()
        code = _choice(intent.code, _SYMPTOMS)
        kind = _choice(intent.kind, {'settings', 'repair'})
        if code.startswith('SOURCE_') and intent.source_id is None:
            _invalid()
        started, finished = _date(intent.started_at), _date(attempt.finished_at)
        if finished < started:
            _invalid()
        _integer(intent.config_revision, 1)
        _integer(intent.index_generation)
        before, after = _health(intent.health_before, intent.source_id), _health(attempt.health_after, intent.source_id)
        if (intent.config_revision != before['config_version']['revision']
                or intent.index_generation != before['index_generation']):
            _invalid()
        reason = intent.reason
        if (type(reason) is not AgentReason or reason.code != code or type(reason.evidence_refs) is not tuple
                or not 1 <= len(reason.evidence_refs) <= 16
                or any(type(ref) is not str or not _EVIDENCE.fullmatch(ref) for ref in reason.evidence_refs)):
            _invalid()
        if (type(attempt.actions) is not tuple or len(attempt.actions) > 8192
                or type(attempt.config_diff) is not tuple or len(attempt.config_diff) > 8192):
            _invalid()
        actions = [_action(value) for value in attempt.actions]
        changes = [self._change(value, intent.source_id) for value in attempt.config_diff]
        verification, rollback = _verification(attempt.verification), _rollback(attempt.rollback)
        checked = datetime.fromisoformat(verification['checked_at'])
        if not started <= checked <= finished:
            _invalid()
        result = _choice(attempt.result, _RESULTS)
        if result == 'fixed' and (not verification['passed']
                or verification['method'] != _FIXED_METHODS.get((kind, code))
                or verification['error_code'] is not None):
            _invalid()
        report = dict(schema_version=1, id=intent.incident_id, operation_id=intent.operation_id,
            plan_id=intent.plan_id, kind=kind, code=code, started_at=started.isoformat(), finished_at=finished.isoformat(),
            symptoms=[_SYMPTOMS[code]], app_version=attempt.health_after.app_version,
            service_version=attempt.health_after.service_version, api_version=attempt.health_after.api_version,
            config_version=after['config_version'], source_id=intent.source_id,
            config_diff=dict(redacted=True, changes=changes), actions=actions,
            health_before=before, health_after=after, verification=verification, rollback=rollback,
            result=result, agent_reason=dict(code=code, evidence_refs=list(reason.evidence_refs)),
            agent_explanation=_EXPLANATIONS[result], details_truncated=False)
        encoded = _bounded_canonical(report, self._max_report_bytes)
        return IncidentPayload(intent.incident_id, encoded, hashlib.sha256(encoded).hexdigest())
