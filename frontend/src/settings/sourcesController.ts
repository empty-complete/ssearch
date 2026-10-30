import { SearchClientError } from '../bridge/client';
import type { JsonObject } from '../bridge/types';
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
  private validated: { generation: number; revision: number } | null = null;

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
    if (this.closed || this.view.draft) return;
    await this.read(false);
  }
  private begin(): { epoch: number; signal: AbortSignal } {
    const epoch = ++this.epoch;
    this.pending?.abort();
    this.pending = new AbortController();
    return { epoch, signal: this.pending.signal };
  }
  private current(epoch: number, draft?: SourcesDraft): boolean {
    const current = this.view.draft;
    return !this.closed && epoch === this.epoch && (!draft || (current !== null && current.generation === draft.generation
      && current.baseline.revision === draft.baseline.revision));
  }
  private candidate(settings: JsonObject, draft: SourcesDraft): JsonObject {
    return createSourcesDraft({ ...draft.baseline, settings }).candidate;
  }
  private error(error: unknown, mutation = false): SearchClientError {
    return error instanceof SearchClientError ? error
      : new SearchClientError('service_unavailable', mutation ? { outcome: 'unknown' } : {});
  }
  private writable(draft: SourcesDraft): boolean {
    return draft.baseline.mutable_fields.includes('sources') && Array.isArray(draft.candidate.sources);
  }
  private async read(keepSources: boolean): Promise<void> {
    if (this.closed || this.view.phase === 'saving') return;
    const staged = this.view.draft;
    const { epoch, signal } = this.begin();
    this.publish({ ...this.view, phase: 'loading', error: null });
    try {
      const snapshot = await this.client.settingsGet(signal);
      if (!this.current(epoch)) return;
      let draft = createSourcesDraft(snapshot);
      if (keepSources && staged) {
        if (!snapshot.mutable_fields.includes('sources')) throw new SearchClientError('access_denied');
        const sourceCopy = this.candidate(staged.candidate, staged);
        draft = { ...draft, candidate: { ...draft.candidate, sources: sourceCopy.sources }, generation: staged.generation + 1 };
      }
      this.validated = null;
      this.publish({ draft, phase: 'ready', error: null, needsReconcile: false });
    } catch (error) {
      if (!this.current(epoch)) return;
      this.publish({ ...this.view, phase: 'error', error: this.error(error) });
    }
  }
  dispatch(action: SourcesDraftAction): void {
    if (this.closed || !this.view.draft || this.view.phase === 'saving') return;
    const draft = reduceSourcesDraft(this.view.draft, action);
    if (draft === this.view.draft) return;
    this.epoch++;
    this.pending?.abort();
    this.validated = null;
    this.publish({ ...this.view, draft, phase: 'ready', error: this.view.needsReconcile ? this.view.error : null });
  }
  async validate(): Promise<void> {
    const draft = this.view.draft;
    if (this.closed || !draft || !this.writable(draft) || this.view.needsReconcile
      || ['loading', 'saving'].includes(this.view.phase)) return;
    const { epoch, signal } = this.begin();
    this.validated = null;
    this.publish({ ...this.view, draft: { ...draft, validation: null }, phase: 'validating', error: null });
    try {
      const validation = await this.client.settingsValidate({ candidate: this.candidate(draft.candidate, draft),
        expected_revision: draft.baseline.revision }, signal);
      if (!this.current(epoch, draft)) return;
      if (validation.expected_revision !== draft.baseline.revision
        || (validation.valid && (!validation.normalized || validation.errors.length !== 0
          || validation.normalized.schema_version !== draft.baseline.schema_version
          || validation.normalized.revision !== draft.baseline.revision + 1))) {
        throw new SearchClientError('invalid_service_response');
      }
      this.validated = validation.valid ? { generation: draft.generation, revision: draft.baseline.revision } : null;
      this.publish({ ...this.view, draft: { ...draft, validation: { ...validation,
        normalized: validation.normalized ? this.candidate(validation.normalized, draft) : null,
        errors: validation.errors.map(error => ({ ...error })), warnings: [...validation.warnings],
      } }, phase: 'ready', error: null });
    } catch (error) {
      if (!this.current(epoch, draft)) return;
      const failure = this.error(error);
      this.publish({ ...this.view, phase: 'error', error: failure,
        needsReconcile: failure.code === 'revision_conflict' || failure.code === 'settings_recovery_required' });
    }
  }
  async save(): Promise<void> {
    const draft = this.view.draft;
    const validation = draft?.validation;
    if (this.closed || !draft || !this.writable(draft) || this.view.needsReconcile || this.view.phase !== 'ready'
      || !validation?.valid || !validation.normalized || this.validated?.generation !== draft.generation
      || this.validated.revision !== draft.baseline.revision) return;
    const { epoch, signal } = this.begin();
    this.validated = null; // One explicit dispatch consumes this validation, including failures.
    this.publish({ ...this.view, phase: 'saving', error: null });
    try {
      const saved = await this.client.settingsApply({ candidate: this.candidate(validation.normalized, draft),
        expected_revision: draft.baseline.revision }, signal);
      if (!this.current(epoch, draft)) return;
      if (saved.schema_version !== draft.baseline.schema_version || saved.revision !== draft.baseline.revision + 1
        || saved.settings.revision !== saved.revision) throw new SearchClientError('invalid_service_response', { outcome: 'unknown' });
      this.publish({ draft: createSourcesDraft(saved), phase: 'ready', error: null, needsReconcile: false });
    } catch (error) {
      if (!this.current(epoch, draft)) return;
      this.publish({ ...this.view, draft: { ...draft, validation: null }, phase: 'error',
        error: this.error(error, true), needsReconcile: true });
    }
  }
  /** Explicit reconciliation only. Preserve the source decision, never replay
   * an apply or merge stale unrelated settings over the newly read full JSON. */
  async reload(): Promise<void> { await this.read(true); }
  close(): void {
    this.closed = true;
    this.epoch++;
    this.pending?.abort();
    this.listeners.clear();
  }
}
