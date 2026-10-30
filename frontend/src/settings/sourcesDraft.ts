import type { JsonObject, JsonValue, SearchClient, SettingsSnapshot, SettingsValidation } from '../bridge/types';

/** The existing P2 port; this draft contract adds no writer or native method. */
export type SourcesSettingsPort = Pick<SearchClient, 'settingsGet' | 'settingsValidate' | 'settingsApply'>;

export interface SourceDraftRow {
  id: string;
  path: string;
  label: string;
  enabled: boolean;
}

export interface SourcesDraft {
  readonly baseline: SettingsSnapshot;
  readonly candidate: JsonObject;
  readonly generation: number;
  readonly validation: SettingsValidation | null;
}

export type SourcesDraftAction =
  | { type: 'edit'; id: string; changes: { label?: string; enabled?: boolean } }
  | { type: 'remove'; id: string }
  // A future host adapter supplies an identified row. null is picker cancel;
  // it must not discard other changes already staged in this session.
  | { type: 'folder_selected'; source: SourceDraftRow | null }
  | { type: 'discard' };

function copyValue(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(copyValue);
  if (value !== null && typeof value === 'object') return copyObject(value);
  return value;
}

function copyObject(value: JsonObject): JsonObject {
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, copyValue(item)]));
}

/** Clone the full supplied snapshot. No defaults, path validation, probes,
 * revision increment, persistence or authority are inferred from its contents. */
export function createSourcesDraft(snapshot: SettingsSnapshot): SourcesDraft {
  return {
    baseline: { ...snapshot, settings: copyObject(snapshot.settings), mutable_fields: [...snapshot.mutable_fields] },
    candidate: copyObject(snapshot.settings),
    generation: 0,
    validation: null,
  };
}

/** Unwired fail-closed contract scaffold. Editing awaits assertion RED and the
 * Task1 packaging boundary; it never simulates validate/apply success. */
export function reduceSourcesDraft(draft: SourcesDraft, _action: SourcesDraftAction): SourcesDraft {
  return draft;
}
