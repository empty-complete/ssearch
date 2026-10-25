import { test, expect } from '@playwright/test';

test('icon tooltip appears on real keyboard focus and pointer hover', async ({ page }) => {
  await page.goto('/');
  const input = page.getByRole('searchbox');
  const about = page.getByRole('button', { name: 'О программе' });
  await expect(input).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(about).toBeFocused();
  await expect(page.getByRole('tooltip')).toHaveText('О программе');
  await page.keyboard.press('Tab');
  await expect(input).toBeFocused();
  await page.mouse.move(1, 1);
  await about.hover();
  await expect(page.getByRole('tooltip')).toHaveText('О программе');
});

test('loading skeleton respects reduced motion while results remain usable', async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  const input = page.getByRole('searchbox');
  await input.fill('медленно');
  await input.press('Enter');
  const skeleton = page.locator('.skeleton').first();
  await expect(skeleton).toBeVisible();
  await expect(skeleton).toHaveCSS('animation-name', 'none');
  await page.screenshot({ path: testInfo.outputPath('reduced-loading.png'), fullPage: true });
  await expect(page.getByRole('option')).toHaveCount(2);
  await page.getByRole('option').first().focus();
  await page.keyboard.press('Enter');
  await expect(page.getByText(/Предпросмотр: Проводник не открывается/)).toBeVisible();
});

test('search, duplicate paths, focus, keyboard and theme screenshots', async ({ page }, testInfo) => {
  await page.goto('/');
  const input = page.getByRole('searchbox'); await expect(input).toBeFocused();
  await input.fill('ведомость'); await input.press('Enter');
  await expect(page.getByRole('option')).toHaveCount(2);
  await expect(page.getByText('C:\\Учебные документы\\Северный завод\\АР\\СтадияП\\Ведомость.xlsx')).toBeVisible();
  await expect(page.getByText('C:\\Учебные документы\\Северный завод\\КМ\\СтадияП\\Ведомость.xlsx')).toBeVisible();
  await page.getByRole('option').first().focus(); await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter');
  await expect(page.getByText(/Предпросмотр: Проводник не открывается/)).toBeVisible();
  for (const theme of ['light', 'dark']) {
    await page.getByRole('combobox', { name: 'Тема предпросмотра' }).selectOption(theme);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-full.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 640, height: 400 });
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 640);
  await page.screenshot({ path: testInfo.outputPath('narrow.png'), fullPage: true });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.screenshot({ path: testInfo.outputPath('reduced.png'), fullPage: true });
});
test('empty, partial, unavailable and reconnect preserve the current query', async ({ page }, testInfo) => {
  await page.goto('/'); const input = page.getByRole('searchbox');
  await input.fill('пусто'); await input.press('Enter');
  await expect(page.getByText(/Документы не найдены/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('empty.png'), fullPage: true });
  await input.fill('частично'); await input.press('Enter');
  await expect(page.getByText(/Результаты могут быть неполными/)).toBeVisible();
  await input.fill('ошибка'); await input.press('Enter');
  await expect(page.getByRole('alert')).toBeVisible(); await expect(input).toHaveValue('ошибка');
  await page.getByRole('button', { name: 'Повторить соединение' }).click();
  await expect(input).toHaveValue('ошибка');
  await page.screenshot({ path: testInfo.outputPath('error.png'), fullPage: true });
});
