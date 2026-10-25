export const uiScenarios = ['load', 'security', 'lifecycle', 'empty-profile'] as const;
export type UiScenario = typeof uiScenarios[number];
export interface UiProofRequest {
  scenario: UiScenario;
  profilePath: string;
  reportPath: string;
  helper: boolean;
}
export interface UiProofPaths { profile: string; report: string }

export function parseUiProofArguments(_arguments: string[]): UiProofRequest | null {
  throw new Error('ui_proof_arguments_invalid');
}
export function admitUiProofPaths(_request: UiProofRequest, _installationRoot: string): UiProofPaths {
  throw new Error('ui_proof_paths_invalid');
}
