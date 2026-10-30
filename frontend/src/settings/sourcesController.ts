import { SearchClientError } from '../bridge/client';
import { createSourcesDraft, reduceSourcesDraft, type SourcesDraft, type SourcesDraftAction, type SourcesSettingsPort } from './sourcesDraft';

export interface SourcesView {
  readonly draft: SourcesDraft | null;
  readonly phase: 'loading' | 'ready' | 'validating' | 'saving' | 'error';
  readonly error: SearchClientError | null;
  readonly needsReconcile: boolean;
}

/** UI session over the existing P2 settings port, with no persisted store. */
export class SourcesController {
  private view: SourcesView = { draft: null, phase: 'loading', error: null, needsReconcile: false };
  private listeners = new Set<() => void>();
  private closed = false;
  private epoch = 0;
  private pending: AbortController | null = null;

  constructor(private readonly client: SourcesSettingsPort) {}
  getSnapshot = (): SourcesView => this.view;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  private publish(view: SourcesView): void {
    if (this.closed) return;
    this.view = view;
    for (const listener of this.listeners) listener();
  }
  async load(): Promise<void> {
    if (this.closed) return;
    const epoch = ++this.epoch;
    this.pending?.abort();
    const pending = new AbortController();
    this.pending = pending;
    this.publish({ ...this.view, phase: 'loading', error: null });
    try {
      const snapshot = await this.client.settingsGet(pending.signal);
      if (this.closed || epoch !== this.epoch) return;
      this.publish({ draft: createSourcesDraft(snapshot), phase: 'ready', error: null, needsReconcile: false });
    } catch (error) {
      if (this.closed || epoch !== this.epoch) return;
      this.publish({ ...this.view, phase: 'error', error: error instanceof SearchClientError
        ? error : new SearchClientError('service_unavailable') });
    }
  }
  dispatch(action: SourcesDraftAction): void {
    if (this.closed || !this.view.draft || this.view.phase === 'saving') return;
    this.publish({ ...this.view, draft: reduceSourcesDraft(this.view.draft, action) });
  }
  // Fail-closed async contract scaffold for assertion RED. No fake validation,
  // apply, retry, settings publication or native action is performed here.
  async validate(): Promise<void> {}
  async save(): Promise<void> {}
  async reload(): Promise<void> {}
  close(): void {
    this.closed = true;
    this.epoch++;
    this.pending?.abort();
    this.listeners.clear();
  }
}
