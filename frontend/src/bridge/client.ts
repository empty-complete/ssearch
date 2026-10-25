import type { SearchService } from './types';
// Task 2 supplies the typed bridge. No HTTP, secret, host import or mock fallback.
export const unavailableService: SearchService = {
  async search() { throw new Error('service_unavailable'); },
};
