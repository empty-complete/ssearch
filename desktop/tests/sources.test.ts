import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import type { AccessCheck, FolderAccessRequest, SettingsSnapshot } from '../src/contracts.js';
import { FolderPicker, type FolderBackend, type FolderDialog } from '../src/folderPicker.js';

// Synthetic parent/dialog/private backend prove MAIN policy only. They do not
// open Windows UI, prove native access or establish a real P2 owner/PG gate.
const sourceId = '11111111-1111-4111-8111-111111111111';
const check: AccessCheck = { state: 'accessible', checked_at: '2026-10-31T00:00:00Z', message: 'Папка доступна сейчас.' };
const signal = () => new AbortController().signal;
function setup(dialog?: FolderDialog, backend?: Partial<FolderBackend>) {
  const parent = { isDestroyed: () => false };
  const dialogs: unknown[] = [];
  const identities: string[] = [];
  const probes: unknown[] = [];
  let clock = 0;
  const native: FolderDialog = dialog ?? { async showOpenDialog(window, options) {
    dialogs.push({ parent: window, options });
    return { canceled: false, filePaths: ['C:\\Учебные документы'] };
  } };
  const port: FolderBackend = {
    async selectSourceIdentity(path) { identities.push(path); return sourceId; },
    async checkFolder(input) { probes.push(input); return check; }, ...backend,
  };
  const picker = new FolderPicker(parent as never, native, port, () => clock);
  return { picker, parent, dialogs, identities, probes, advance: (milliseconds: number) => { clock += milliseconds; } };
}
function saved(): SettingsSnapshot {
  return { schema_version: 3, revision: 7, mutable_fields: ['sources'], settings: {
    schema_version: 3, revision: 7, sources: [{ id: sourceId, path: '\\\\study-server\\Архив', label: 'Архив', enabled: false }],
  } };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}

test('picker uses the actual retained parent, only openDirectory, and owner-resolved UUID distinct from selection ID', async () => {
  const f = setup();
  const selected = await f.picker.pickFolder(signal());
  assert.ok(selected !== null);
  assert.equal(selected.source_id, sourceId);
  assert.notEqual(selected.selection_id, selected.source_id);
  assert.equal(selected.path, 'C:\\Учебные документы');
  assert.deepEqual(f.dialogs, [{ parent: f.parent, options: { properties: ['openDirectory'] } }]);
  assert.deepEqual(f.identities, ['C:\\Учебные документы']);
  assert.deepEqual(f.probes, []);
});

test('native cancellation performs no identity lookup, access probe or source publication', async () => {
  let opened = 0;
  const f = setup({ async showOpenDialog() { opened++; return { canceled: true, filePaths: [] }; } });
  assert.equal(await f.picker.pickFolder(signal()), null);
  assert.equal(opened, 1);
  assert.deepEqual(f.identities, []);
  assert.deepEqual(f.probes, []);
});

test('repeated single selections keep owner identity for a removed/readded canonical root and preserve Unicode/UNC text', async () => {
  const paths = ['C:\\Учебные документы', 'c:\\учебные документы\\', '\\\\study-server\\Архив с пробелами'];
  const resolved = [sourceId, sourceId, '22222222-2222-4222-8222-222222222222'];
  let next = 0;
  const f = setup({ async showOpenDialog() { return { canceled: false, filePaths: [paths[next]] }; } }, {
    async selectSourceIdentity(path) { assert.equal(path, paths[next]); return resolved[next++]; },
  });
  const selections = [];
  for (let index = 0; index < paths.length; index++) {
    const selected = await f.picker.pickFolder(signal());
    assert.ok(selected !== null);
    selections.push(selected);
  }
  assert.deepEqual(selections.map(value => value.source_id), resolved);
  assert.deepEqual(selections.map(value => value.path), paths);
  assert.equal(new Set(selections.map(value => value.selection_id)).size, 3);
  assert.deepEqual(f.probes, []);
});

test('only known local selection IDs or saved UUIDs can start an explicit private probe', async () => {
  const f = setup();
  const selected = await f.picker.pickFolder(signal());
  assert.ok(selected !== null);
  assert.deepEqual(await f.picker.checkFolderAccess({ selection_id: selected.selection_id }, signal()), check);
  f.picker.recordSavedSources(saved());
  assert.deepEqual(await f.picker.checkFolderAccess({ source_id: sourceId }, signal()), check);
  assert.deepEqual(f.probes, [{ selected_path: selected.path }, { source_id: sourceId }]);
});

