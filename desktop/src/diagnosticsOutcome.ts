import type { AuditFailureCategory } from './diagnosticsWire.js';

export type ProofFailurePhase = 'bootstrap' | 'action' | 'audit_before_action' | 'audit_after_action'
  | 'cleanup' | 'final_report' | 'unhandled' | 'deadline';
export type ProofFailureCategory = 'wire_audit' | 'renderer_load' | 'assertion' | 'deadline'
  | 'cancelled' | 'service_unavailable' | 'runtime_failure' | 'report_write' | 'retained_child' | 'owner_state';
export interface FailureDiagnostics {
  primary_phase: ProofFailurePhase;
  primary_category: ProofFailureCategory;
  wire_audit_category: AuditFailureCategory | null;
  cleanup_category: ProofFailureCategory | null;
}
export interface ProofOutcome {
  ok: boolean;
  reason_code: string;
  failure_diagnostics?: FailureDiagnostics;
}
export interface FailureObservation {
  phase: ProofFailurePhase;
  category: ProofFailureCategory;
  wireCategory?: AuditFailureCategory;
  cleanupCategory?: ProofFailureCategory;
}

/** Existing failure assignment extracted for the packaged runner's actual
 * update/outer/cleanup paths. First-failure arbitration is pending its RED. */
export function recordProofFailure(report: ProofOutcome, reason: string, _observation?: FailureObservation): void {
  report.ok = false;
  report.reason_code = reason;
}
