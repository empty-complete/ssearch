import type { SourcesSettingsPort } from './sourcesDraft';

export interface SourcesPanelProps {
  client: SourcesSettingsPort;
  onClose: () => void;
}

/** Unwired rendering contract for browser assertion RED. No settings or native
 * operation is simulated while the actual controller/panel is implemented. */
export function SourcesPanel(_props: SourcesPanelProps) {
  return <section aria-label="Источники"><h1>Источники</h1></section>;
}
