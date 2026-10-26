import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, realpathSync, symlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { parseUiProofArguments, admitUiProofPaths, proofChecksPassed, requiredUiChecks, type UiProofRequest } from '../src/diagnosticsPolicy.js';
import { decodeProfileIdentity } from '../src/runtime.js';
import { encodeBrokerFrame } from '../src/brokerFrames.js';
import { inspectUiProof, reserveUiProof, type ProofReport } from '../src/diagnosticsReport.js';
import { PackagedRuntime } from '../src/packagedRuntime.js';

function fixture() {
  const parent = realpathSync(mkdtempSync(join(tmpdir(), 'ssearch proof policy ')));
  const installation = join(parent, 'installed');
  mkdirSync(installation);
  const request: UiProofRequest = { scenario: 'load', profilePath: join(parent, 'new QA profile'),
    reportPath: join(parent, 'new QA profile/report.json'), helper: false };
  return { parent, installation, request, cleanup: () => rmSync(parent, { recursive: true, force: true }) };
}

test('public QA CLI is explicit and normal startup has no diagnostics side effects', () => {
  const f = fixture();
  try {
    assert.equal(parseUiProofArguments(['--data-dir', f.request.profilePath]), null);
    assert.deepEqual(parseUiProofArguments(['--ui-self-test', f.request.reportPath,
      '--ui-scenario', 'load', '--data-dir', f.request.profilePath]), f.request);
    assert.deepEqual(admitUiProofPaths(f.request, f.installation),
      { profile: f.request.profilePath, report: f.request.reportPath });
    const outside = { ...f.request, reportPath: join(f.parent, 'explicit-report.json') };
    assert.equal(admitUiProofPaths(outside, f.installation).report, outside.reportPath);
    assert.equal(existsSync(f.request.profilePath), false);
    assert.equal(existsSync(outside.reportPath), false);
  } finally { f.cleanup(); }
});

test('malformed, missing, duplicate and unknown CLI tokens are refused', () => {
  const f = fixture();
  const valid = ['--ui-self-test', f.request.reportPath, '--ui-scenario', 'load', '--data-dir', f.request.profilePath];
  try {
    for (const args of [valid.slice(0, -1), [...valid, '--data-dir', f.request.profilePath],
      [...valid, '--unknown', 'value'], ['--ui-scenario', 'load'],
      valid.map(value => value === 'load' ? 'unknown' : value), [...valid, '--ui-self-test', f.request.reportPath],
      valid.map(value => value === f.request.profilePath ? '\\\\unreachable.invalid\\share\\qa' : value),
      valid.map(value => value === f.request.reportPath ? '\\\\?\\C:\\unsafe.json' : value),
      valid.map(value => value === f.request.profilePath ? join(f.parent, 'unused') + '/../new' : value),
      ...(process.platform === 'win32' ? [valid.map(value => value === f.request.profilePath ? '\\drive-relative' : value)] : []),
    ]) {
      assert.throws(() => parseUiProofArguments(args), { message: 'ui_proof_arguments_invalid' });
    }
    assert.equal(existsSync(f.request.profilePath), false);
  } finally { f.cleanup(); }
});

test('native helper parent admission binds the exact expected epoch without weakening normal runtime identity', () => {
  const f = fixture();
  try {
    const image = join(f.installation, 'ssearch-service.exe');
    writeFileSync(image, 'synthetic identity fixture; never executed');
    const session = '00000000-0000-0000-0000-000000000001';
    const challenge = '00000000-0000-0000-0000-000000000002';
    const parent = { pid: process.pid + 100, executable: realpathSync(process.execPath), created: '132000000000000001' };
    const reply = encodeBrokerFrame({ schema_version: 1, kind: 'profile', session_id: session, challenge,
      profile: f.parent, key: 'a'.repeat(64), service_version: '0.2.0', python_base: image, python_venv: image, parent });
    const expected = { image, version: '0.2.0' };
    assert.throws(() => decodeProfileIdentity(reply, session, challenge, expected), { message: 'service_identity_mismatch' });
    assert.deepEqual(decodeProfileIdentity(reply, session, challenge, { ...expected, parent }).parent, parent);
    assert.throws(() => decodeProfileIdentity(reply, session, challenge,
      { ...expected, parent: { ...parent, created: '132000000000000002' } }), { message: 'service_identity_mismatch' });
    assert.throws(() => decodeProfileIdentity(reply, session, 'different', { ...expected, parent }),
      { message: 'service_identity_mismatch' });
  } finally { f.cleanup(); }
});

