import { test, expect } from '@playwright/test';
test.use({ baseURL: 'http://127.0.0.1:5174' });
test('production uses local assets, denies networking and preserves reconnect query', async ({ page }) => {
  const requests: string[] = []; const pageErrors: string[] = [];
  page.on('request', request => requests.push(request.url()));
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto('/'); const input = page.getByRole('searchbox');
  await expect(input).toBeFocused();
  await input.fill('ведомость'); await input.press('Enter');
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('listbox', { name: 'Найденные документы' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Повторить соединение' }).click();
  await expect(input).toHaveValue('ведомость');
  await expect(page.getByText(/Учебные данные/)).toHaveCount(0);
  expect(requests.every(url => url.startsWith('http://127.0.0.1:5174/'))).toBe(true);
  expect(pageErrors).toEqual([]);
  const connection = await page.evaluate(async () => {
    try { await fetch('https://example.com/should-never-leave-renderer'); return 'unexpected'; }
    catch { return 'blocked'; }
  });
  expect(connection).toBe('blocked');
});
