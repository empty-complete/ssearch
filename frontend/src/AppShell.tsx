import { useEffect, useState, type ReactNode } from 'react';
import { MotionProvider } from './motion/MotionProvider';

export type Theme = 'light' | 'dark' | 'system';
export type ShellMode = 'application' | 'bootstrapper';

interface AppShellProps {
  children: ReactNode;
  theme: Theme;
  mode: ShellMode;
}

export function AppShell({ children, theme, mode }: AppShellProps) {
  const [dark, setDark] = useState(() => matchMedia('(prefers-color-scheme: dark)').matches);
  useEffect(() => {
    const media = matchMedia('(prefers-color-scheme: dark)');
    const update = () => setDark(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);

  const resolved = theme === 'system' ? dark ? 'dark' : 'light' : theme;
  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
    document.documentElement.classList.toggle('dark', resolved === 'dark');
    document.documentElement.classList.toggle('light', resolved === 'light');
  }, [resolved]);

  return (
    <MotionProvider>
      <main className="app-shell" data-mode={mode}
        aria-label={mode === 'bootstrapper' ? 'Подготовка ssearch' : 'Поиск ssearch'}>
        {children}
      </main>
    </MotionProvider>
  );
}