// Reservation policy only. These bytes never represent an installed/runtime proof.
function syntheticReservation(f: ReturnType<typeof fixture>): ReturnType<typeof inspectUiProof> {
  mkdirSync(join(f.installation, 'service/_internal/models'), { recursive: true });
  writeFileSync(join(f.installation, 'service/ssearch-service.exe'), 'synthetic non-executable');
  const report: ProofReport = {
    schema_version: 1, source_commit: 'a'.repeat(40), scenario: 'load', proof_scope: 'packaged_hidden_offscreen',
    ok: false, reason_code: 'pending',
    hashes: { desktop_sha256: '0'.repeat(64), service_sha256: '0'.repeat(64), app_asar_sha256: '0'.repeat(64), product_metadata_sha256: '0'.repeat(64) },
    versions: { app: '0.2.0', core_expected: '0.2.0', service: null, electron: 'synthetic', node: 'synthetic', chrome: 'synthetic' },
    owner: null, measured_counts: { scope: 'authenticated_descriptors_and_retained_children', verified_authority_pids: [],
      live_retained_owner_children: 0, owner_starts: 0, broker_starts: 0, global_os_owners: null, global_models: null,
      global_pg_processes: null, read_only_profile_invocations: 0, service_method_calls: {} },
    checks: requiredUiChecks.load.map(name => ({ name, status: 'not_run' })), open_gates: ['synthetic_policy_test'],
  };
  return { paths: { profile: f.request.profilePath, report: f.request.reportPath }, report, root: f.installation,
    resources: join(f.installation, 'resources'), assetRoot: join(f.installation, 'resources/app.asar/renderer'),
    runtime: new PackagedRuntime({ installationRoot: f.installation, expectedServiceVersion: '0.2.0' }) };
}

test('fresh declared report is exclusively reserved and progress never implies unexecuted checks passed', () => {
  const f = fixture();
  try {
    const prepared = syntheticReservation(f);
    const reserved = reserveUiProof(prepared);
    try {
      const initial = JSON.parse(readFileSync(f.request.reportPath, 'utf8'));
      assert.equal(initial.ok, false);
      assert.ok(initial.checks.every((check: { status: string }) => check.status === 'not_run'));
      assert.equal(initial.owner, null);
      assert.equal(initial.versions.service, null);
      assert.equal(initial.measured_counts.global_models, null);
      reserved.report.checks[0].status = 'pass';
      reserved.writer.write();
      assert.equal(JSON.parse(readFileSync(f.request.reportPath, 'utf8')).ok, false);
      assert.throws(() => reserveUiProof(prepared));
    } finally { reserved.writer.close(); }
    assert.equal(existsSync(f.request.reportPath), true);
  } finally { f.cleanup(); }
});

test('a report appearing after admission is preserved and only our empty reservation is rolled back', () => {
  const f = fixture();
  try {
    f.request.reportPath = join(f.parent, 'explicit-output.json');
    const prepared = syntheticReservation(f);
    writeFileSync(f.request.reportPath, 'preserve raced output');
    assert.throws(() => reserveUiProof(prepared));
    assert.equal(readFileSync(f.request.reportPath, 'utf8'), 'preserve raced output');
    assert.equal(existsSync(f.request.profilePath), false);
  } finally { f.cleanup(); }
});

test('unpackaged execution cannot create a QA profile or report', () => {
  const f = fixture();
  try {
    assert.throws(() => inspectUiProof(f.request, { packaged: false, executable: process.execPath,
      resources: f.installation, appPath: f.installation, appVersion: '0.2.0', versions: { node: process.versions.node } }),
    { message: 'ui_proof_packaged_required' });
    assert.equal(existsSync(f.request.profilePath), false);
    assert.equal(existsSync(f.request.reportPath), false);
  } finally { f.cleanup(); }
});

test('proof success requires every selected real check and cleanup; unknown scope stays not_run', () => {
  const complete = requiredUiChecks.load.map(name => ({ name, status: 'pass' }));
  assert.equal(proofChecksPassed('load', [...complete, { name: 'global_model_count', status: 'not_run' }], true), true);
  assert.equal(proofChecksPassed('load', complete, false), false);
  assert.equal(proofChecksPassed('load', complete.filter(check => check.name !== 'clean_shutdown'), true), false);
  assert.equal(proofChecksPassed('load', complete.map(check => check.name === 'clean_shutdown'
    ? { ...check, status: 'not_run' } : check), true), false);
  assert.equal(proofChecksPassed('load', [...complete, { name: 'input_failure', status: 'fail' }], true), false);
  assert.equal(proofChecksPassed('load', [...complete, complete[0]], true), false);
});

test('existing, reparse, installation-owned and implicit output paths fail before writes', () => {
  const f = fixture();
  try {
    const existing = join(f.parent, 'existing');
    mkdirSync(existing);
    const occupiedReport = join(f.parent, 'occupied.json');
    writeFileSync(occupiedReport, 'preserve');
    for (const request of [
      { ...f.request, profilePath: existing }, { ...f.request, profilePath: 'relative-profile' },
      { ...f.request, reportPath: occupiedReport }, { ...f.request, reportPath: 'relative.json' },
      { ...f.request, reportPath: join(f.request.profilePath, 'missing-parent/report.json') },
      { ...f.request, profilePath: join(f.installation, 'new-profile') },
      { ...f.request, reportPath: join(f.installation, 'new-report.json') },
    ]) assert.throws(() => admitUiProofPaths(request, f.installation), { message: 'ui_proof_paths_invalid' });
    const linkedParent = join(f.parent, 'linked');
    symlinkSync(existing, linkedParent, 'junction');
    assert.throws(() => admitUiProofPaths({ ...f.request, profilePath: join(linkedParent, 'new') }, f.installation),
      { message: 'ui_proof_paths_invalid' });
    assert.equal(existsSync(f.request.profilePath), false);
  } finally { f.cleanup(); }
});
