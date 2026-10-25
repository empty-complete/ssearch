"""One-way desktop settings migration. Runtime observations are not config."""
from __future__ import annotations

import math
from copy import deepcopy

from .schema import CURRENT_SETTINGS_SCHEMA, settings_error, validate_settings

DEFAULT_UPDATE_SOURCE = {
    'kind': 'github',
    'manifest_location': 'https://github.com/empty-complete/ssearch/releases/latest/download/update-manifest.json',
    'allowed_origins': ['https://github.com', 'https://release-assets.githubusercontent.com'],
    'credential_ref': None,
}


def default_settings() -> dict:
    return {
        'schema_version': CURRENT_SETTINGS_SCHEMA, 'revision': 1,
        'database': {'mode': 'managed_postgresql',
                     'runtime_manifest': 'resources/postgresql/manifest.json',
                     'data_subdirectory': 'engine/postgresql/18/data'},
        'sources': [],
        'indexing': {'metadata_enabled': True, 'interval_seconds': 86400, 'deep_profile': 'off'},
        'storage': {'quota_bytes': 5368709120, 'min_free_bytes': 2147483648},
        'ui': {'theme': 'system', 'global_hotkey': 'Ctrl+Alt+Space', 'close_action': 'hide_to_tray'},
        'updates': {'enabled': True, 'channel': 'stable', 'source': deepcopy(DEFAULT_UPDATE_SOURCE)},
        'onboarding': {'greeting': {'enabled': True, 'show_whats_new': True}},
        'search': {'default_mode': 'hybrid', 'project_aliases': []}, 'projects': [],
        'agent': {'repair_policy': {'enabled': False, 'allowed_actions': [],
                  'allow_permission_expansion': False, 'max_concurrent_repairs': 1, 'plan_ttl_seconds': 600}},
        'diagnostics': {'remote_directory': None, 'remote_credential_ref': None, 'probe_stale_seconds': 300,
            'outbox': {'relative_directory': 'diagnostics/outbox', 'max_unsent_reports': 100,
                       'max_unsent_bytes': 33554432, 'max_report_bytes': 262144, 'sent_retention_days': 30,
                       'max_sent_reports': 100, 'max_sent_bytes': 33554432, 'max_total_bytes': 67108864},
            'retry': {'initial_seconds': 60, 'max_seconds': 1800, 'jitter': True,
                      'max_attempts_per_report_per_day': 8}},
    }


def legacy_runtime_state(value: dict) -> dict:
    result = {'schema_version': 1, 'last_attempt': 0, 'last_refresh': 0}
    for key in ('last_attempt', 'last_refresh'):
        item = value.get(key, 0)
        if type(item) not in (int, float) or not math.isfinite(item) or item < 0:
            raise settings_error('settings_invalid')
        result[key] = item
    return result


def migrate_settings_v1(value: dict) -> dict:
    """Pure format conversion: publication owns the single revision increase."""
    before = validate_settings(value, allow_legacy=True)
    if before['schema_version'] != 1:
        raise settings_error('settings_schema_unsupported')
    return validate_settings(dict(before, schema_version=CURRENT_SETTINGS_SCHEMA))


def migrate_settings_v2(value: dict) -> dict:
    """Allow empty profiles without replacing existing sources or update choices."""
    before = validate_settings(value, allow_legacy=True)
    if before['schema_version'] != 2:
        raise settings_error('settings_schema_unsupported')
    return validate_settings(dict(before, schema_version=CURRENT_SETTINGS_SCHEMA))


def migrate_legacy_settings(value: dict, *, source_id: str) -> dict:
    if type(value) is not dict:
        raise settings_error('settings_invalid')
    if 'schema_version' in value:
        raise settings_error('settings_schema_unsupported')
    if set(value) - {'root', 'refresh_days', 'last_attempt', 'last_refresh', 'storage'}:
        raise settings_error('settings_invalid')
    root, days = value.get('root'), value.get('refresh_days', 1)
    if ('root' in value and (type(root) is not str or not root.strip())) or type(days) is not int or not 1 <= days <= 365:
        raise settings_error('settings_invalid')
    legacy_runtime_state(value)
    result = default_settings()
    if root is not None:
        result['sources'] = [dict(id=source_id, path=root, label='', enabled=True)]
    result['indexing']['interval_seconds'] = days * 86400
    if 'storage' in value:
        if type(value['storage']) is not dict or set(value['storage']) - {'quota_bytes', 'min_free_bytes'}:
            raise settings_error('settings_invalid')
        result['storage'].update(value['storage'])
    return validate_settings(result)
