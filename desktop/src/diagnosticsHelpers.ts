import { spawn, spawnSync } from 'node:child_process';
import { basename, dirname, join } from 'node:path';
import { lstatSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import type { Readable } from 'node:stream';
import { BrokerFrameDecoder, encodeBrokerFrame } from './brokerFrames.js';
import { decodeProfileIdentity, type ProfileIdentity, type RuntimeProcesses } from './runtime.js';
import { frozenServiceEnvironment } from './packagedRuntime.js';
import { canonicalDirectory, type UiProofRequest } from './diagnosticsPolicy.js';
import { exactKeys, plainObject, publicUuid } from './validation.js';

type Role = NonNullable<UiProofRequest['helperRole']>;
export const helperStages = ['admission', 'native_profile', 'spawn', 'reply', 'child_exit', 'deadline'] as const;
export const helperCategories = ['refused', 'spawn_error', 'identity_refused', 'decode_refused', 'binding_refused',
  'missing', 'truncated', 'duplicate', 'nonzero', 'signal', 'timeout', 'write_error'] as const;
export type HelperStage = typeof helperStages[number];
export type HelperCategory = typeof helperCategories[number];
export interface HelperFailureDetails { stage: HelperStage; category: HelperCategory }
export const nativeFailureCategories = ['spawn_error', 'identity_refused', 'nonzero', 'signal', 'timeout'] as const;

// Private error identity carries correlation only. Neither raw native output nor
// Error text is inspected by the public receipt projection.
const failures = new WeakMap<object, { details: HelperFailureDetails; context?: HelperReplyContext }>();
function helperError(stage: HelperStage, category: HelperCategory, context?: HelperReplyContext): Error {
  const error = new Error(stage === 'admission' || stage === 'native_profile'
    ? 'ui_proof_helper_context_invalid' : 'ui_proof_helper_failed');
  failures.set(error, { details: { stage, category }, ...(context ? { context } : {}) });
  return error;
}
export function helperFailureDetails(error: unknown): HelperFailureDetails | null {
  const failure = typeof error === 'object' && error !== null ? failures.get(error) : undefined;
  return failure ? Object.freeze({ ...failure.details }) : null;
}

interface HelperMarker {
  schema_version: 1; source_commit: string; role: Role; session_id: string; challenge: string;
  profile_key: string; parent: ProfileIdentity['parent'];
}
export type HelperReplyContext = Pick<HelperMarker, 'source_commit' | 'role' | 'session_id' | 'challenge'>;
export interface HelperReplyChild {
  readonly pid?: number;
  readonly stdout: Readable;
  once(event: string, listener: (...arguments_: unknown[]) => void): unknown;
  kill(): boolean;
}
export type HelperClock = (callback: () => void, milliseconds: number) => () => void;
const helperClock: HelperClock = (callback, milliseconds) => {
  const timer = setTimeout(callback, milliseconds);
  return () => clearTimeout(timer);
};
export type HelperFrameWriter = (bytes: Buffer, callback: (error?: Error | null) => void) => unknown;
const helperOutput: HelperFrameWriter = (bytes, callback) => process.stdout.write(bytes, callback);
const nativeProfile: RuntimeProcesses['profile'] = (image, args, options) => spawnSync(image, args, options);

/** A closed negative observation, never admission/identity proof. No bytes are
 * emitted unless the existing marker binding already supplied valid correlation. */
export function writeHelperFailure(error: unknown, write: HelperFrameWriter = helperOutput,
  clock: HelperClock = helperClock): Promise<boolean> {
  const failure = typeof error === 'object' && error !== null ? failures.get(error) : undefined;
  if (!failure?.context || failure.details.stage !== 'native_profile'
    || !nativeFailureCategories.includes(failure.details.category as typeof nativeFailureCategories[number])) return Promise.resolve(false);
  const bytes = encodeBrokerFrame({ schema_version: 1, kind: 'helper_failure', ...failure.context,
    helper_pid: process.pid, stage: failure.details.stage, category: failure.details.category }, 65_536);
  return new Promise((resolve, reject) => {
    let settled = false;
    let cancel = () => {};
    const finish = (failed: boolean) => {
      if (settled) return;
      settled = true;
      cancel();
      if (failed) reject(helperError('reply', 'write_error'));
      else resolve(true);
    };
    cancel = clock(() => finish(true), 2_000);
    try { write(bytes, failure => finish(!!failure)); }
    catch { finish(true); }
  });
}

/** Read-only native SDK admission before ready/writes; the existing service frame is unchanged. */
export function authenticateUiHelper(request: UiProofRequest, root: string, sourceCommit: string, version: string,
  probe: RuntimeProcesses['profile'] = nativeProfile) {
  let context: HelperReplyContext | undefined;
  let stage: HelperStage = 'admission';
  let category: HelperCategory = 'refused';
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
    context = { source_commit: sourceCommit, role: request.helperRole,
      session_id: marker.session_id, challenge: marker.challenge };
    stage = 'native_profile';
    category = 'spawn_error';
    const image = join(root, 'service/ssearch-service.exe');
    const result = probe(image, ['--profile-info', '--data-dir', parentRoot], {
      cwd: root, env: frozenServiceEnvironment(), windowsHide: true, timeout: 20_000, maxBuffer: 65_536,
      stdio: [0, 'pipe', 'ignore'], // Only the parent's inherited exact SDK frame, no second buffered frame.
    });
    if (result.error) {
      category = (result.error as NodeJS.ErrnoException).code === 'ETIMEDOUT' ? 'timeout' : 'spawn_error';
      throw new Error();
    }
    if (result.signal || result.status !== 0) {
      category = result.signal ? 'signal' : 'nonzero';
      throw new Error();
    }
    category = 'identity_refused';
    const parent = { pid: process.ppid, executable: marker.parent.executable, created: marker.parent.created };
    const profile = decodeProfileIdentity(result.stdout, marker.session_id, marker.challenge, { version, image, parent });
    if (profile.profile.toLowerCase() !== parentRoot.toLowerCase() || profile.key !== marker.profile_key) throw new Error();
    return { profile, parentRoot, role: request.helperRole, session_id: marker.session_id, challenge: marker.challenge };
  } catch { throw helperError(stage, category, context); }
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

export function receiveHelper(child: HelperReplyChild, marker: HelperReplyContext,
  clock: HelperClock = helperClock): Promise<Record<string, unknown>> {
  return new Promise((accept, reject) => {
    const decoder = new BrokerFrameDecoder(65_536);
    let result: Record<string, unknown> | null = null;
    let firstFailure: Error | null = null;
    let seenFrame = false;
    let settled = false;
    let cancel = () => {};
    const stop = () => { try { child.kill(); } catch { /* Retained child only; never expose native errors. */ } };
    const fail = (stage: HelperStage, category: HelperCategory) => {
      if (settled) return;
      firstFailure ??= helperError(stage, category);
      settled = true;
      cancel();
      reject(firstFailure);
    };
    cancel = clock(() => { if (!settled) { fail('deadline', 'timeout'); stop(); } }, 180_000);
    child.once('error', () => fail('spawn', 'spawn_error'));
    child.stdout.on('data', chunk => {
      if (settled) return;
      let frames: unknown[];
      try { frames = decoder.push(chunk); }
      catch { fail('reply', 'decode_refused'); stop(); return; }
      for (const frame of frames) {
        if (seenFrame) { fail('reply', 'duplicate'); stop(); return; }
        seenFrame = true;
        if (!plainObject(frame) || !Number.isInteger(child.pid) || (child.pid ?? 0) <= 0
          || frame.schema_version !== 1 || frame.session_id !== marker.session_id
          || frame.challenge !== marker.challenge || frame.role !== marker.role || frame.helper_pid !== child.pid) {
          fail('reply', 'binding_refused'); stop(); return;
        }
        if (frame.kind === 'helper_failure') {
          if (!exactKeys(frame, ['schema_version', 'kind', 'session_id', 'challenge', 'role', 'source_commit',
            'helper_pid', 'stage', 'category']) || frame.source_commit !== marker.source_commit
            || frame.stage !== 'native_profile'
            || !nativeFailureCategories.includes(frame.category as typeof nativeFailureCategories[number])) {
            fail('reply', 'binding_refused'); stop(); return;
          }
          // Correlated negative metadata never establishes native admission and
          // cannot become success even when the child subsequently exits zero.
          firstFailure = helperError('native_profile', frame.category as HelperCategory);
        } else {
          if (!exactKeys(frame, ['schema_version', 'session_id', 'challenge', 'role', 'helper_pid', 'kind',
            'owner_starts', 'broker_starts', 'profile_invocations'], ['generation', 'owner_pid'])
            || frame.kind !== (marker.role === 'second-instance' ? 'second_instance' : 'orphan_ready')) {
            fail('reply', 'binding_refused'); stop(); return;
          }
          result = frame;
        }
      }
    });
    child.once('close', (code, signal) => {
      if (settled) return;
      cancel();
      try {
        decoder.end();
      } catch { fail('reply', 'truncated'); return; }
      if (firstFailure) { settled = true; reject(firstFailure); return; }
      if (signal) { fail('child_exit', 'signal'); return; }
      if (code === 2 && !result) { fail('admission', 'refused'); return; }
      if (code !== 0) { fail('child_exit', 'nonzero'); return; }
      if (!result) { fail('reply', 'missing'); return; }
      settled = true;
      accept(result);
    });
  });
}
