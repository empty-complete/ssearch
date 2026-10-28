import { afterEach, expect, it, vi } from 'vitest';
import { readTransientQuery, writeTransientQuery } from '../src/bridge/transientQuery';

afterEach(() => { sessionStorage.clear(); vi.restoreAllMocks(); });

it('recovers only the ephemeral query without issuing a service request or writing settings', () => {
  writeTransientQuery('чертёж 🪐');
  expect(readTransientQuery()).toBe('чертёж 🪐');
  writeTransientQuery('новый запрос');
  expect(readTransientQuery()).toBe('новый запрос');
  writeTransientQuery('x'.repeat(65_537));
  expect(readTransientQuery()).toBe('');
});

it('does not prevent query editing when session storage is refused', () => {
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  expect(() => writeTransientQuery('current')).not.toThrow();
});
