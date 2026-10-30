"""Bounded metadata views over the existing authenticated service client.

The service credential remains the source of authority on every call. This
facade never reads a file, retains search results, or expands a caller's scope.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from atom_search.http.security import public_uuid, scope_tuple
from atom_search.http.serialization import encode, envelope
from atom_search.service import contracts as dto

if TYPE_CHECKING:
    from atom_search.service.client import ServiceClient


MAX_RESPONSE_BYTES = 256 * 1024
_ERROR_MESSAGES = {
    'invalid_argument': 'Invalid agent request arguments.',
    'invalid_request': 'Invalid service request.',
    'access_denied': 'Access is denied.',
    'authentication_required': 'Search authentication is required.',
    'service_identity_mismatch': 'The local search identity has changed.',
    'service_unavailable': 'Local search is unavailable. Retry the request.',
    'service_stopping': 'Local search is stopping.',
    'request_timeout': 'Local search did not respond in time.',
    'too_many_requests': 'Local search is busy. Retry the request.',
    'request_too_large': 'The request exceeds the allowed size.',
    'response_too_large': 'The metadata response exceeds the allowed size.',
    'capability_unavailable': 'The requested capability is unavailable.',
    'project_scope_denied': 'The project is outside the allowed search scope.',
    'source_scope_denied': 'The sources are outside the allowed search scope.',
    'result_not_found': 'The result was not found.',
    'stale_result': 'The result is stale. Repeat the search.',
    'route_not_found': 'The requested service route was not found.',
}
_READ_CAPABILITIES = frozenset({'search', 'fetch', 'status', 'settings_get',
                              'project_search', 'project_aliases'})


def _failure(code, *, retryable=False):
    # No service-supplied message, details, or unknown code reaches an agent.
    if type(code) is not str or code not in _ERROR_MESSAGES:
        code = 'service_unavailable'
    return envelope(error=dto.ServiceError(code, _ERROR_MESSAGES[code],
                                          retryable is True))


def _scalar(value):
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError('invalid_service_projection')


def _pick(value, expected, names):
    """Read only named scalar fields from a trusted DTO or settings mapping."""
    if not isinstance(value, expected):
        raise ValueError('invalid_service_projection')
    return {name: _scalar(value[name] if expected is dict else getattr(value, name))
            for name in names.split()}


def _strings(values):
    if type(values) not in (tuple, list) or any(type(item) is not str for item in values):
        raise ValueError('invalid_service_projection')
    return list(values)


def _metadata(value):
    result = _pick(value, dto.ResultMetadata,
        'result_id source_id name relative_path full_path size modified_ns stale project_id score match_kind')
    result['matched_fields'] = _strings(value.matched_fields)
    return result


def _search_response(value):
    result = _pick(value, dto.SearchResponse, 'catalog_generation')
    interpretation = _pick(value.query_interpretation, dto.QueryInterpretation,
                           'original_query normalized_query project_id')
    interpretation['expanded_terms'] = _strings(value.query_interpretation.expanded_terms)
    result.update(results=[_metadata(item) for item in value.results],
                  query_interpretation=interpretation,
                  applied_aliases=[_pick(item, dto.AppliedAlias,
                      'alias_id project_id source_id matched_text canonical_name') for item in value.applied_aliases],
                  ambiguities=[], warnings=_strings(value.warnings))
    for item in value.ambiguities:
        ambiguity = _pick(item, dto.QueryAmbiguity, 'term')
        ambiguity['options'] = []
        for option in item.options:
            public = _pick(option, dto.ProjectOption, 'project_id canonical_name')
            public['source_ids'] = _strings(option.source_ids)
            ambiguity['options'].append(public)
        result['ambiguities'].append(ambiguity)
    return result


def _settings(value):
    result = _pick(value, dto.SettingsSnapshot, 'schema_version revision')
    settings = value.settings
    public = _pick(settings, dict, 'schema_version revision')
    public['ui'] = _pick(settings['ui'], dict, 'theme global_hotkey close_action')
    public['onboarding'] = {'greeting': _pick(settings['onboarding']['greeting'], dict,
                                           'enabled show_whats_new')}
    # The HTTP read projection has already excluded unauthorized sources. The
    # facade neither selects an owner credential nor infers scope from a cache.
    public['sources'] = [_pick(item, dict, 'id path label enabled') for item in settings['sources']]
    public['search'] = _pick(settings['search'], dict, 'default_mode')
    result.update(settings=public, mutable_fields=[])
    return result


def _health_error(value):
    if value is None:
        return None
    # Component errors are status codes, never diagnostic prose or paths.
    if type(value) is str and value in {'source_access_denied', 'source_unavailable',
            'access_denied', 'service_unavailable', 'request_timeout'}:
        return value
    return 'component_unavailable'


def _health(value):
    result = _pick(value, dto.HealthSnapshot,
        'liveness readiness app_version service_version api_version observed_at index_generation')
    result['config_version'] = _pick(value.config_version, dto.ConfigVersion, 'schema_version revision')
    components = value.components
    if not isinstance(components, dto.HealthComponents):
        raise ValueError('invalid_service_projection')
    sources = []
    for item in components.sources:
        source = _pick(item, dto.SourceHealth,
            'source_id state checked_at last_success_at stale catalogued_files metadata_embedded_files '
            'catalog_counts_checked_at catalog_counts_stale')
        source['error_code'] = _health_error(item.error_code)
        sources.append(source)
    database = _pick(components.database, dto.DatabaseHealth,
        'engine version extension_version runtime_state checked_at stale')
    database['error_code'] = _health_error(components.database.error_code)
    jobs = []
    for item in components.jobs:
        job = _pick(item, dto.JobSnapshot,
            'job_id source_id state phase started_at last_progress_at discovered processed succeeded skipped errors total')
        job['error_code'] = _health_error(item.error_code)
        jobs.append(job)
    outbox = _pick(components.outbox, dto.OutboxHealth, 'queued sent repair_blocked')
    outbox['last_error'] = None  # Free-form delivery diagnostics can hold secrets.
    result['components'] = dict(sources=sources, database=database, jobs=jobs, outbox=outbox,
        model=_pick(components.model, dto.ModelHealth, 'state model_id checked_at stale lexical_available'),
        storage=_pick(components.storage, dto.StorageHealth,
            'physical_owned_bytes pgdata_bytes wal_bytes temp_bytes cache_bytes outbox_bytes backup_bytes '
            'staging_bytes database_logical_bytes quota_bytes free_bytes reserved_bytes checked_at stale maintenance_required'))
    result['capabilities'] = [item for item in _strings(value.capabilities) if item in _READ_CAPABILITIES]
    return result


class AgentReadAPI:
    """Four metadata-only reads using a caller-supplied scoped ServiceClient."""

    def __init__(self, client: ServiceClient):
        self._client = client

    @staticmethod
    def _reply(operation):
        try:
            result = envelope(data=operation())
            if len(encode(result)) > MAX_RESPONSE_BYTES:
                return _failure('response_too_large')
            return result
        except dto.ServiceError as failure:
            return _failure(failure.code, retryable=failure.retryable)
        except Exception:
            return _failure('service_unavailable', retryable=True)

    def search(self, query: str, *, source_ids: tuple[str, ...] | None = None,
               limit: int = 20) -> dict:
        if (type(query) is not str or not 1 <= len(query.strip()) <= 512
                or type(limit) is not int or not 1 <= limit <= 100):
            return _failure('invalid_argument')
        try:
            scope = scope_tuple(source_ids)
        except dto.ServiceError:
            return _failure('invalid_argument')
        request = dto.SearchRequest(query.strip(), source_ids=scope, limit=limit)
        return self._reply(lambda: _search_response(self._client.search(request)))

    def fetch(self, result_id: str) -> dict:
        try:
            identity = public_uuid(result_id)
        except dto.ServiceError:
            return _failure('invalid_argument')
        # Fresh authenticated HTTP fetch rechecks the latest credential scope.
        return self._reply(lambda: _metadata(self._client.fetch(identity)))

    def health(self) -> dict:
        return self._reply(lambda: _health(self._client.status()))

    def settings_get(self) -> dict:
        return self._reply(lambda: _settings(self._client.settings_get()))
