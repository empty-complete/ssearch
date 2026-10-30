import type { BrowserWindow } from 'electron';
import type { AccessCheck, FolderAccessRequest, FolderSelection, SettingsSnapshot } from './contracts.js';

export interface FolderDialog {
  showOpenDialog(parent: BrowserWindow, options: { properties: ['openDirectory'] }): Promise<{ canceled: boolean; filePaths: string[] }>;
}
export interface FolderBackend {
  selectSourceIdentity(path: string, signal: AbortSignal): Promise<string>;
  checkFolder(input: { selected_path: string } | { source_id: string }, signal: AbortSignal): Promise<AccessCheck>;
}

/** Unwired MAIN policy contract. Neither a picker nor access proof is simulated
 * by this fail-closed assertion-RED scaffold. No runtime Electron import. */
export class FolderPicker {
  constructor(private readonly parent: BrowserWindow, private readonly dialog: FolderDialog,
    private readonly backend: FolderBackend, private readonly now: () => number = Date.now) {}
  async pickFolder(_signal: AbortSignal): Promise<FolderSelection | null> { return null; }
  async checkFolderAccess(_input: FolderAccessRequest, _signal: AbortSignal): Promise<AccessCheck> {
    throw new Error('access_denied');
  }
  recordSavedSources(_snapshot: SettingsSnapshot): void {}
  reset(): void {}
  close(): void {}
}
