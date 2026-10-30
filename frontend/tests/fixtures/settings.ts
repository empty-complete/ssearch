import type { JsonObject, SettingsSnapshot, SettingsValidation } from '../../src/bridge/types';

/** Synthetic complete schema-v3 fixture copied from P2 default_settings;
 * paths and UUIDs below are invented, never an actual profile or native grant. */
export function settingsSnapshot(revision = 7): SettingsSnapshot {
  const settings: JsonObject = {
    schema_version: 3, revision,
    database: { mode: 'managed_postgresql', runtime_manifest: 'resources/postgresql/manifest.json', data_subdirectory: 'engine/postgresql/18/data' },
    sources: [{ id: '11111111-1111-4111-8111-111111111111', path: 'C:\\Учебные документы', label: 'Документы', enabled: true }],
    indexing: { metadata_enabled: true, interval_seconds: 86400, deep_profile: 'off' },
    storage: { quota_bytes: 5368709120, min_free_bytes: 2147483648 },
    ui: { theme: 'system', global_hotkey: 'Ctrl+Alt+Space', close_action: 'hide_to_tray' },
    updates: { enabled: true, channel: 'stable', source: {
      kind: 'github', manifest_location: 'https://github.com/empty-complete/ssearch/releases/latest/download/update-manifest.json',
      allowed_origins: ['https://github.com', 'https://release-assets.githubusercontent.com'], credential_ref: null,
    } },
    onboarding: { greeting: { enabled: true, show_whats_new: true } },
    search: { default_mode: 'hybrid', project_aliases: [] }, projects: [],
    agent: { repair_policy: { enabled: false, allowed_actions: [], allow_permission_expansion: false, max_concurrent_repairs: 1, plan_ttl_seconds: 600 } },
    diagnostics: {
      remote_directory: null, remote_credential_ref: null, probe_stale_seconds: 300,
      outbox: { relative_directory: 'diagnostics/outbox', max_unsent_reports: 100, max_unsent_bytes: 33554432,
        max_report_bytes: 262144, sent_retention_days: 30, max_sent_reports: 100, max_sent_bytes: 33554432, max_total_bytes: 67108864 },
      retry: { initial_seconds: 60, max_seconds: 1800, jitter: true, max_attempts_per_report_per_day: 8 },
    },
  };
  return { schema_version: 3, revision, settings, mutable_fields: ['sources', 'indexing.interval_seconds', 'ui',
    'updates', 'onboarding', 'search.default_mode', 'projects', 'search.project_aliases', 'agent.repair_policy', 'diagnostics'] };
}

/** Controlled P2 response fixture only; this is not a second schema validator. */
export function validSettings(candidate: JsonObject, expectedRevision: number): SettingsValidation {
  return { valid: true, expected_revision: expectedRevision, normalized: { ...candidate, revision: expectedRevision + 1 },
    diff: [], warnings: [], errors: [] };
}
