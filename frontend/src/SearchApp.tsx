import { Button } from '@heroui/react';
import { useState } from 'react';
import { AppShell, type ShellMode, type Theme } from './AppShell';
import { IconAction } from './components/IconAction';
import { SearchPage } from './search/SearchPage';
import type { SearchService } from './bridge/types';
import license from '../../LICENSE.txt?raw';
import notices from '../THIRD_PARTY_NOTICES.txt?raw';
import { builtinExtensions } from '../../desktop/src/extensions/registry';
import { readTransientQuery, writeTransientQuery } from './bridge/transientQuery';

interface SearchAppProps {
  service: SearchService;
  scope?: string[] | null;
  preview?: boolean;
  mode?: ShellMode;
}

export function SearchApp({ service, scope = null, preview = false, mode = 'application' }: SearchAppProps) {
  const [theme, setTheme] = useState<Theme>('system');
  const [about, setAbout] = useState(false);
  const [initialQuery] = useState(readTransientQuery);
  const devPreview = import.meta.env.DEV && preview;
  return (
    <AppShell theme={theme} mode={mode}>
      <header className="app-header">
        <div className="wordmark">
          <img src="./app-icon.png" width="24" height="24" alt="" />
          <span>{__PRODUCT__.product_name}</span>
        </div>
        {devPreview && <p className="preview-label">Учебные данные. Предпросмотр интерфейса.</p>}
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
      <SearchPage client={service} scope={scope} mode="application"
        initialQuery={initialQuery} onQueryChange={writeTransientQuery} />
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
        <span>Локальный поиск</span>
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
