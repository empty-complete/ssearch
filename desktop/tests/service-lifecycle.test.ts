import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { SourceRuntime, type ProfileIdentity } from '../src/sourceRuntime.js';
import { ServiceLifecycle } from '../src/serviceLifecycle.js';

// Real retained Node children exercise ordering only; the synthetic ready is not P2 proof.
class ExitingHandshakeRuntime extends SourceRuntime {
  brokers: ReturnType<SourceRuntime['spawnBroker']>[] = [];
  owner: ReturnType<SourceRuntime['spawnOwner']> | null = null;
  readyBufferedBeforeExit = false;
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
      child.stdout.pause();
      child.stdout.once('readable', () => {
        // Ready has been written, but MAIN must reconcile the owner's exit before consuming it.
        this.readyBufferedBeforeExit = child.stdout.readableLength > 0;
        retained.once('exit', () => child.stdout.resume());
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
  try {
    await assert.rejects(lifecycle.ensureService(), { message: 'service_unavailable' });
    assert.equal(runtime.readyBufferedBeforeExit, true);
    assert.equal(lifecycle.authority, null);
    assert.equal(runtime.brokers.length, 2);
    assert.ok(runtime.brokers.every(child => child.exitCode !== null || child.signalCode !== null));
  } finally {
    await lifecycle.releaseService();
    for (const child of [...runtime.brokers, ...(runtime.owner ? [runtime.owner] : [])]) {
      if (child.exitCode === null && child.signalCode === null) child.kill();
    }
  }
});
