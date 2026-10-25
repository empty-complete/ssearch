import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, rmSync, realpathSync, symlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { parseUiProofArguments, admitUiProofPaths, type UiProofRequest } from '../src/diagnosticsPolicy.js';

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
      valid.map(value => value === 'load' ? 'unknown' : value), [...valid, '--ui-self-test', f.request.reportPath]]) {
      assert.throws(() => parseUiProofArguments(args), { message: 'ui_proof_arguments_invalid' });
    }
    assert.equal(existsSync(f.request.profilePath), false);
  } finally { f.cleanup(); }
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
