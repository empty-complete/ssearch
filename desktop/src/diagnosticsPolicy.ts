import { lstatSync, realpathSync } from 'node:fs';
import { dirname, extname, isAbsolute, normalize, parse, relative, resolve, sep } from 'node:path';

export const uiScenarios = ['load', 'security', 'lifecycle', 'empty-profile'] as const;
export type UiScenario = typeof uiScenarios[number];
export interface UiProofRequest {
  scenario: UiScenario;
  profilePath: string;
  reportPath: string;
  helper: boolean;
  helperRole?: 'second-instance' | 'orphan';
}
export interface UiProofPaths { profile: string; report: string }

export const requiredUiChecks = {
  load: ['installed_bootstrap_loaded', 'sandboxed_named_bridge', 'unavailable_without_service', 'no_service_start', 'clean_shutdown'],
  security: ['installed_renderer_loaded', 'sandboxed_named_bridge', 'real_csp_and_interception',
    'navigation_windows_permissions_downloads_denied', 'real_sender_frame_origin_denials', 'clean_shutdown'],
  lifecycle: ['two_views_one_verified_owner', 'second_instance_without_service_start', 'attached_release_safe',
    'renderer_crash_query_and_stale_response', 'lost_mutation_reply_no_retry', 'idle_owner_crash_recovery',
    'close_is_not_quit', 'parent_orphan_graceful_stop_and_restart', 'clean_shutdown'],
  'empty-profile': ['fresh_sources_empty_and_jobs_zero', 'real_settings_revision_apply',
    'restart_preserves_empty_sources_and_revision', 'clean_shutdown'],
} as const;
export const uiProofBudgets = { load: 30_000, security: 120_000, lifecycle: 600_000, 'empty-profile': 240_000 } as const;
export function proofChecksPassed(scenario: UiScenario, checks: Array<{ name: string; status: string }>, completed: boolean): boolean {
  return completed && requiredUiChecks[scenario].every(name => checks.filter(check => check.name === name && check.status === 'pass').length === 1)
    && checks.every(check => ['pass', 'not_run'].includes(check.status)) && new Set(checks.map(check => check.name)).size === checks.length;
}

export function parseUiProofArguments(arguments_: string[]): UiProofRequest | null {
  if (!arguments_.some(value => value.startsWith('--ui-'))) return null;
  const values = new Map<string, string>();
  let helper = false;
  let helperRole: UiProofRequest['helperRole'];
  for (let index = 0; index < arguments_.length; index++) {
    const flag = arguments_[index];
    if (flag === '--ui-proof-helper') {
      if (helper) throw new Error('ui_proof_arguments_invalid');
      const role = arguments_[++index];
      if (role !== 'second-instance' && role !== 'orphan') throw new Error('ui_proof_arguments_invalid');
      helper = true;
      helperRole = role;
      continue;
    }
    if (!['--ui-self-test', '--ui-scenario', '--data-dir'].includes(flag) || values.has(flag)
      || !arguments_[index + 1] || arguments_[index + 1].startsWith('--')) throw new Error('ui_proof_arguments_invalid');
    values.set(flag, arguments_[++index]);
  }
  const scenario = values.get('--ui-scenario');
  if (values.size !== 3 || !uiScenarios.includes(scenario as UiScenario)
    || (helper && scenario !== 'lifecycle')) throw new Error('ui_proof_arguments_invalid');
  try { safeAbsolute(values.get('--data-dir')!); safeAbsolute(values.get('--ui-self-test')!); }
  catch { throw new Error('ui_proof_arguments_invalid'); }
  return { scenario: scenario as UiScenario, profilePath: values.get('--data-dir')!,
    reportPath: values.get('--ui-self-test')!, helper, ...(helperRole ? { helperRole } : {}) };
}

export function absent(path: string): boolean {
  try { lstatSync(path); return false; } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return true; throw error; }
}

export function canonicalDirectory(path: string): string {
  const canonical = realpathSync(path);
  if (canonical.toLowerCase() !== resolve(path).toLowerCase()) throw new Error('ui_proof_paths_invalid');
  for (let cursor = canonical; ; cursor = dirname(cursor)) {
    const stat = lstatSync(cursor);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('ui_proof_paths_invalid');
    if (dirname(cursor) === cursor) break;
  }
  return canonical;
}

export function contained(root: string, target: string): boolean {
  const value = relative(root, target);
  return value === '' || (!isAbsolute(value) && value !== '..' && !value.startsWith(`..${sep}`));
}

function safeAbsolute(path: string): string {
  if (!isAbsolute(path) || path.startsWith('\\\\') || path.startsWith('//')
    || (process.platform === 'win32' && !/^[a-z]:[\\/]/i.test(path))
    || path.split(/[\\/]/).some(component => component === '.' || component === '..')) throw new Error();
  const result = resolve(normalize(path));
  for (const component of result.slice(parse(result).root.length).split(/[\\/]/)) {
    if (!component || /[<>:"|?*\u0000-\u001f]/.test(component) || /[. ]$/.test(component)
      || /^(?:con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/i.test(component)) throw new Error();
  }
  return result;
}

export function admitUiProofPaths(request: UiProofRequest, installationRoot: string): UiProofPaths {
  try {
    const profile = safeAbsolute(request.profilePath);
    const report = safeAbsolute(request.reportPath);
    if (!absent(profile) || !absent(report) || profile.toLowerCase() === report.toLowerCase()
      || extname(report).toLowerCase() !== '.json' || contained(installationRoot, profile)
      || contained(installationRoot, report)) throw new Error();
    canonicalDirectory(dirname(profile));
    if (dirname(report).toLowerCase() !== profile.toLowerCase()) canonicalDirectory(dirname(report));
    return { profile, report };
  } catch { throw new Error('ui_proof_paths_invalid'); }
}
