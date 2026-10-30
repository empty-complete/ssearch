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

/** In-memory staging only. P2 still owns schema/path policy and atomic apply. */
export function reduceSourcesDraft(draft: SourcesDraft, action: SourcesDraftAction): SourcesDraft {
  if (!draft.baseline.mutable_fields.includes('sources') || !Array.isArray(draft.candidate.sources)) return draft;
  if (action.type !== 'edit' && action.type !== 'remove') return draft;
  const matches = (value: JsonValue): value is JsonObject => value !== null && typeof value === 'object'
    && !Array.isArray(value) && value.id === action.id;
  if (!draft.candidate.sources.some(matches)) return draft;
  const candidate = copyObject(draft.candidate);
  const sources = candidate.sources as JsonValue[];
  candidate.sources = action.type === 'remove' ? sources.filter(row => !matches(row))
    : sources.map(row => matches(row) ? { ...row,
      ...(action.changes.label === undefined ? {} : { label: action.changes.label }),
      ...(action.changes.enabled === undefined ? {} : { enabled: action.changes.enabled }),
    } : row);
  return { ...draft, candidate, generation: draft.generation + 1, validation: null };
}
