import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { SourceRuntime, type ProfileIdentity } from '../src/sourceRuntime.js';
import { ServiceLifecycle } from '../src/serviceLifecycle.js';
import type { JsonObject, UiMethod } from '../src/contracts.js';

class TrackingRuntime extends SourceRuntime {
  brokers: ReturnType<SourceRuntime['spawnBroker']>[] = [];
  owners: ReturnType<SourceRuntime['spawnOwner']>[] = [];
  override spawnBroker(profile: ProfileIdentity) {
    const child = super.spawnBroker(profile);
    this.brokers.push(child);
    return child;
  }
  override spawnOwner(profile: ProfileIdentity) {
    const child = super.spawnOwner(profile);
    this.owners.push(child);
    return child;
  }
}

const root = process.cwd();
const artifact = join(root, 'desktop/artifacts/source-lifecycle.json');
const sourceCommit = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
const completed: string[] = [];
let current = 'admission';
const receipt = async (ok: boolean, reason = 'pending') => {
  await mkdir(join(root, 'desktop/artifacts'), { recursive: true });
  await writeFile(artifact, JSON.stringify({ source_commit: sourceCommit, ok,
    completed_cases: completed, failed_case: ok ? null : current, reason_code: reason,
    runtime_versions: { node: process.versions.node }, proof_scope: 'real_source_only' }, null, 2));
};
const alive = (child: { exitCode: number | null; signalCode: string | null }) => child.exitCode === null && child.signalCode === null;
async function until(check: () => boolean | Promise<boolean>, budget = 120_000) {
  const deadline = Date.now() + budget;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('source_case_deadline');
}
async function call(service: ServiceLifecycle, method: UiMethod, args: JsonObject = {}) {
  return service.request(method, args, randomUUID(), new AbortController().signal);
}
async function data(service: ServiceLifecycle, method: UiMethod, args: JsonObject = {}) {
  const reply = await call(service, method, args);
  assert.equal(reply.error, null);
  assert.ok(reply.data && typeof reply.data === 'object');
  return reply.data as JsonObject;
}

