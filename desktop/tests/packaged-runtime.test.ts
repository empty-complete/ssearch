import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync, realpathSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { PassThrough } from 'node:stream';
import { PackagedRuntime } from '../src/packagedRuntime.js';
import type { ProfileIdentity, RuntimeProcesses } from '../src/runtime.js';
import { BrokerFrameDecoder, encodeBrokerFrame } from '../src/brokerFrames.js';

// Synthetic launch seam proves path/flag/correlation policy, never frozen authentication.
function fixture() {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'ssearch package Кириллица ')));
  const image = join(root, 'service/ssearch-service.exe');
  mkdirSync(join(root, 'service/_internal/models'), { recursive: true });
  writeFileSync(image, 'synthetic image; never executed');
  const profilePath = join(root, 'profile');
  const parent = { pid: process.pid, executable: process.execPath, created: '132000000000000001' };
  const calls: Array<{ role: string; executable: string; args: string[]; options: object }> = [];
  const children: ReturnType<typeof spawn>[] = [];
  let alterReply: (value: Record<string, unknown>) => void = () => {};
  let profileInput: Buffer | undefined;
  let ownerInput: Promise<unknown> | undefined;
  const processes: RuntimeProcesses = {
    profile(executable, args, options) {
      calls.push({ role: 'profile', executable, args, options });
      profileInput = options.input as Buffer;
      const decoder = new BrokerFrameDecoder();
      const [context] = decoder.push(profileInput) as Array<Record<string, unknown>>;
      decoder.end();
      const reply: Record<string, unknown> = { schema_version: 1, kind: 'profile',
        session_id: context.session_id, challenge: context.challenge, profile: profilePath,
        key: 'a'.repeat(64), service_version: '0.2.0', python_base: image, python_venv: image, parent };
      alterReply(reply);
      return { pid: 1, output: [], stdout: encodeBrokerFrame(reply), stderr: Buffer.alloc(0), status: 0, signal: null };
    },
    broker(executable, args, options) {
      calls.push({ role: 'broker', executable, args, options });
      const child = spawn(process.execPath, ['-e', "process.stdin.resume();process.stdin.on('end',()=>process.exit(0))"],
        { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
      children.push(child);
      return child;
    },
    owner(executable, args, options) {
      calls.push({ role: 'owner', executable, args, options });
      const child = spawn(process.execPath, ['-e', "process.stdin.resume();process.stdin.on('end',()=>process.exit(0))"],
        { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
      const input = new PassThrough();
      child.stdin = input;
      ownerInput = new Promise(resolve => {
        const decoder = new BrokerFrameDecoder();
        input.on('data', chunk => {
          const [context] = decoder.push(chunk);
          if (context) resolve(context);
        });
      });
      children.push(child);
      return child;
    },
  };
  return { root, image, profilePath, parent, calls, processes,
    alter: (change: typeof alterReply) => { alterReply = change; },
    ownerContext: () => ownerInput, profileContext: () => profileInput,
    cleanup() { children.forEach(child => child.kill()); rmSync(root, { recursive: true, force: true }); } };
}

test('packaged runtime uses only fixed installed service modes and preserves private parent context', async () => {
  const f = fixture();
  try {
    const runtime = new PackagedRuntime({ installationRoot: f.root, expectedServiceVersion: '0.2.0' }, f.processes);
    const profile = runtime.profileIdentitySync(f.profilePath);
    assert.equal(profile.serviceVersion, '0.2.0');
    assert.equal(profile.pythonBase, f.image);
    assert.equal(profile.pythonVenv, f.image);
    runtime.spawnBroker(profile);
    runtime.spawnOwner(profile);
    assert.deepEqual(f.calls.map(call => [call.role, call.executable, call.args]), [
      ['profile', f.image, ['--profile-info', '--data-dir', f.profilePath]],
      ['broker', f.image, ['--desktop-client', '--data-dir', f.profilePath]],
      ['owner', f.image, ['--serve', '--parent-lease', '--data-dir', f.profilePath,
        '--model-dir', join(f.root, 'service/_internal/models')]],
    ]);
    for (const call of f.calls) {
      assert.equal((call.options as { cwd: string }).cwd, f.root);
      assert.equal((call.options as { windowsHide: boolean }).windowsHide, true);
    }
    assert.equal((f.calls[2].options as { detached: boolean }).detached, process.platform === 'win32');
    assert.notEqual((f.calls[1].options as { detached?: boolean }).detached, true);
    const owner = await f.ownerContext() as { parent: ProfileIdentity['parent'] };
    assert.deepEqual(owner.parent, profile.parent);
    assert.equal(f.calls.some(call => call.args.includes(profile.parent.created)), false);
  } finally { f.cleanup(); }
});

test('packaged self-report cannot replace the build version, parent or exact frozen image', () => {
  for (const change of [
    (reply: Record<string, unknown>) => { reply.service_version = '0.1.1'; },
    (reply: Record<string, unknown>) => { reply.python_base = process.execPath; },
    (reply: Record<string, unknown>) => { reply.python_venv = process.execPath; },
    (reply: Record<string, unknown>) => { reply.challenge = '00000000-0000-0000-0000-000000000001'; },
    (reply: Record<string, unknown>) => { reply.parent = { pid: process.pid + 1, executable: process.execPath, created: '1' }; },
  ]) {
    const f = fixture();
    try {
      f.alter(change);
      const runtime = new PackagedRuntime({ installationRoot: f.root, expectedServiceVersion: '0.2.0' }, f.processes);
      assert.throws(() => runtime.profileIdentitySync(f.profilePath), { message: 'service_identity_mismatch' });
      assert.equal(f.calls.filter(call => call.role !== 'profile').length, 0);
    } finally { f.cleanup(); }
  }
});

test('missing service/model closure and reparse escapes refuse launch', () => {
  for (const missing of ['service/ssearch-service.exe', 'service/_internal/models']) {
    const f = fixture();
    try {
      rmSync(join(f.root, missing), { recursive: true, force: true });
      assert.throws(() => new PackagedRuntime({ installationRoot: f.root, expectedServiceVersion: '0.2.0' }, f.processes));
      assert.equal(f.calls.length, 0);
    } finally { f.cleanup(); }
  }
  const f = fixture();
  const foreign = mkdtempSync(join(tmpdir(), 'ssearch foreign '));
  try {
    rmSync(join(f.root, 'service/_internal/models'), { recursive: true });
    symlinkSync(foreign, join(f.root, 'service/_internal/models'), 'junction');
    assert.throws(() => new PackagedRuntime({ installationRoot: f.root, expectedServiceVersion: '0.2.0' }, f.processes));
    assert.equal(f.calls.length, 0);
  } finally { f.cleanup(); rmSync(foreign, { recursive: true, force: true }); }
});

test('verified profile cannot choose a different image and later resource removal prevents launch', () => {
  const f = fixture();
  try {
    const runtime = new PackagedRuntime({ installationRoot: f.root, expectedServiceVersion: '0.2.0' }, f.processes);
    const profile = runtime.profileIdentitySync(f.profilePath);
    assert.throws(() => runtime.spawnOwner({ ...profile, pythonBase: process.execPath }),
      { message: 'service_identity_mismatch' });
    assert.equal(f.calls.length, 1);
    rmSync(f.image);
    assert.throws(() => runtime.spawnBroker(profile));
    assert.equal(f.calls.length, 1);
  } finally { f.cleanup(); }
});

test('frozen launches ignore external Python and Node bootstrap environment', () => {
  const f = fixture();
  const keys = ['PYTHONPATH', 'PYTHONHOME', '__PYVENV_LAUNCHER__', '_PYI_APPLICATION_HOME_DIR', 'NODE_OPTIONS'];
  const previous = keys.map(key => process.env[key]);
  try {
    keys.forEach(key => { process.env[key] = key === 'NODE_OPTIONS' ? '--no-warnings' : 'synthetic-external-runtime'; });
    const runtime = new PackagedRuntime({ installationRoot: f.root, expectedServiceVersion: '0.2.0' }, f.processes);
    const profile = runtime.profileIdentitySync();
    runtime.spawnBroker(profile);
    runtime.spawnOwner(profile);
    for (const call of f.calls) {
      const environment = (call.options as { env: NodeJS.ProcessEnv }).env;
      for (const key of keys) assert.equal(environment[key], undefined);
    }
    assert.deepEqual(f.calls[0].args, ['--profile-info']);
  } finally {
    keys.forEach((key, index) => {
      if (previous[index] === undefined) delete process.env[key];
      else process.env[key] = previous[index];
    });
    f.cleanup();
  }
});
