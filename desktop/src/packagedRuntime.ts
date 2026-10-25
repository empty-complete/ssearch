import type { ProfileIdentity, RuntimeProcesses, ServiceRuntime } from './runtime.js';

export interface PackagedRuntimeOptions {
  installationRoot: string;
  expectedServiceVersion: string;
}

export class PackagedRuntime implements ServiceRuntime {
  constructor(_options: PackagedRuntimeOptions, _processes?: RuntimeProcesses) {}
  profileIdentitySync(_profilePath?: string): ProfileIdentity { throw new Error('packaged_runtime_unavailable'); }
  spawnBroker(_profile: ProfileIdentity): ReturnType<ServiceRuntime['spawnBroker']> { throw new Error('packaged_runtime_unavailable'); }
  spawnOwner(_profile: ProfileIdentity): ReturnType<ServiceRuntime['spawnOwner']> { throw new Error('packaged_runtime_unavailable'); }
}
