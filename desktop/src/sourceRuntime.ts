import { spawn, spawnSync, type ChildProcessWithoutNullStreams, type ChildProcess } from 'node:child_process';
import { isAbsolute, join, resolve } from 'node:path';
import { realpathSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { BrokerFrameDecoder, encodeBrokerFrame } from './brokerFrames.js';
import { exactKeys, plainObject } from './validation.js';

export interface ProfileIdentity {
  readonly profile: string;
  readonly key: string;
  readonly serviceVersion: string;
  readonly pythonBase: string;
  readonly pythonVenv: string;
  readonly parent: { readonly pid: number; readonly executable: string; readonly created: string };
}

export interface SourceRuntimeOptions {
  repositoryRoot: string;
  python: string;
  modelDirectory: string;
}

export class SourceRuntime {
  readonly root: string;
  readonly python: string;
  readonly modelDirectory: string;
  constructor(options: SourceRuntimeOptions) {
    for (const path of [options.repositoryRoot, options.python, options.modelDirectory]) {
      if (!isAbsolute(path)) throw new Error('source_absolute_path_required');
    }
    this.root = realpathSync(options.repositoryRoot);
    this.python = realpathSync(options.python);
    this.modelDirectory = resolve(options.modelDirectory);
  }

  private environment(profile?: ProfileIdentity): NodeJS.ProcessEnv {
    return {
      ...process.env,
      PYTHONPATH: [join(this.root, 'src'), join(this.root, 'packages/atom-search-core/src')].join(';'),
      PYTHONNOUSERSITE: '1', PYTHONUTF8: '1',
      ...(profile ? { __PYVENV_LAUNCHER__: profile.pythonVenv } : {}),
    };
  }

  spawnBroker(profile: ProfileIdentity): ChildProcessWithoutNullStreams {
    return spawn(profile.pythonBase, [join(this.root, 'desktop/src/ssearch_service_adapter.py'), 'broker',
      '--profile', profile.profile], { cwd: this.root, env: this.environment(profile), windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'] });
  }

  spawnOwner(profile: ProfileIdentity): ChildProcess {
    if (profile.parent.pid !== process.pid
      || realpathSync(profile.parent.executable).toLowerCase() !== realpathSync(process.execPath).toLowerCase()) {
      throw new Error('source_parent_identity_mismatch');
    }
    const child = spawn(profile.pythonBase, [join(this.root, 'desktop/src/ssearch_service_adapter.py'),
      'owner', '--profile', profile.profile, '--model-dir', this.modelDirectory], {
      cwd: this.root, env: this.environment(profile), windowsHide: true, stdio: ['pipe', 'ignore', 'ignore'],
    });
    child.stdin!.on('error', () => {});
    child.stdin!.write(encodeBrokerFrame({ schema_version: 1, session_id: randomUUID(),
      challenge: randomUUID(), parent: profile.parent }));
    return child;
  }

  /** Before Electron ready, so userData/lock identity cannot race browser initialization. */
  profileIdentitySync(profilePath?: string): ProfileIdentity {
    const session = randomUUID();
    const challenge = randomUUID();
    const result = spawnSync(this.python, [join(this.root, 'desktop/src/ssearch_service_adapter.py'), 'profile',
      ...(profilePath ? ['--profile', resolve(profilePath)] : [])], {
      cwd: this.root, env: this.environment(), windowsHide: true, timeout: 20_000, maxBuffer: 65_536,
      input: encodeBrokerFrame({ schema_version: 1, session_id: session, challenge,
        parent_pid: process.pid, parent_executable: realpathSync(process.execPath) }),
    });
    if (result.error || result.status !== 0) throw new Error('service_unavailable');
    const decoder = new BrokerFrameDecoder();
    const frames = decoder.push(result.stdout);
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
      || reply.parent.pid !== process.pid || typeof reply.parent.executable !== 'string'
      || realpathSync(reply.parent.executable).toLowerCase() !== realpathSync(process.execPath).toLowerCase()
      || typeof reply.parent.created !== 'string' || !/^[1-9][0-9]{0,19}$/.test(reply.parent.created)) {
      throw new Error('service_identity_mismatch');
    }
    return Object.freeze({ profile: reply.profile, key: reply.key, serviceVersion: reply.service_version,
      pythonBase: realpathSync(reply.python_base), pythonVenv: realpathSync(reply.python_venv),
      parent: Object.freeze({ pid: process.pid, executable: reply.parent.executable, created: reply.parent.created }) });
  }

  async profileIdentity(profilePath?: string): Promise<ProfileIdentity> {
    return this.profileIdentitySync(profilePath);
  }
}
