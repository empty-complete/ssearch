import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import { SourceRuntime, type ProfileIdentity } from '../src/sourceRuntime.js';
import { ServiceLifecycle } from '../src/serviceLifecycle.js';

// Real retained Node children exercise ordering only; the synthetic ready is not P2 proof.
class ExitingHandshakeRuntime extends SourceRuntime {
  brokers: ReturnType<SourceRuntime['spawnBroker']>[] = [];
  owner: ReturnType<SourceRuntime['spawnOwner']> | null = null;
  readyBufferedBeforeExit = false;
  ownerExitObservedBeforeDelivery = false;
  override spawnOwner() {
    this.owner = spawn(process.execPath, ['-e',
      "process.stdin.resume();process.stdin.on('end',()=>process.exit(0))"],
    { windowsHide: true, stdio: ['pipe', 'ignore', 'ignore'] });
    return this.owner;
  }
  override spawnBroker(profile: ProfileIdentity) {
    const child = this.owner
      ? spawn(process.execPath, [join(__dirname, 'fixtures/correlated-error-broker.cjs'),
        profile.profile, profile.key, 'service_unavailable', String(this.owner.pid)],
      { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
      : spawn(process.execPath, ['-e', 'process.exit(1)'],
        { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    if (this.owner) {
      const retained = this.owner;
      const raw = child.stdout;
      const held = new PassThrough();
      child.stdout = held;
      raw.once('data', bytes => {
        raw.pause();
        // No bytes enter the client-facing stream until the retained owner has emitted exit.
        this.readyBufferedBeforeExit = bytes.length > 0;
        retained.once('exit', () => {
          this.ownerExitObservedBeforeDelivery = retained.exitCode !== null || retained.signalCode !== null;
          held.write(bytes);
          raw.pipe(held);
        });
        retained.kill();
      });
    }
    this.brokers.push(child);
    return child;
  }
}

test('owner exit before MAIN consumes ready cannot publish a dead generation as attached', { timeout: 20_000 }, async () => {
  const runtime = new ExitingHandshakeRuntime({ repositoryRoot: process.cwd(),
    python: process.execPath, modelDirectory: process.cwd() });
  const profile = { profile: __dirname, key: 'd'.repeat(64), serviceVersion: '0.2.0',
    pythonBase: process.execPath, pythonVenv: process.execPath };
  const lifecycle = new ServiceLifecycle(runtime, profile);
  let primaryFailed = false;
  try {
    await assert.rejects(lifecycle.ensureService(), { message: 'service_unavailable' });
    assert.equal(runtime.readyBufferedBeforeExit, true, 'real_broker_bytes_buffered');
    assert.equal(runtime.ownerExitObservedBeforeDelivery, true, 'owner_exit_before_ready_delivery');
    assert.equal(lifecycle.authority, null);
    assert.equal(runtime.brokers.length, 2);
    assert.ok(runtime.brokers.every(child => child.exitCode !== null || child.signalCode !== null));
  } catch (error) {
    primaryFailed = true;
    throw error;
  } finally {
    let cleanupError: unknown;
    try { await lifecycle.releaseService(); } catch (error) { cleanupError = error; }
    for (const child of [...runtime.brokers, ...(runtime.owner ? [runtime.owner] : [])]) {
      if (child.exitCode === null && child.signalCode === null) child.kill();
    }
    // Keep a failed contract assertion visible if strict shutdown also fails after fault injection.
    if (cleanupError && !primaryFailed) throw cleanupError;
  }
});
