import { describe, expect, it, vi } from 'vitest';
import { SearchClientError } from '../src/bridge/client';
import type { JsonObject, SettingsSnapshot, SettingsValidation } from '../src/bridge/types';
import { SourcesController } from '../src/settings/sourcesController';
import type { SourcesSettingsPort } from '../src/settings/sourcesDraft';
import { settingsSnapshot, validSettings } from './fixtures/settings';

const sourceId = '11111111-1111-4111-8111-111111111111';
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup(overrides: Partial<SourcesSettingsPort> = {}) {
  const saved = settingsSnapshot();
  const get = vi.fn(async () => saved);
  const validate = vi.fn(async (request: { candidate: JsonObject; expected_revision: number }) =>
    validSettings(request.candidate, request.expected_revision));
  const apply = vi.fn(async (request: { candidate: JsonObject; expected_revision: number }) => ({
    ...settingsSnapshot(request.expected_revision + 1), settings: request.candidate,
  }));
  const port: SourcesSettingsPort = { settingsGet: get, settingsValidate: validate, settingsApply: apply, ...overrides };
  const controller = new SourcesController(port);
  return { controller, saved, get, validate, apply };
}
function edit(controller: SourcesController, label = 'Рабочие документы') {
  controller.dispatch({ type: 'edit', id: sourceId, changes: { label } });
}

