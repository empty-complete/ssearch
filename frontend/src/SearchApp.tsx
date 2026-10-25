import { Button } from '@heroui/react';
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { AppShell, type ShellMode, type Theme } from './AppShell';
import { IconAction } from './components/IconAction';
import { SearchBox } from './components/SearchBox';
import { ResultList } from './components/ResultList';
import { SearchController } from './searchController';
import type { SearchService } from './bridge/types';
import license from '../../LICENSE.txt?raw';
import notices from '../THIRD_PARTY_NOTICES.txt?raw';
import { builtinExtensions } from '../../desktop/src/extensions/registry';

interface SearchAppProps {
  service: SearchService;
  preview?: boolean;
  mode?: ShellMode;
}

export function SearchApp({ service, preview = false, mode = 'application' }: SearchAppProps) {
  const controller = useMemo(() => new SearchController(service), [service]);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const [theme, setTheme] = useState<Theme>('system');
  const [notice, setNotice] = useState('');
  const [about, setAbout] = useState(false);
  const response = state.response;
  const devPreview = import.meta.env.DEV && preview;
  useEffect(() => () => controller.dispose(), [controller]);

  const submit = () => { void controller.submit(); };
  const reveal = (id: string) => {
    if (!controller.revealCandidate(id)) return;
    setNotice(devPreview
      ? 'Предпросмотр: Проводник не открывается. Полный путь указан в выдаче.'
      : 'Показ в Проводнике будет доступен после подключения приложения.');
  };

  return (
    <AppShell theme={theme} mode={mode}>
      <header className="app-header">
        <div className="wordmark">
          <img src="./app-icon.png" width="24" height="24" alt="" />
          <span>{__PRODUCT__.product_name}</span>
        </div>
        <IconAction
          label="О программе"
          onPress={() => setAbout(value => !value)}
          icon={
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 11v6M12 7v1" />
            </svg>
          }
        />
      </header>
      <section className="search-area" aria-label="Поиск документов">
        <SearchBox
          value={state.query}
          onChange={value => { controller.edit(value); setNotice(''); }}
          onSubmit={submit}
        />
        <div className="search-context">
          <span>Все выбранные источники</span>
          <Button variant="secondary" size="sm" onPress={submit} isDisabled={!state.query.trim()}>
            Найти
          </Button>
        </div>
      </section>
      {devPreview && <p className="preview-label">Учебные данные. Предпросмотр интерфейса.</p>}
      <section className="result-area" aria-label="Результаты поиска" aria-busy={state.state === 'loading'}>
        <div role="status" aria-live="polite" className="search-status">
          {state.state === 'idle' && (
            <p>Введите название, объект или часть пути. Нажмите Enter для поиска.</p>
          )}
          {state.state === 'loading' && (
            <>
              <p>Ищем документы…</p>
              <div className="skeleton" aria-hidden="true" />
              <div className="skeleton short" aria-hidden="true" />
            </>
          )}
          {state.state === 'empty' && (
            <p>Документы не найдены. Попробуйте другое название или часть пути.</p>
          )}
          {state.state === 'ready' && (
            <p>Найдено: {response?.results.length}. Выберите документ; Enter — показать в Проводнике.</p>
          )}
        </div>
        {state.state === 'error' && (
          <div className="connection-error" role="alert">
            <p>Локальный поиск недоступен. Запрос сохранён.</p>
            <Button variant="secondary" onPress={submit}>Повторить соединение</Button>
          </div>
        )}
        {!!response?.warnings.length && (
          <p className="persistent-warning" role="status">
            Результаты могут быть неполными. Часть источников или способов поиска недоступна.
          </p>
        )}
        {!!response?.ambiguities.length && (
          <p className="persistent-warning">
            В запросе несколько проектов с похожими названиями. Уточните название проекта или часть пути.
          </p>
        )}
        {state.state === 'ready' && response && (
          <ResultList results={response.results} selectedId={state.selectedId}
            onSelect={id => controller.select(id)} onReveal={reveal} />
        )}
        {state.selectedId && (
          <Button className="reveal-action" variant="secondary" onPress={() => reveal(state.selectedId!)}>
            Показать в Проводнике
          </Button>
        )}
        {notice && <p className="persistent-warning" role="status">{notice}</p>}
      </section>
      {about && (
        <section className="about" aria-label="О программе">
          <h1>{__PRODUCT__.product_name} — {__PRODUCT__.product_name_ru}</h1>
          <p>Локальный поиск по вашим папкам.</p>
          <p>{__PRODUCT__.author_name} ({__PRODUCT__.organization_group})</p>
          <p>{__PRODUCT__.support_email}</p>
          <details>
            <summary>Лицензия MIT</summary>
            <pre>{license}</pre>
          </details>
          <details>
            <summary>Сторонние компоненты</summary>
            <pre>{notices}</pre>
          </details>
          <Button variant="ghost" onPress={() => setAbout(false)}>Закрыть</Button>
        </section>
      )}
      <footer className="app-footer">
        <span>{devPreview ? 'Предпросмотр' : 'Подключение к локальному поиску ожидается'}</span>
        <label className="theme-control">
          Тема
          <select aria-label="Тема предпросмотра" value={theme}
            onChange={event => setTheme(event.target.value as Theme)}>
            {builtinExtensions.themePreviewModes.map(option => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
      </footer>
    </AppShell>
  );
}
