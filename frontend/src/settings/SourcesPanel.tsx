import { Button } from '@heroui/react';
import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import type { JsonValue } from '../bridge/types';
import type { SearchClientError } from '../bridge/client';
import { SourcesController } from './sourcesController';
import { SourceRow } from './SourceRow';
import type { SourceDraftRow, SourcesSettingsPort } from './sourcesDraft';
import './settings.css';

export interface SourcesPanelProps {
  client: SourcesSettingsPort;
  onClose: () => void;
}

function sourceRows(value: JsonValue | undefined): SourceDraftRow[] | null {
  if (!Array.isArray(value) || value.length > 64) return null;
  const rows: SourceDraftRow[] = [];
  for (const row of value) {
    if (!row || typeof row !== 'object' || Array.isArray(row) || typeof row.id !== 'string'
      || typeof row.path !== 'string' || typeof row.label !== 'string' || typeof row.enabled !== 'boolean') return null;
    rows.push({ id: row.id, path: row.path, label: row.label, enabled: row.enabled });
  }
  return rows;
}

function errorText(error: SearchClientError): string {
  if (error.details.outcome === 'unknown') return 'Неизвестно, сохранены ли изменения. Обновите сохранённые настройки перед новой попыткой.';
  if (error.code === 'revision_conflict') return 'Настройки изменены другим окном. Обновите сохранённые настройки и проверьте свой черновик.';
  if (error.code === 'access_denied') return 'В этом подключении нельзя изменять настройки.';
  if (error.code === 'settings_recovery_required') return 'Настройки требуют восстановления сервисом. Черновик сохранён в этом окне.';
  if (error.code === 'settings_apply_failed') return 'Не удалось применить изменения. Обновите сохранённые настройки перед новой попыткой.';
  if (error.code === 'settings_busy') return 'Сервис занят изменением настроек. Повторите проверку позже.';
  return 'Настройки сейчас недоступны. Черновик остаётся в этом окне.';
}

export function SourcesPanel({ client, onClose }: SourcesPanelProps) {
  const controller = useMemo(() => new SourcesController(client), [client]);
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { void controller.load(); return () => controller.close(); }, [controller]);
  useEffect(() => { heading.current?.focus(); }, []);
  const draft = view.draft;
  const rows = draft ? sourceRows(draft.candidate.sources) : null;
  const readonly = !!draft && !draft.baseline.mutable_fields.includes('sources');
  const saving = view.phase === 'saving';
  const busy = saving || view.phase === 'loading';
  const dirty = !!draft && JSON.stringify(draft.candidate.sources) !== JSON.stringify(draft.baseline.settings.sources);
  const editable = !!draft && rows !== null && !readonly && !busy;
  const validated = !!draft?.validation?.valid && !!draft.validation.normalized;

  return (
    <section className="sources-panel" aria-label="Источники" onKeyDown={event => {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); }
    }}>
      <div className="sources-heading">
        <h1 tabIndex={-1} ref={heading}>Источники</h1>
        <Button variant="ghost" onPress={onClose}>{saving ? 'Закрыть' : 'Отменить и закрыть'}</Button>
      </div>
      <p className="sources-description">Выберите папки для поиска. Изменения вступят в силу после сохранения.</p>
      {view.phase === 'loading' && <div role="status" aria-label="Загрузка источников">
        <div className="skeleton" /><div className="skeleton short" />
      </div>}
      {view.error && <p role="alert" className="sources-error">{errorText(view.error)}</p>}
      {saving && <p role="status" className="sources-description">Сохранение выполняется. Закрытие панели не отменит уже начатое сохранение.</p>}
      {readonly && <p className="sources-description">Настройки доступны только для чтения в этом подключении.</p>}
      {draft && rows === null && <p role="alert" className="sources-error">Не удалось прочитать список папок. Настройки не изменены.</p>}
      {rows?.length === 0 && <p>Папки ещё не выбраны. Добавьте папку, в которой нужно искать документы.</p>}
      {rows && <ul className="sources-list" aria-label="Папки для поиска">
        {rows.map((source, index) => <SourceRow key={`${source.id}-${index}`} source={source} editable={editable}
          onEdit={changes => controller.dispatch({ type: 'edit', id: source.id, changes })}
          onRemove={() => controller.dispatch({ type: 'remove', id: source.id })} />)}
      </ul>}
      <div className="sources-add">
        <Button variant="secondary" isDisabled>Добавить папку</Button>
        <p className="sources-description">Выбор папки в этом подключении недоступен.</p>
      </div>
      <div aria-live="polite" className="sources-validation">
        {draft?.validation?.errors.map((error, index) => <p role="alert" className="sources-error" key={`${error.path}-${index}`}>{error.message}</p>)}
        {draft?.validation?.warnings.map((warning, index) => <p className="persistent-warning" key={`${warning}-${index}`}>
          {warning === 'source_roots_overlap' ? 'Некоторые папки вложены друг в друга. Их содержимое может пересекаться.'
            : 'Сервис вернул предупреждение настроек. Проверьте черновик перед сохранением.'}
        </p>)}
        {validated && <p>Изменения проверены. Сохраните их, чтобы применить.</p>}
      </div>
      <div className="sources-footer">
        <Button variant="secondary" isDisabled={!editable || !dirty || view.phase === 'validating' || view.needsReconcile}
          onPress={() => { void controller.validate(); }}>{view.phase === 'validating' ? 'Проверка…' : 'Проверить изменения'}</Button>
        <Button variant="primary" isDisabled={!editable || !dirty || !validated || view.phase !== 'ready' || view.needsReconcile}
          onPress={() => { void controller.save(); }}>{saving ? 'Сохранение…' : 'Сохранить'}</Button>
        <Button variant="ghost" isDisabled={!draft || busy} onPress={() => controller.dispatch({ type: 'discard' })}>Отменить изменения</Button>
        {(view.needsReconcile || (!draft && view.error)) && <Button variant="secondary" isDisabled={busy}
          onPress={() => { void (draft ? controller.reload() : controller.load()); }}>Обновить сохранённые настройки</Button>}
      </div>
    </section>
  );
}
