import type { ChildProcess, ChildProcessWithoutNullStreams, SpawnOptions, SpawnSyncOptionsWithBufferEncoding, SpawnSyncReturns } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { isAbsolute } from 'node:path';
import { BrokerFrameDecoder } from './brokerFrames.js';
import { exactKeys, plainObject } from './validation.js';

export interface ProfileIdentity {
  readonly profile: string;
  readonly key: string;
  readonly serviceVersion: string;
  readonly pythonBase: string;
  readonly pythonVenv: string;
  readonly parent: { readonly pid: number; readonly executable: string; readonly created: string };
}

/** Trusted MAIN composition only. No renderer selects executables or process options. */
export interface ServiceRuntime {
  spawnBroker(profile: ProfileIdentity): ChildProcessWithoutNullStreams;
  spawnOwner(profile: ProfileIdentity): ChildProcess;
  profileIdentitySync(profilePath?: string): ProfileIdentity;
}

/** Narrow process seam for policy tests; successful frozen execution is a separate gate. */
export interface RuntimeProcesses {
  broker(executable: string, args: string[], options: SpawnOptions): ChildProcessWithoutNullStreams;
  owner(executable: string, args: string[], options: SpawnOptions): ChildProcess;
  profile(executable: string, args: string[], options: SpawnSyncOptionsWithBufferEncoding): SpawnSyncReturns<Buffer>;
}

/** Same bounded private bootstrap frame for source and frozen service images. */
export function decodeProfileIdentity(bytes: Buffer, session: string, challenge: string,
  expected?: { version: string; image: string;
    parent?: ProfileIdentity['parent'] }): ProfileIdentity {
  try {
    const decoder = new BrokerFrameDecoder();
    const frames = decoder.push(bytes);
    decoder.end();
    const reply = frames[0];
    if (frames.length !== 1 || !plainObject(reply)
      || !exactKeys(reply, ['schema_version', 'kind', 'session_id', 'challenge', 'profile', 'key',
        'service_version', 'python_base', 'python_venv', 'parent'])
      || reply.schema_version !== 1 || reply.kind !== 'profile' || reply.session_id !== session
      || reply.challenge !== challenge || typeof reply.key !== 'string' || !/^[0-9a-f]{64}$/.test(reply.key)
      || typeof reply.profile !== 'string' || !isAbsolute(reply.profile)
      || typeof reply.service_version !== 'string' || !/^\d+\.\d+\.\d+$/.test(reply.service_version)
      || typeof reply.python_base !== 'string' || !isAbsolute(reply.python_base)
      || typeof reply.python_venv !== 'string' || !isAbsolute(reply.python_venv)
      || !plainObject(reply.parent) || !exactKeys(reply.parent, ['pid', 'executable', 'created'])
      || reply.parent.pid !== (expected?.parent?.pid ?? process.pid) || typeof reply.parent.executable !== 'string'
      || realpathSync(reply.parent.executable).toLowerCase() !== realpathSync(expected?.parent?.executable ?? process.execPath).toLowerCase()
      || typeof reply.parent.created !== 'string' || !/^[1-9][0-9]{0,19}$/.test(reply.parent.created)
      || (expected?.parent && reply.parent.created !== expected.parent.created)) throw new Error();
    const pythonBase = realpathSync(reply.python_base);
    const pythonVenv = realpathSync(reply.python_venv);
    if (expected && (reply.service_version !== expected.version
      || pythonBase.toLowerCase() !== expected.image.toLowerCase()
      || pythonVenv.toLowerCase() !== expected.image.toLowerCase())) throw new Error();
    return Object.freeze({ profile: reply.profile, key: reply.key, serviceVersion: reply.service_version,
      pythonBase, pythonVenv,
      parent: Object.freeze({ pid: reply.parent.pid as number, executable: reply.parent.executable, created: reply.parent.created }) });
  } catch { throw new Error('service_identity_mismatch'); }
}