test('unknown, cross-renderer, expired or reset selection is refused before private path IO', async () => {
  const a = setup(); const b = setup();
  const selected = await a.picker.pickFolder(signal());
  assert.ok(selected !== null);
  for (const [picker, selection_id] of [[a.picker, randomUUID()], [b.picker, selected.selection_id]] as const) {
    await assert.rejects(picker.checkFolderAccess({ selection_id }, signal()), { message: 'access_denied' });
  }
  a.advance(10 * 60_000);
  await assert.rejects(a.picker.checkFolderAccess({ selection_id: selected.selection_id }, signal()), { message: 'access_denied' });
  a.picker.reset();
  await assert.rejects(a.picker.checkFolderAccess({ selection_id: selected.selection_id }, signal()), { message: 'access_denied' });
  assert.deepEqual(a.probes, []); assert.deepEqual(b.probes, []);
});

test('raw path, extra fields and zero/two selectors cannot enter a private probe', async () => {
  const f = setup();
  for (const input of [{}, { path: 'C:\\unselected' }, { source_id: sourceId, selection_id: randomUUID() },
    { source_id: sourceId, path: 'C:\\unselected' }]) {
    await assert.rejects(f.picker.checkFolderAccess(input as FolderAccessRequest, signal()), { message: 'invalid_request' });
  }
  assert.deepEqual(f.probes, []);
});

test('a second pending picker is refused and abort/rollover/close never issues a late selection', async () => {
  for (const ending of ['abort', 'reset', 'close'] as const) {
    const dialog = deferred<{ canceled: boolean; filePaths: string[] }>();
    let opened = 0;
    const f = setup({ showOpenDialog() { opened++; return dialog.promise; } });
    const abort = new AbortController();
    const pending = f.picker.pickFolder(abort.signal).then(() => null, error => error as Error);
    assert.equal(opened, 1);
    await assert.rejects(f.picker.pickFolder(signal()), { message: 'too_many_requests' });
    if (ending === 'abort') abort.abort(); else f.picker[ending]();
    dialog.resolve({ canceled: false, filePaths: ['C:\\Поздняя папка'] });
    const error = await pending;
    assert.ok(error !== null);
    assert.ok(['request_cancelled', 'stale_session', 'access_denied'].includes(error.message));
    assert.deepEqual(f.identities, []);
  }
});

test('destroyed parent and malformed owner identity never produce a usable selection', async () => {
  const dead = setup(); dead.parent.isDestroyed = () => true;
  await assert.rejects(dead.picker.pickFolder(signal()), { message: 'access_denied' });
  assert.deepEqual(dead.dialogs, []);
  const malformed = setup(undefined, { async selectSourceIdentity() { return 'not-a-uuid'; } });
  await assert.rejects(malformed.picker.pickFolder(signal()), { message: 'invalid_service_response' });
  assert.deepEqual(malformed.probes, []);
});

test('bounded selection lifetime evicts the oldest token without accepting another session or changing owner identity', async () => {
  const f = setup();
  let first = ''; let last = '';
  for (let index = 0; index < 65; index++) {
    const selected = await f.picker.pickFolder(signal());
    assert.ok(selected !== null);
    assert.equal(selected.source_id, sourceId);
    if (index === 0) first = selected.selection_id;
    last = selected.selection_id;
  }
  await assert.rejects(f.picker.checkFolderAccess({ selection_id: first }, signal()), { message: 'access_denied' });
  assert.deepEqual(await f.picker.checkFolderAccess({ selection_id: last }, signal()), check);
  assert.equal(f.probes.length, 1);
});

test('identity failure has no fallback UUID and no selection/access success', async () => {
  const f = setup(undefined, { async selectSourceIdentity() { throw new Error('PRIVATE_PATH_SENTINEL'); } });
  await assert.rejects(f.picker.pickFolder(signal()), error => {
    assert.ok(error instanceof Error);
    assert.ok(error.message === 'service_unavailable');
    return true;
  });
  assert.deepEqual(f.probes, []);
});
