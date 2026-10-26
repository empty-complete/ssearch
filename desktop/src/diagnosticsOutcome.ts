import { auditFailureCategories, type AuditFailureCategory } from './diagnosticsWire.js';
import { requiredUiChecks } from './diagnosticsPolicy.js';

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

const reasons = new Set<string>([...Object.values(requiredUiChecks).flat(), 'ui_proof_runtime_failed',
  'ui_proof_wire_audit_incomplete', 'ui_proof_cleanup_failed', 'unhandled_runtime_failure',
  'ui_proof_deadline', 'ui_proof_report_write_failed']);

/** Only known public codes are compared. Error text, stack, paths and arbitrary
 * exception properties never become receipt values. */
export function classifyProofError(error: unknown): ProofFailureCategory {
  try {
    if (!(error instanceof Error)) return 'runtime_failure';
    if ((error as Error & { code?: unknown }).code === 'ERR_ASSERTION') return 'assertion';
    if (['ui_proof_deadline', 'ui_proof_case_deadline', 'ui_proof_cleanup_deadline'].includes(error.message)) return 'deadline';
    if (error.message === 'ui_proof_cancelled') return 'cancelled';
    if (error.message === 'service_unavailable') return 'service_unavailable';
    if (error.message === 'ui_proof_report_write_failed') return 'report_write';
    if (['ui_proof_wire_audit_incomplete', 'ui_proof_mutation_dispatch_not_once'].includes(error.message)) return 'wire_audit';
    return 'runtime_failure';
  } catch { return 'runtime_failure'; }
}

/** First failure is immutable. Secondary audit/cleanup refusals remain visible
 * and continue to block success without replacing the original observation. */
export function recordProofFailure(report: ProofOutcome, reason: string, observation?: FailureObservation): void {
  report.ok = false;
  if (!report.failure_diagnostics) {
    report.reason_code = reasons.has(reason) ? reason : 'ui_proof_runtime_failed';
    report.failure_diagnostics = { primary_phase: observation?.phase ?? 'action',
      primary_category: observation?.category ?? 'runtime_failure', wire_audit_category: null, cleanup_category: null };
  }
  const details = report.failure_diagnostics;
  if (details.wire_audit_category === null && observation?.wireCategory
    && auditFailureCategories.includes(observation.wireCategory)) details.wire_audit_category = observation.wireCategory;
  if (details.cleanup_category === null && observation?.cleanupCategory) details.cleanup_category = observation.cleanupCategory;
}
