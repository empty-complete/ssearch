import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyProofError, recordProofFailure, type ProofOutcome } from '../src/diagnosticsOutcome.js';

test('actual runner failure arbiter preserves the primary action failure through audit and cleanup failures', () => {
  const report: ProofOutcome = { ok: false, reason_code: 'pending' };
  recordProofFailure(report, 'installed_renderer_loaded', { phase: 'action', category: 'renderer_load' });
  recordProofFailure(report, 'ui_proof_wire_audit_incomplete', {
    phase: 'audit_after_action', category: 'wire_audit', wireCategory: 'end_payload_empty',
  });
  recordProofFailure(report, 'ui_proof_cleanup_failed', {
    phase: 'cleanup', category: 'assertion', cleanupCategory: 'assertion',
  });
  recordProofFailure(report, 'ui_proof_wire_audit_incomplete', {
    phase: 'final_report', category: 'wire_audit', wireCategory: 'end_payload_empty',
  });
  assert.equal(report.reason_code, 'installed_renderer_loaded');
  assert.equal(report.ok, false);
  assert.deepEqual(report.failure_diagnostics, { primary_phase: 'action', primary_category: 'renderer_load',
    wire_audit_category: 'end_payload_empty', cleanup_category: 'assertion' });
});

test('audit refusal before the action is primary even when abort and cleanup subsequently fail', () => {
  const report: ProofOutcome = { ok: true, reason_code: 'pending' };
  recordProofFailure(report, 'ui_proof_wire_audit_incomplete', {
    phase: 'audit_before_action', category: 'wire_audit', wireCategory: 'unsupported_chunk',
  });
  recordProofFailure(report, 'installed_renderer_loaded', { phase: 'action', category: 'cancelled' });
  recordProofFailure(report, 'ui_proof_cleanup_failed', {
    phase: 'cleanup', category: 'wire_audit', wireCategory: 'unsupported_chunk', cleanupCategory: 'wire_audit',
  });
  assert.equal(report.reason_code, 'ui_proof_wire_audit_incomplete');
  assert.equal(report.ok, false);
  assert.deepEqual(report.failure_diagnostics, { primary_phase: 'audit_before_action', primary_category: 'wire_audit',
    wire_audit_category: 'unsupported_chunk', cleanup_category: 'wire_audit' });
});

test('successful output acquires no optional failure-only fields', () => {
  const report: ProofOutcome = { ok: true, reason_code: 'passed' };
  assert.deepEqual(Object.keys(report), ['ok', 'reason_code']);
});

test('opaque exceptions and unknown reason codes cannot disclose error text or private paths', () => {
  const sentinel = 'PRIVATE_SENTINEL_CREDENTIAL_C:\\private-profile';
  const opaque = new Error(sentinel);
  assert.equal(classifyProofError(opaque), 'runtime_failure');
  const report: ProofOutcome = { ok: true, reason_code: 'pending' };
  recordProofFailure(report, sentinel, { phase: 'action', category: classifyProofError(opaque) });
  assert.equal(report.reason_code, 'ui_proof_runtime_failed');
  assert.equal(JSON.stringify(report).includes(sentinel), false);
  assert.equal(classifyProofError({ message: sentinel, stack: sentinel }), 'runtime_failure');
  assert.equal(classifyProofError(new Error('ui_proof_cancelled')), 'cancelled');
  assert.equal(classifyProofError(new Error('ui_proof_cleanup_deadline')), 'deadline');
  assert.equal(classifyProofError(new Error('service_unavailable')), 'service_unavailable');
});
