import { createHash } from 'node:crypto';
import { closeSync, fstatSync, fsyncSync, ftruncateSync, lstatSync, mkdirSync, openSync, readFileSync,
  realpathSync, rmdirSync, unlinkSync, writeSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { exactKeys, plainObject } from './validation.js';
import { admitUiProofPaths, canonicalDirectory, requiredUiChecks, type UiProofRequest } from './diagnosticsPolicy.js';
import { PackagedRuntime } from './packagedRuntime.js';
import type { BrokerFrameCounts } from './diagnosticsWire.js';
import type { FailureDiagnostics } from './diagnosticsOutcome.js';

export interface InstalledProofInputs {
  packaged: boolean;
  executable: string;
  resources: string;
  appPath: string;
  appVersion: string;
  versions: { electron?: string; node: string; chrome?: string };
}
export interface ProofReport {
  schema_version: 1;
  source_commit: string;
  scenario: UiProofRequest['scenario'];
  proof_scope: 'packaged_hidden_offscreen' | 'internal_packaged_helper';
  ok: boolean;
  reason_code: string;
  failure_diagnostics?: FailureDiagnostics;
  hashes: { desktop_sha256: string; service_sha256: string; app_asar_sha256: string; product_metadata_sha256: string };
  versions: { app: string; core_expected: string; service: string | null; electron: string; node: string; chrome: string };
  owner: { pid: number; created: string; generation: string; installation_id: string } | null;
  measured_counts: { scope: 'authenticated_descriptors_and_retained_children'; verified_authority_pids: number[];
    live_retained_owner_children: number; owner_starts: number; broker_starts: number; global_os_owners: null;
    global_models: null; global_pg_processes: null; read_only_profile_invocations: number;
    service_method_calls: Record<string, number>; broker_stdin_frame_counts: BrokerFrameCounts };
  checks: Array<{ name: string; status: 'pass' | 'fail' | 'not_run' }>;
  open_gates: string[];
}

function physicalSha256(path: string, files: typeof import('node:fs')): string {
  const file = files.openSync(path, 'r');
  try {
    const hash = createHash('sha256');
    const bytes = Buffer.alloc(1024 * 1024);
    let size: number;
    while ((size = files.readSync(file, bytes, 0, bytes.length, null)) > 0) hash.update(bytes.subarray(0, size));
    return hash.digest('hex');
  } finally { files.closeSync(file); }
}

/** Exclusive declared output; progress writes retain the original open file object. */
export class ProofReportFile {
  constructor(private file: number, readonly report: ProofReport) {}
  write(): void {
    const bytes = Buffer.from(JSON.stringify(this.report, null, 2) + '\n');
    if (bytes.length > 60 * 1024) throw new Error('ui_proof_report_limit');
    ftruncateSync(this.file, 0);
    writeSync(this.file, bytes, 0, bytes.length, 0);
    fsyncSync(this.file);
  }
  close(): void { closeSync(this.file); }
}

export function inspectUiProof(request: UiProofRequest, inputs: InstalledProofInputs) {
  if (!inputs.packaged) throw new Error('ui_proof_packaged_required');
  // Electron's regular fs treats ASAR as a virtual directory. Hash the physical
  // archive with its documented original-fs API, without changing process.noAsar.
  const physicalFiles = require('original-fs') as typeof import('node:fs');
  const executable = realpathSync(inputs.executable);
  const root = canonicalDirectory(dirname(executable));
  const resources = canonicalDirectory(inputs.resources);
  const archive = join(resources, 'app.asar');
  if (resources.toLowerCase() !== join(root, 'resources').toLowerCase()
    || resolve(inputs.appPath).toLowerCase() !== archive.toLowerCase()
    || !physicalFiles.lstatSync(archive).isFile() || physicalFiles.lstatSync(archive).isSymbolicLink()
    || physicalFiles.realpathSync(archive).toLowerCase() !== archive.toLowerCase()) throw new Error('ui_proof_package_invalid');
  const paths = admitUiProofPaths(request, root);
  const metadataPath = join(resources, 'product-metadata.json');
  const stat = lstatSync(metadataPath);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 65536) throw new Error('ui_proof_metadata_invalid');
  const metadata = JSON.parse(readFileSync(metadataPath, 'utf8'));
  const product = plainObject(metadata) ? metadata.product : null;
  const productKeys = ['product_name', 'product_name_ru', 'support_email', 'author_name', 'author_title',
    'organization_group', 'copyright_holder'];
  if (!plainObject(metadata) || !exactKeys(metadata, ['schema_version', 'source_commit', 'app_version', 'core_version', 'product'])
    || metadata.schema_version !== 1 || typeof metadata.source_commit !== 'string' || !/^[0-9a-f]{40}$/.test(metadata.source_commit)
    || metadata.app_version !== inputs.appVersion || typeof metadata.core_version !== 'string'
    || !/^\d+\.\d+\.\d+$/.test(metadata.core_version) || !plainObject(product)
    || !exactKeys(product, productKeys) || productKeys.some(key => typeof product[key] !== 'string'
      || !(product[key] as string).trim() || /[\u0000-\u001f\u007f]/.test(product[key] as string))
    || !inputs.versions.electron || !inputs.versions.chrome) throw new Error('ui_proof_metadata_invalid');
  const runtime = new PackagedRuntime({ installationRoot: root, expectedServiceVersion: inputs.appVersion });
  const assetRoot = join(inputs.appPath, 'renderer');
  readFileSync(join(assetRoot, 'index.html')); // Installed application bytes, never a source/dev fixture.
  const report: ProofReport = {
    schema_version: 1, source_commit: metadata.source_commit, scenario: request.scenario,
    proof_scope: request.helper ? 'internal_packaged_helper' : 'packaged_hidden_offscreen', ok: false, reason_code: 'pending',
    hashes: { desktop_sha256: physicalSha256(executable, physicalFiles), service_sha256: physicalSha256(join(root, 'service/ssearch-service.exe'), physicalFiles),
      app_asar_sha256: physicalSha256(archive, physicalFiles), product_metadata_sha256: physicalSha256(metadataPath, physicalFiles) },
    versions: { app: inputs.appVersion, core_expected: metadata.core_version, service: null,
      electron: inputs.versions.electron, node: inputs.versions.node, chrome: inputs.versions.chrome },
    owner: null,
    measured_counts: { scope: 'authenticated_descriptors_and_retained_children', verified_authority_pids: [],
      live_retained_owner_children: 0, owner_starts: 0, broker_starts: 0,
      global_os_owners: null, global_models: null, global_pg_processes: null, read_only_profile_invocations: 0,
      service_method_calls: { search: 0, fetch: 0, status: 0, settings_get: 0, settings_validate: 0,
        settings_apply: 0, start_catalog: 0, cancel_job: 0 },
      broker_stdin_frame_counts: { search: 0, fetch: 0, status: 0, settings_get: 0, settings_validate: 0,
        settings_apply: 0, start_catalog: 0, cancel_job: 0 } },
    checks: (request.helper
      ? ['native_parent_epoch_verified', request.helperRole === 'second-instance'
        ? 'same_singleton_refused_without_service_start' : 'fresh_owned_service_verified']
      : [...requiredUiChecks[request.scenario], 'global_os_model_pg_counts',
        ...(request.scenario === 'load' ? ['service_owner_verification'] : [])]).map(name => ({ name, status: 'not_run' })),
    open_gates: ['native_input', 'native_visual', 'dpi', 'narrator', 'windows_10',
      'global_os_owner_count', 'global_model_count', 'global_pg_count', 'full_p4_task3_acceptance'],
  };
  return { paths, root, resources, assetRoot, runtime, report };
}

