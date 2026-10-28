import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBuiltinRegistry } from '../src/extensions/registry.js';
import { BuiltinRegistryError } from '../src/extensions/contracts.js';

// Literal public metadata fixture; it cannot register code or authorize itself.
const appearance = {
  id: 'ssearch.appearance', apiVersion: 1,
  capabilities: ['ui.theme-preview'], settingsNamespace: 'ssearch.appearance',
  contributions: [{
    kind: 'theme-preview', modes: [
      { value: 'system', label: 'Как в системе' },
      { value: 'light', label: 'Светлая' },
      { value: 'dark', label: 'Тёмная' },
    ],
  }],
};

function rejects(candidate: unknown, code: string): void {
  assert.throws(() => createBuiltinRegistry(candidate), (error: unknown) => {
    assert.equal(error instanceof BuiltinRegistryError, true);
    assert.equal((error as BuiltinRegistryError).code, code);
    return true;
  });
}

test('canonical host builtin resolves and supplies the existing theme preview options', () => {
  const registry = createBuiltinRegistry();
  assert.deepEqual(registry.resolve('ssearch.appearance'), appearance);
  assert.deepEqual(registry.themePreviewModes, [
    { value: 'system', label: 'Как в системе' },
    { value: 'light', label: 'Светлая' },
    { value: 'dark', label: 'Тёмная' },
  ]);
  assert.equal(registry.resolve('external.appearance'), null);
});

test('selection returns immutable canonical host data, detached from caller metadata', () => {
  const candidate = structuredClone(appearance);
  const registry = createBuiltinRegistry([candidate]);
  const resolved = registry.resolve('ssearch.appearance');
  assert.ok(resolved);
  assert.notEqual(resolved, candidate);
  candidate.settingsNamespace = 'credentials';
  candidate.capabilities.push('settings_apply');
  candidate.contributions[0].modes[0].label = 'Changed by caller';
  assert.deepEqual(resolved, appearance);
  assert.equal(Reflect.set(resolved, 'settingsNamespace', 'credentials'), false);
  assert.equal(Reflect.set(registry, 'themePreviewModes', []), false);
  assert.throws(() => Reflect.apply(Array.prototype.push, resolved.capabilities, ['settings_apply']), TypeError);
  assert.equal(Reflect.set(registry.themePreviewModes[0], 'value', 'shell'), false);
  assert.deepEqual(registry.themePreviewModes, appearance.contributions[0].modes);
});

test('duplicate and unknown builtin identities cannot replace a trusted contribution', () => {
  rejects([appearance, structuredClone(appearance)], 'duplicate_builtin');
  rejects([{ ...appearance, id: 'external.appearance' }], 'builtin_not_allowed');
  rejects([{ ...appearance, id: 'x'.repeat(65) }], 'invalid_descriptor');
  rejects([{ ...appearance, builtin: true }], 'invalid_descriptor');
  rejects([{ ...appearance, handler: '/untrusted/module.js' }], 'invalid_descriptor');
  rejects([{ ...appearance, path: 'C:/untrusted/plugin.js' }], 'invalid_descriptor');
});

test('unknown versions and service or execution permissions are rejected', () => {
  for (const apiVersion of [0, 2, '1', true]) {
    rejects([{ ...appearance, apiVersion }], 'unsupported_api_version');
  }
  for (const capability of ['settings_apply', 'search', 'shell', 'fs', 'sql', 'ipc', 'ui.unknown']) {
    rejects([{ ...appearance, capabilities: [capability] }], 'capability_not_allowed');
  }
  rejects([{ ...appearance, capabilities: ['ui.theme-preview', 'ui.theme-preview'] }], 'capability_not_allowed');
});

test('reserved, foreign and oversized settings namespaces cannot become a writer namespace', () => {
  for (const settingsNamespace of [
    'ui', 'runtime', 'credentials', 'sources', 'ssearch.settings', 'external.appearance',
    '../settings', 'ssearch.' + 'x'.repeat(65),
  ]) rejects([{ ...appearance, settingsNamespace }], 'settings_namespace_not_allowed');
});

test('spoofed contribution data and executable fields cannot alter the builtin', () => {
  for (const contributions of [
    [], [{ kind: 'shell', command: 'untrusted' }],
    [{ kind: 'theme-preview', modes: [{ value: 'dark', label: 'Injected' }] }],
    [{ ...appearance.contributions[0], handler: () => { throw new Error('must never execute'); } }],
    [{ ...appearance.contributions[0], modes: [...appearance.contributions[0].modes, { value: 'custom', label: 'Custom' }] }],
  ]) rejects([{ ...appearance, contributions }], 'contribution_not_allowed');
});

test('malformed collections fail closed without reading getters or executing caller code', () => {
  for (const candidate of [null, {}, 'ssearch.appearance', Array(17).fill(appearance)]) {
    rejects(candidate, 'invalid_descriptor');
  }
  let reads = 0;
  const descriptor = Object.defineProperty({ ...appearance }, 'apiVersion', {
    get() { reads++; return 1; }, enumerable: true,
  });
  rejects([descriptor], 'invalid_descriptor');
  assert.equal(reads, 0);
  assert.deepEqual(createBuiltinRegistry([]).descriptors, []);
});
