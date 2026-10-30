import { Button } from '@heroui/react';
import { useRef, useState } from 'react';
import { SearchBox } from '../components/SearchBox';
import { QueryChoices } from './QueryChoices';
import { SearchResults } from './SearchResults';
import { useSearchState, type RevealAction, type SearchPageMode, type SearchPort } from './searchState';

export interface SearchPageProps {
  client: SearchPort;
  scope: string[] | null;
  mode: SearchPageMode;
  onReveal?: RevealAction;
  initialQuery?: string;
  onQueryChange?: (query: string) => void;
}

export function SearchPage({ client, scope, mode, onReveal, initialQuery = '', onQueryChange }: SearchPageProps) {
  const search = useSearchState(client, scope, initialQuery);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const composing = useRef(false);
  const compositionActivation = useRef(false);
  const revealInFlight = useRef(false);
  const [notice, setNotice] = useState('');
  const [revealing, setRevealing] = useState(false);

  const activate = async (id: string) => {
    if (composing.current || compositionActivation.current || !onReveal || revealInFlight.current) return;
    if (search.controller.getSnapshot().generation !== search.generation) return;
    if (!search.revealCandidate(id)) return;
    const generation = search.generation;
    revealInFlight.current = true;
    setRevealing(true);
    try {
      const outcome = await onReveal(id);
      if (search.controller.getSnapshot().generation !== generation) return;
      setNotice(outcome.status === 'revealed' ? 'Файл показан в Проводнике.'
        : outcome.status === 'unavailable' ? 'Показ в Проводнике пока недоступен.'
          : 'Не удалось показать файл. Повторите попытку.');
    } catch {
      if (search.controller.getSnapshot().generation === generation) {
        setNotice('Не удалось показать файл. Повторите попытку.');
      }
    } finally { revealInFlight.current = false; setRevealing(false); }
  };

  const submit = () => {
    if (composing.current || compositionActivation.current) return;
    if (search.state === 'ready' && search.selectedId) void activate(search.selectedId);
    else void search.submit();
  };
  const navigate = (edge: 'first' | 'last') => {
    if (search.state !== 'ready' || !search.results.length) return false;
    const index = edge === 'first' ? 0 : search.results.length - 1;
    search.select(search.results[index].result_id);
    const row = list.current?.querySelectorAll<HTMLElement>('[role="option"]')[index];
    row?.focus();
    row?.scrollIntoView({ block: 'nearest' });
    return true;
  };

  return (
    <div className="search-page" data-search-mode={mode}
      onCompositionStartCapture={() => { composing.current = true; }}
      onCompositionEndCapture={() => { composing.current = false; }}
      onPointerDownCapture={() => { compositionActivation.current = false; }}
      onKeyDownCapture={event => {
        compositionActivation.current = event.key === 'Enter'
          && (composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229);
      }}>
      <section className="search-area" aria-label={mode === 'onboarding' ? 'Учебный поиск' : 'Поиск документов'}>
        <SearchBox inputRef={input} value={search.query} onNavigate={navigate} onSubmit={submit}
          onChange={value => { search.setQuery(value); onQueryChange?.(value); setNotice(''); }} />
        <div className="search-context">
          <span>{scope === null ? 'Все выбранные источники'
            : scope.length === 0 ? 'Источники не выбраны' : `Выбрано источников: ${scope.length}`}</span>
          <Button variant="secondary" size="sm" isDisabled={!search.query.trim()}
            onPress={() => { void search.submit(); }}>Найти</Button>
        </div>
      </section>
      <QueryChoices ambiguities={search.ambiguities} selected={search.project} disabled={search.loading}
        onChoose={id => { void search.chooseProject(id); input.current?.focus(); setNotice(''); }} />
      <section className="result-area" aria-label="Результаты поиска" aria-busy={search.loading}>
        <div role="status" aria-live="polite" className="search-status">
          {search.state === 'idle' && <p>Введите название, объект или часть пути. Нажмите Enter для поиска.</p>}
          {search.loading && <><p>Ищем документы…</p><div className="skeleton" aria-hidden="true" /><div className="skeleton short" aria-hidden="true" /></>}
          {search.state === 'empty' && <p>{scope?.length === 0
            ? 'Добавьте папку для поиска. Запрос сохранён.'
            : 'Документы не найдены. Попробуйте другое название или часть пути.'}</p>}
          {search.state === 'ready' && <p>Найдено: {search.results.length}. Стрелки — выбрать документ.</p>}
        </div>
        {search.error && <div className="connection-error" role="alert">
          <p>{search.error === 'access_denied' ? 'Нет доступа к поиску в выбранных источниках. Запрос сохранён.'
            : 'Локальный поиск недоступен. Запрос сохранён.'}</p>
          <Button variant="secondary" onPress={() => { void search.submit(); }}>Повторить соединение</Button>
        </div>}
        {!!search.response?.warnings.length && <p className="persistent-warning" role="status">
          Результаты могут быть неполными. Часть источников или способов поиска недоступна.
        </p>}
        {!!search.results.length && <SearchResults results={search.results} query={search.query}
          selectedId={search.selectedId} loading={search.loading} listRef={list}
          onSelect={search.select} onActivate={id => { void activate(id); }} />}
      </section>
      <div className="search-actions">
        {search.selectedId && <Button variant="secondary" className="reveal-action"
          isDisabled={!onReveal || search.state !== 'ready' || revealing}
          onPress={() => { if (search.selectedId) void activate(search.selectedId); }}>Показать в Проводнике</Button>}
        {!onReveal && <p className="search-status">Показ в Проводнике пока недоступен.</p>}
        {notice && <p role="status">{notice}</p>}
      </div>
    </div>
  );
}
