import type { ChildProcess, ChildProcessWithoutNullStreams, SpawnOptions, SpawnSyncOptionsWithBufferEncoding, SpawnSyncReturns } from 'node:child_process';

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