test('actual source TS to private P2 broker and owner lifecycle', { timeout: 850_000 }, async () => {
  await receipt(false);
  assert.equal(process.env.GITHUB_ACTIONS, 'true');
  assert.equal(process.env.BUILD_MODE, 'source');
  assert.equal(process.platform, 'win32');
  assert.equal(process.versions.node, '24.22.1');
  assert.equal(sourceCommit, process.env.SOURCE_SHA);
  const directory = await mkdtemp(join(tmpdir(), 'ssearch-source-'));
  const sessions: ServiceLifecycle[] = [];
  try {
    for (const label of ['ASCII profile', 'Кириллица profile']) {
      current = label.startsWith('ASCII') ? 'ascii_scopes_revisions' : 'unicode_scopes_revisions';
      await receipt(false);
      const runtime = new TrackingRuntime({ repositoryRoot: root,
        python: join(root, '.venv/Scripts/python.exe'), modelDirectory: join(directory, 'missing-model') });
      const identity = await runtime.profileIdentity(join(directory, label));
      const same = await runtime.profileIdentity(join(directory, label, '..', label));
      assert.equal(identity.key, same.key);
      const owner = new ServiceLifecycle(runtime, identity);
      sessions.push(owner);
      const [first, second] = await Promise.all([owner.ensureService(), owner.ensureService()]);
      assert.equal(first.connection, second.connection);
      assert.equal((await owner.ensureService()).connection, first.connection);
      assert.equal(first.mode, 'owned');
      assert.equal(first.connection.owner, true);
      assert.equal(runtime.owners.filter(alive).length, 1);
      const brokerCount = runtime.brokers.length;
      await owner.ensureService();
      assert.equal(runtime.brokers.length, brokerCount);
      const attached = new ServiceLifecycle(runtime, identity);
      sessions.push(attached);
      const shared = await attached.ensureService();
      assert.equal(shared.mode, 'attached');
      assert.equal(shared.connection.generation, first.connection.generation);
      assert.equal(shared.connection.owner, true); // Read-only private owner proof, not owned mode.
      const snapshot = await data(owner, 'settings_get');
      const candidate = structuredClone(snapshot.settings) as JsonObject;
      (candidate.ui as JsonObject).theme = 'dark';
      const sources: JsonObject[] = [];
      for (const folder of ['allowed', 'private']) {
        const path = join(directory, label + '-' + folder);
        await mkdir(path);
        await writeFile(join(path, folder + '-drawing.pdf'), 'synthetic metadata fixture');
        sources.push({ id: randomUUID(), path, label: folder, enabled: true });
      }
      candidate.sources = sources;
      const revision = snapshot.revision as number;
      assert.equal((await data(owner, 'settings_validate', { candidate, expected_revision: revision })).valid, true);
      const applied = await data(owner, 'settings_apply', { candidate, expected_revision: revision });
      assert.equal(applied.revision, revision + 1);
      assert.equal((await call(attached, 'settings_apply', { candidate, expected_revision: revision })).error?.code, 'revision_conflict');
      await data(owner, 'start_catalog', { source_ids: null });
      await until(async () => {
        const result = await data(owner, 'search', { query: 'drawing', mode: 'literal', source_ids: null });
        return Array.isArray(result.results) && result.results.length === 2;
      }, 90_000);
      const all = await data(owner, 'search', { query: 'drawing', mode: 'literal', source_ids: null });
      const none = await data(owner, 'search', { query: 'drawing', mode: 'literal', source_ids: [] });
      assert.equal((all.results as unknown[]).length, 2);
      assert.deepEqual(none.results, []);
      await attached.releaseService();
      assert.equal((await call(owner, 'status')).error, null);
      assert.equal(runtime.owners.filter(alive).length, 1);
      completed.push(current);

      current = 'actual_transport_pending_cancel_late_reply';
      await receipt(false);
      const broker = runtime.brokers.filter(alive)[0];
      assert.ok(broker);
      broker.stdout.pause();
      const aborts = Array.from({ length: 64 }, () => new AbortController());
      const pending = aborts.map(abort => owner.request('status', {}, randomUUID(), abort.signal));
      assert.equal((await call(owner, 'status')).error?.code, 'too_many_requests');
      aborts.forEach(abort => abort.abort());
      for (const reply of await Promise.all(pending)) assert.equal(reply.error?.code, 'request_cancelled');
      broker.stdout.resume();
      assert.equal((await call(owner, 'status')).error, null);
      completed.push(current);

      current = 'actual_broker_lost_mutation_reply_no_retry';
      await receipt(false);
      const before = await data(owner, 'settings_get');
      const next = structuredClone(before.settings) as JsonObject;
      (next.ui as JsonObject).theme = 'light';
      broker.stdout.pause();
      const uncertain = call(owner, 'settings_apply', { candidate: next, expected_revision: before.revision! });
      broker.stdout.destroy();
      broker.kill(); // Only our retained broker child, never the discovered service.
      const lost = await uncertain;
      assert.equal(lost.error?.details.outcome, 'unknown');
      await until(() => owner.authority !== null);
      const after = await data(owner, 'settings_get');
      assert.ok(after.revision === before.revision || after.revision === (before.revision as number) + 1);
      assert.equal(runtime.owners.filter(alive).length, 1);
      completed.push(current);

      current = 'actual_service_crash_claim_recovery';
      await receipt(false);
      const process = runtime.owners.find(child => child.pid === first.connection.process.pid);
      assert.ok(process);
      process.kill(); // Fault injection only: retained child created by this test/runtime.
      await until(() => !alive(process));
      // Recovery must be driven by the retained owner's exit, without any probe request.
      await until(() => owner.authority !== null && owner.authority.generation !== first.connection.generation);
      assert.equal((await call(owner, 'status')).error, null);
      assert.equal(runtime.owners.filter(alive).length, 1);
      completed.push(current);
      await owner.releaseService();
      assert.equal(runtime.owners.filter(alive).length, 0);
    }
    current = 'complete';
    await receipt(true, 'passed');
  } catch (error) {
    await receipt(false, error instanceof assert.AssertionError ? 'assertion_failed' : 'source_case_failed');
    throw error;
  } finally {
    await Promise.allSettled(sessions.map(session => session.releaseService()));
    await rm(directory, { recursive: true, force: true });
  }
});
