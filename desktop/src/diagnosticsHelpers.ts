import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { basename, dirname, join } from 'node:path';
import { lstatSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { BrokerFrameDecoder, encodeBrokerFrame } from './brokerFrames.js';
import { decodeProfileIdentity, type ProfileIdentity } from './runtime.js';
import { frozenServiceEnvironment } from './packagedRuntime.js';
import { canonicalDirectory, type UiProofRequest } from './diagnosticsPolicy.js';
import { exactKeys, plainObject, publicUuid } from './validation.js';

type Role = NonNullable<UiProofRequest['helperRole']>;
interface HelperMarker {
  schema_version: 1; source_commit: string; role: Role; session_id: string; challenge: string;
  profile_key: string; parent: ProfileIdentity['parent'];
}

/** Read-only native SDK admission before ready/writes; the existing service frame is unchanged. */
export function authenticateUiHelper(request: UiProofRequest, root: string, sourceCommit: string, version: string) {
  try {
    const helperRoot = join(dirname(request.profilePath), basename(request.profilePath));
    const directory = canonicalDirectory(dirname(helperRoot));
    if (!request.helperRole || basename(directory) !== 'helpers' || !publicUuid(basename(helperRoot))
      || request.reportPath.toLowerCase() !== join(helperRoot, 'helper.json').toLowerCase()) throw new Error();
    const parentRoot = canonicalDirectory(dirname(directory));
    const markerPath = join(directory, `${basename(helperRoot)}.context.json`);
    const stat = lstatSync(markerPath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 4096
      || realpathSync(markerPath).toLowerCase() !== markerPath.toLowerCase()) throw new Error();
    const marker: unknown = JSON.parse(readFileSync(markerPath, 'utf8'));
    if (!plainObject(marker) || !exactKeys(marker, ['schema_version', 'source_commit', 'role', 'session_id', 'challenge', 'profile_key', 'parent'])
      || marker.schema_version !== 1 || marker.source_commit !== sourceCommit || marker.role !== request.helperRole
      || !publicUuid(marker.session_id) || !publicUuid(marker.challenge)
      || typeof marker.profile_key !== 'string' || !/^[0-9a-f]{64}$/.test(marker.profile_key)
      || !plainObject(marker.parent) || !exactKeys(marker.parent, ['pid', 'executable', 'created'])
      || marker.parent.pid !== process.ppid || typeof marker.parent.executable !== 'string'
      || realpathSync(marker.parent.executable).toLowerCase() !== realpathSync(process.execPath).toLowerCase()
      || typeof marker.parent.created !== 'string' || !/^[1-9][0-9]{0,19}$/.test(marker.parent.created)) throw new Error();
    const image = join(root, 'service/ssearch-service.exe');
    const result = spawnSync(image, ['--profile-info', '--data-dir', parentRoot], {
      cwd: root, env: frozenServiceEnvironment(), windowsHide: true, timeout: 20_000, maxBuffer: 65_536,
      stdio: [0, 'pipe', 'ignore'], // Only the parent's inherited exact SDK frame, no second buffered frame.
    });
    if (result.error || result.status !== 0) throw new Error();
    const parent = { pid: process.ppid, executable: marker.parent.executable, created: marker.parent.created };
    const profile = decodeProfileIdentity(result.stdout, marker.session_id, marker.challenge, { version, image, parent });
    if (profile.profile.toLowerCase() !== parentRoot.toLowerCase() || profile.key !== marker.profile_key) throw new Error();
    return { profile, parentRoot, role: request.helperRole, session_id: marker.session_id, challenge: marker.challenge };
  } catch { throw new Error('ui_proof_helper_context_invalid'); }
}

export function writeHelperReply(context: { session_id: string; challenge: string; role: Role },
  values: { kind: 'second_instance' | 'orphan_ready'; owner_starts: number; broker_starts: number;
    profile_invocations: number; generation?: string; owner_pid?: number }): Promise<void> {
  return new Promise((resolve, reject) => process.stdout.write(encodeBrokerFrame({ schema_version: 1,
    session_id: context.session_id, challenge: context.challenge, role: context.role,
    helper_pid: process.pid, ...values }), error => error ? reject(new Error('ui_proof_helper_write_failed')) : resolve()));
}

export function startUiHelper(root: string, reportSource: string, role: Role, profile: ProfileIdentity) {
  const directory = join(root, 'helpers');
  try { mkdirSync(directory, { mode: 0o700 }); } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
  }
  canonicalDirectory(directory);
  const id = randomUUID();
  const helperRoot = join(directory, id);
  const marker: HelperMarker = { schema_version: 1, source_commit: reportSource, role, session_id: randomUUID(),
    challenge: randomUUID(), profile_key: profile.key, parent: profile.parent };
  writeFileSync(join(directory, `${id}.context.json`), JSON.stringify(marker), { flag: 'wx', mode: 0o600 });
  const child = spawn(process.execPath, ['--ui-self-test', join(helperRoot, 'helper.json'), '--ui-scenario', 'lifecycle',
    '--data-dir', helperRoot, '--ui-proof-helper', role], {
    cwd: dirname(process.execPath), env: frozenServiceEnvironment(), windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
  });
  child.stderr.resume();
  const reply = receiveHelper(child, marker);
  child.stdin.on('error', () => {});
  child.stdin.end(encodeBrokerFrame({ schema_version: 1, session_id: marker.session_id, challenge: marker.challenge,
    parent_pid: marker.parent.pid, parent_executable: marker.parent.executable }));
  return { child, helperRoot, reply };
}

function receiveHelper(child: ChildProcessWithoutNullStreams, marker: HelperMarker): Promise<Record<string, unknown>> {
  return new Promise((accept, reject) => {
    const decoder = new BrokerFrameDecoder(65_536);
    let result: Record<string, unknown> | null = null;
    const timer = setTimeout(() => { child.kill(); reject(new Error('ui_proof_helper_deadline')); }, 180_000);
    const fail = () => { clearTimeout(timer); reject(new Error('ui_proof_helper_failed')); };
    child.once('error', fail);
    child.stdout.on('data', chunk => {
      try {
        for (const frame of decoder.push(chunk)) {
          if (result || !plainObject(frame) || !exactKeys(frame,
            ['schema_version', 'session_id', 'challenge', 'role', 'helper_pid', 'kind', 'owner_starts', 'broker_starts', 'profile_invocations'],
            ['generation', 'owner_pid']) || frame.schema_version !== 1 || frame.session_id !== marker.session_id
            || frame.challenge !== marker.challenge || frame.role !== marker.role || frame.helper_pid !== child.pid) throw new Error();
          result = frame;
        }
      } catch { child.kill(); fail(); }
    });
    child.once('close', code => {
      clearTimeout(timer);
      try { decoder.end(); } catch { fail(); return; }
      if (code !== 0 || !result) fail();
      else accept(result);
    });
  });
}
