import { spawn, spawnSync } from 'node:child_process';
import { lstatSync, realpathSync } from 'node:fs';
import { isAbsolute, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { encodeBrokerFrame } from './brokerFrames.js';
import { decodeProfileIdentity, type ProfileIdentity, type RuntimeProcesses, type ServiceRuntime } from './runtime.js';

export interface PackagedRuntimeOptions {
  installationRoot: string;
  expectedServiceVersion: string;
}

const processes: RuntimeProcesses = {
  broker: (image, args, options) => spawn(image, args, { ...options, stdio: ['pipe', 'pipe', 'pipe'] }),
  owner: (image, args, options) => spawn(image, args, options),
  profile: (image, args, options) => spawnSync(image, args, options),
};

export class PackagedRuntime implements ServiceRuntime {
  private readonly root: string;
  private readonly image: string;
  private readonly version: string;

  constructor(options: PackagedRuntimeOptions, private readonly host: RuntimeProcesses = processes) {
    if (!isAbsolute(options.installationRoot) || !/^\d+\.\d+\.\d+$/.test(options.expectedServiceVersion)) {
      throw new Error('packaged_runtime_inputs_invalid');
    }
    this.root = realpathSync(options.installationRoot);
    this.image = join(this.root, 'service/ssearch-service.exe');
    this.version = options.expectedServiceVersion;
    this.verifyLayout();
  }

  private verifyLayout(): void {
    for (const [path, directory] of [
      [join(this.root, 'service'), true], [join(this.root, 'service/_internal'), true],
      [join(this.root, 'service/_internal/models'), true], [this.image, false],
    ] as const) {
      const stat = lstatSync(path);
      if (stat.isSymbolicLink() || (directory ? !stat.isDirectory() : !stat.isFile())
        || realpathSync(path).toLowerCase() !== path.toLowerCase()) throw new Error('packaged_resource_path_invalid');
    }
  }

  private environment(): NodeJS.ProcessEnv {
    const environment = { ...process.env };
    for (const key of Object.keys(environment)) {
      if (/^(?:PYTHON|_PYI_|__PYVENV_LAUNCHER__$|NODE_OPTIONS$|ELECTRON_RUN_AS_NODE$)/i.test(key)) delete environment[key];
    }
    return { ...environment, PYTHONNOUSERSITE: '1', PYTHONUTF8: '1' };
  }

  private verifyProfile(profile: ProfileIdentity): void {
    this.verifyLayout();
    if (profile.serviceVersion !== this.version
      || realpathSync(profile.pythonBase).toLowerCase() !== this.image.toLowerCase()
      || realpathSync(profile.pythonVenv).toLowerCase() !== this.image.toLowerCase()
      || profile.parent.pid !== process.pid
      || realpathSync(profile.parent.executable).toLowerCase() !== realpathSync(process.execPath).toLowerCase()
      || !/^[1-9][0-9]{0,19}$/.test(profile.parent.created)) throw new Error('service_identity_mismatch');
  }

  profileIdentitySync(profilePath?: string): ProfileIdentity {
    this.verifyLayout();
    const session = randomUUID();
    const challenge = randomUUID();
    const result = this.host.profile(this.image,
      ['--profile-info', ...(profilePath ? ['--data-dir', resolve(profilePath)] : [])], {
        cwd: this.root, env: this.environment(), windowsHide: true, timeout: 20_000, maxBuffer: 65_536,
        input: encodeBrokerFrame({ schema_version: 1, session_id: session, challenge,
          parent_pid: process.pid, parent_executable: realpathSync(process.execPath) }),
      });
    if (result.error || result.status !== 0) throw new Error('service_unavailable');
    return decodeProfileIdentity(result.stdout, session, challenge, { version: this.version, image: this.image });
  }

  spawnBroker(profile: ProfileIdentity): ReturnType<ServiceRuntime['spawnBroker']> {
    this.verifyProfile(profile);
    return this.host.broker(this.image, ['--desktop-client', '--data-dir', profile.profile], {
      cwd: this.root, env: this.environment(), windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    });
  }

  spawnOwner(profile: ProfileIdentity): ReturnType<ServiceRuntime['spawnOwner']> {
    this.verifyProfile(profile);
    const child = this.host.owner(this.image, ['--serve', '--parent-lease', '--data-dir', profile.profile,
      '--model-dir', join(this.root, 'service/_internal/models')], {
      // Retain outer job containment while allowing the verified parent lease to close P2 gracefully.
      detached: process.platform === 'win32',
      cwd: this.root, env: this.environment(), windowsHide: true, stdio: ['pipe', 'ignore', 'ignore'],
    });
    if (!child.stdin) throw new Error('service_control_pipe_required');
    child.stdin.on('error', () => {});
    child.stdin.write(encodeBrokerFrame({ schema_version: 1, session_id: randomUUID(),
      challenge: randomUUID(), parent: profile.parent }));
    return child;
  }
}