export function reserveUiProof(prepared: ReturnType<typeof inspectUiProof>) {
  const { paths, report } = prepared;
  let descriptor: number | undefined;
  canonicalDirectory(dirname(paths.profile));
  mkdirSync(paths.profile, { mode: 0o700 });
  const createdProfile = lstatSync(paths.profile);
  try {
    canonicalDirectory(paths.profile);
    canonicalDirectory(dirname(paths.report));
    descriptor = openSync(paths.report, 'wx', 0o600);
    const opened = fstatSync(descriptor);
    const declared = lstatSync(paths.report);
    if (!declared.isFile() || declared.isSymbolicLink() || declared.ino !== opened.ino || declared.dev !== opened.dev
      || realpathSync(paths.report).toLowerCase() !== paths.report.toLowerCase()) throw new Error('ui_proof_paths_invalid');
    const writer = new ProofReportFile(descriptor, report);
    writer.write();
    return { ...prepared, writer };
  } catch (error) {
    if (descriptor !== undefined) {
      const original = fstatSync(descriptor);
      closeSync(descriptor);
      try {
        const current = lstatSync(paths.report);
        if (current.ino === original.ino && current.dev === original.dev) unlinkSync(paths.report);
      } catch { /* Never remove a replaced declared output. */ }
    }
    try {
      const current = lstatSync(paths.profile);
      if (!current.isSymbolicLink() && current.ino === createdProfile.ino && current.dev === createdProfile.dev) rmdirSync(paths.profile);
    } catch { /* Only our still-empty exclusive directory may be removed. */ }
    throw error;
  }
}
