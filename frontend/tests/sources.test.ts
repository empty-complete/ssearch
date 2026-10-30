import { describe, expect, it } from 'vitest';
import type { JsonObject, SettingsSnapshot } from '../src/bridge/types';
import { createSourcesDraft, reduceSourcesDraft, type SourceDraftRow } from '../src/settings/sourcesDraft';

const local: SourceDraftRow = {
  id: '11111111-1111-4111-8111-111111111111', path: 'C:\\Учебный каталог', label: 'Документы', enabled: true,
};
const network: SourceDraftRow = {
  id: '22222222-2222-4222-8222-222222222222', path: '\\\\study-server\\Архив с пробелами', label: 'Архив', enabled: false,
};

// Synthetic DTO projection for pure draft policy, not a complete P2 settings
// document, native folder grant, service response or persistence proof.
function snapshot(sources: SourceDraftRow[]): SettingsSnapshot {
  return {
    schema_version: 3,
    revision: 7,
    mutable_fields: ['sources', 'ui'],
    settings: {
      schema_version: 3, revision: 7, sources: sources.map(row => ({ ...row })),
      ui: { theme: 'system' }, projects: [],
    },
  };
}

describe('sources draft public contract (unwired, no native or service actions)', () => {
  it('preserves fresh [] and existing source identities/revision without shared mutable rows', () => {
    for (const sources of [[], [local, network]]) {
      const saved = snapshot(sources);
      const draft = createSourcesDraft(saved);
      expect(draft.baseline).toEqual(saved);
      expect(draft.candidate).toEqual(saved.settings);
      expect(draft.baseline).not.toBe(saved);
      expect(draft.candidate).not.toBe(saved.settings);
      expect(draft.candidate.sources).not.toBe(saved.settings.sources);
      expect(draft.candidate.sources).not.toBe(draft.baseline.settings.sources);
      expect(draft.baseline.revision).toBe(7);
      expect(draft.validation).toBeNull();
    }
  });

  it('stages label/enabled changes while saved roots, UUIDs, revision and unrelated settings stay unchanged', () => {
    const saved = snapshot([local, network]);
    const draft = createSourcesDraft(saved);
    const edited = reduceSourcesDraft(draft, {
      type: 'edit', id: local.id, changes: { label: 'Рабочие документы', enabled: false },
    });
    expect(edited.candidate.sources).toEqual([{ ...local, label: 'Рабочие документы', enabled: false }, network]);
    expect(saved).toEqual(snapshot([local, network]));
    expect(draft.candidate).toEqual(saved.settings);
    expect(edited.baseline).toEqual(saved);
    expect(edited.candidate.revision).toBe(7);
    expect(edited.candidate.ui).toEqual(saved.settings.ui);
    expect(edited.candidate.projects).toEqual(saved.settings.projects);
    expect(edited.generation).toBeGreaterThan(draft.generation);
  });

  it('stages removal without publishing roots or rewriting other P2 settings', () => {
    const saved = snapshot([local, network]);
    const draft = createSourcesDraft(saved);
    const removed = reduceSourcesDraft(draft, { type: 'remove', id: local.id });
    expect(removed.candidate.sources).toEqual([network]);
    expect(removed.baseline).toEqual(saved);
    expect(saved.settings.sources).toEqual([local, network]);
    expect(removed.candidate.projects).toEqual(saved.settings.projects);
    expect(removed.candidate.revision).toBe(7);
  });

  it('native picker cancel(null) keeps the current staged changes instead of discarding them', () => {
    const saved = snapshot([local]);
    const edited = reduceSourcesDraft(createSourcesDraft(saved), {
      type: 'edit', id: local.id, changes: { label: 'Черновик' },
    });
    expect(edited.candidate.sources).toEqual([{ ...local, label: 'Черновик' }]);
    const cancelled = reduceSourcesDraft(edited, { type: 'folder_selected', source: null });
    expect(cancelled).toBe(edited);
    expect(cancelled.candidate.sources).toEqual([{ ...local, label: 'Черновик' }]);
    expect(cancelled.baseline).toEqual(saved);
    expect(cancelled.candidate.revision).toBe(7);
  });

  it('UI discard restores the baseline rows and revision without applying staged edits', () => {
    const saved = snapshot([local, network]);
    const edited = reduceSourcesDraft(createSourcesDraft(saved), {
      type: 'edit', id: local.id, changes: { label: 'Не сохранять', enabled: false },
    });
    expect(edited.candidate.sources).toEqual([{ ...local, label: 'Не сохранять', enabled: false }, network]);
    const discarded = reduceSourcesDraft(edited, { type: 'discard' });
    expect(discarded.candidate).toEqual(saved.settings);
    expect(discarded.baseline).toEqual(saved);
    expect(saved).toEqual(snapshot([local, network]));
    expect(discarded.validation).toBeNull();
  });

  it('keeps fresh empty state on picker cancel without selecting a default root', () => {
    const saved = snapshot([]);
    const draft = createSourcesDraft(saved);
    expect(reduceSourcesDraft(draft, { type: 'folder_selected', source: null })).toBe(draft);
    expect(draft.candidate.sources).toEqual([]);
    expect(draft.baseline).toEqual(saved);
  });

  it('does not coerce an invalid null or missing sources field into [] or a default root', () => {
    const invalidSettings: JsonObject[] = [{ schema_version: 3, revision: 7, sources: null },
      { schema_version: 3, revision: 7 }];
    for (const settings of invalidSettings) {
      const saved = { ...snapshot([]), settings };
      const draft = createSourcesDraft(saved);
      expect(draft.candidate).toEqual(settings);
      expect(Object.hasOwn(draft.candidate, 'sources')).toBe(Object.hasOwn(settings, 'sources'));
      expect(draft.baseline).toEqual(saved);
    }
  });
});
