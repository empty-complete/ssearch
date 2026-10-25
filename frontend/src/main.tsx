import { createRoot } from 'react-dom/client';
import { SearchApp } from './SearchApp';
import { createSearchClient, unavailableService } from './bridge/client';
import './styles.css';

async function start() {
  let service = window.ssearch ? createSearchClient(window.ssearch) : unavailableService;
  let preview = false;
  // Vite removes this branch and the fixture chunk from every production build.
  if (import.meta.env.DEV && import.meta.env.VITE_DEV_MOCK === '1') {
    service = (await import('./dev/mockService')).mockService;
    preview = true;
  }
  const root = document.getElementById('root');
  if (!root) throw new Error('Missing application root');
  createRoot(root).render(<SearchApp service={service} preview={preview} />);
}
void start();