describe('sources P2 settings session (controlled port, no real persistence/native claims)', () => {
  it('loads the full P2 JSON without defaults, validation, apply or source probes', async () => {
    const f = setup();
    await f.controller.load();
    expect(f.controller.getSnapshot().draft?.candidate).toEqual(f.saved.settings);
    expect(f.validate).not.toHaveBeenCalled();
    expect(f.apply).not.toHaveBeenCalled();
    f.controller.close();
  });

  it('validates the full staged JSON with original expected revision and publishes no new settings', async () => {
    const f = setup();
    await f.controller.load(); edit(f.controller);
    const candidate = f.controller.getSnapshot().draft!.candidate;
    await f.controller.validate();
    expect(f.validate).toHaveBeenCalledTimes(1);
    expect(f.validate.mock.calls[0][0]).toEqual({ candidate, expected_revision: 7 });
    expect(f.controller.getSnapshot().draft?.validation).toEqual(validSettings(candidate, 7));
    expect(f.controller.getSnapshot().draft?.baseline).toEqual(f.saved);
    expect(f.controller.getSnapshot().draft?.candidate.revision).toBe(7);
    expect(f.apply).not.toHaveBeenCalled();
    f.controller.close();
  });

  it('uses P2 duplicate errors and overlap warnings without local path-policy replacements', async () => {
    const errors: SettingsValidation = { valid: false, expected_revision: 7, normalized: null, diff: [], warnings: [],
      errors: [{ path: 'sources.1.path', code: 'duplicate_root', message: 'Повторяющаяся папка' }] };
    const warnings = { ...validSettings(settingsSnapshot().settings, 7), warnings: ['source_roots_overlap'] };
    const validation = vi.fn().mockResolvedValueOnce(errors).mockResolvedValueOnce(warnings);
    const f = setup({ settingsValidate: validation });
    await f.controller.load(); edit(f.controller);
    await f.controller.validate();
    expect(f.controller.getSnapshot().draft?.validation).toEqual(errors);
    await f.controller.save();
    expect(f.apply).not.toHaveBeenCalled();
    await f.controller.validate();
    expect(f.controller.getSnapshot().draft?.validation?.warnings).toEqual(['source_roots_overlap']);
    expect(f.apply).not.toHaveBeenCalled();
    f.controller.close();
  });

  it('requires explicit valid-current save and adopts only the returned P2 snapshot/revision', async () => {
    const returned = settingsSnapshot(8);
    returned.settings.sources = [{ id: sourceId, path: 'C:\\Учебные документы', label: 'Сохранено P2', enabled: true }];
    const apply = vi.fn(async () => returned);
    const f = setup({ settingsApply: apply });
    await f.controller.load(); edit(f.controller);
    await f.controller.save();
    expect(apply).not.toHaveBeenCalled();
    await f.controller.validate();
    expect(apply).not.toHaveBeenCalled();
    await f.controller.save();
    expect(apply).toHaveBeenCalledTimes(1);
    expect(f.controller.getSnapshot().draft?.baseline).toEqual(returned);
    expect(f.controller.getSnapshot().draft?.candidate).toEqual(returned.settings);
    expect(f.controller.getSnapshot().draft?.validation).toBeNull();
    f.controller.close();
  });

  it('does not grant write access to an attached settings projection with no mutable fields', async () => {
    const readonly = { ...settingsSnapshot(), mutable_fields: [] };
    const f = setup({ settingsGet: async () => readonly });
    await f.controller.load(); edit(f.controller);
    await f.controller.validate(); await f.controller.save();
    expect(f.controller.getSnapshot().draft?.candidate).toEqual(readonly.settings);
    expect(f.validate).not.toHaveBeenCalled();
    expect(f.apply).not.toHaveBeenCalled();
    f.controller.close();
  });

  it('retains staged and last-good state on definite apply failure and never retries automatically', async () => {
    const apply = vi.fn(async () => { throw new SearchClientError('settings_apply_failed'); });
    const f = setup({ settingsApply: apply });
    await f.controller.load(); edit(f.controller);
    const candidate = f.controller.getSnapshot().draft!.candidate;
    await f.controller.validate(); await f.controller.save();
    expect(apply).toHaveBeenCalledTimes(1);
    expect(f.controller.getSnapshot().error?.code).toBe('settings_apply_failed');
    expect(f.controller.getSnapshot().draft?.candidate).toEqual(candidate);
    expect(f.controller.getSnapshot().draft?.baseline).toEqual(f.saved);
    await f.controller.save();
    expect(apply).toHaveBeenCalledTimes(1);
    f.controller.close();
  });

  it('unknown mutation outcome requires explicit authoritative reread and never replays apply', async () => {
    const apply = vi.fn(async () => { throw new SearchClientError('request_timeout', { outcome: 'unknown' }); });
    const f = setup({ settingsApply: apply });
    await f.controller.load(); edit(f.controller);
    const candidate = f.controller.getSnapshot().draft!.candidate;
    await f.controller.validate(); await f.controller.save();
    expect(f.controller.getSnapshot().needsReconcile).toBe(true);
    expect(f.controller.getSnapshot().error?.details.outcome).toBe('unknown');
    expect(f.controller.getSnapshot().draft?.candidate).toEqual(candidate);
    expect(f.controller.getSnapshot().draft?.baseline).toEqual(f.saved);
    expect(f.get).toHaveBeenCalledTimes(1);
    await f.controller.validate(); await f.controller.save();
    expect(apply).toHaveBeenCalledTimes(1);
    expect(f.get).toHaveBeenCalledTimes(1);
    f.controller.close();
  });

  it('explicit conflict reload rebases only staged sources onto the latest full JSON without auto apply', async () => {
    let current = settingsSnapshot();
    const latest = settingsSnapshot(9);
    latest.settings.ui = { theme: 'dark', global_hotkey: 'Ctrl+Alt+Space', close_action: 'hide_to_tray' };
    const f = setup({ settingsGet: async () => current,
      settingsValidate: async () => { throw new SearchClientError('revision_conflict'); } });
    await f.controller.load(); edit(f.controller);
    const stagedSources = f.controller.getSnapshot().draft!.candidate.sources;
    await f.controller.validate();
    expect(f.controller.getSnapshot().error?.code).toBe('revision_conflict');
    current = latest;
    await f.controller.reload();
    expect(f.controller.getSnapshot().draft?.baseline).toEqual(latest);
    expect(f.controller.getSnapshot().draft?.candidate).toEqual({ ...latest.settings, sources: stagedSources });
    expect(f.controller.getSnapshot().draft?.validation).toBeNull();
    expect(f.apply).not.toHaveBeenCalled();
    f.controller.close();
  });

  it('late validation cannot authorize an edited generation or a discarded draft', async () => {
    for (const discard of [false, true]) {
      const reply = deferred<SettingsValidation>();
      const validate = vi.fn(() => reply.promise);
      const f = setup({ settingsValidate: validate });
      await f.controller.load(); edit(f.controller);
      const candidate = f.controller.getSnapshot().draft!.candidate;
      const pending = f.controller.validate();
      expect(validate).toHaveBeenCalledTimes(1);
      if (discard) f.controller.dispatch({ type: 'discard' });
      else edit(f.controller, 'Следующее изменение');
      reply.resolve(validSettings(candidate, 7));
      await pending;
      expect(f.controller.getSnapshot().draft?.validation).toBeNull();
      await f.controller.save();
      expect(f.apply).not.toHaveBeenCalled();
      f.controller.close();
    }
  });

  it('close fences pending validation so a late valid response cannot enable save', async () => {
    const reply = deferred<SettingsValidation>();
    const validate = vi.fn(() => reply.promise);
    const f = setup({ settingsValidate: validate });
    await f.controller.load(); edit(f.controller);
    const candidate = f.controller.getSnapshot().draft!.candidate;
    const pending = f.controller.validate();
    expect(validate).toHaveBeenCalledTimes(1);
    f.controller.close();
    const closed = f.controller.getSnapshot();
    reply.resolve(validSettings(candidate, 7));
    await pending;
    expect(f.controller.getSnapshot()).toBe(closed);
    await f.controller.save();
    expect(f.apply).not.toHaveBeenCalled();
  });

  it('close fences pending apply, including a late successful reply', async () => {
    const reply = deferred<SettingsSnapshot>();
    const apply = vi.fn(() => reply.promise);
    const f = setup({ settingsApply: apply });
    await f.controller.load(); edit(f.controller);
    await f.controller.validate();
    const pending = f.controller.save();
    expect(apply).toHaveBeenCalledTimes(1);
    f.controller.close();
    const closed = f.controller.getSnapshot();
    reply.resolve(settingsSnapshot(8));
    await pending;
    expect(f.controller.getSnapshot()).toBe(closed);
    expect(apply).toHaveBeenCalledTimes(1);
  });
});
