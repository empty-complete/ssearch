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
  const results = page.getByRole('listbox', { name: 'Найденные документы' });
  await expect(results.getByRole('option')).toHaveCount(2);
  await results.getByRole('option').first().focus();
  await page.keyboard.press('Enter');
  await expect(page.getByText(/Предпросмотр: Проводник не открывается/)).toBeVisible();
});

test('search, duplicate paths, focus, keyboard and theme screenshots', async ({ page }, testInfo) => {
  await page.goto('/');
  const input = page.getByRole('searchbox'); await expect(input).toBeFocused();
  await input.fill('ведомость'); await input.press('Enter');
  const results = page.getByRole('listbox', { name: 'Найденные документы' });
  await expect(results.getByRole('option')).toHaveCount(2);
  await expect(page.getByText('C:\\Учебные документы\\Северный завод\\АР\\СтадияП\\Ведомость.xlsx')).toBeVisible();
  await expect(page.getByText('C:\\Учебные документы\\Северный завод\\КМ\\СтадияП\\Ведомость.xlsx')).toBeVisible();
  await results.getByRole('option').first().focus(); await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter');
  await expect(page.getByText(/Предпросмотр: Проводник не открывается/)).toBeVisible();
  for (const theme of ['light', 'dark']) {
    await page.getByRole('combobox', { name: 'Тема предпросмотра' }).selectOption(theme);
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    await page.screenshot({
      path: testInfo.outputPath(`${theme}-full.png`),
      fullPage: true,
      animations: 'disabled',
    });
  }
  await page.setViewportSize({ width: 640, height: 400 });
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 640);
  await page.screenshot({ path: testInfo.outputPath('narrow.png'), fullPage: true });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.screenshot({ path: testInfo.outputPath('reduced.png'), fullPage: true });
});

test('system palette follows media without losing query or selection; manual mode remains fixed', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/');
  const input = page.getByRole('searchbox');
  await input.fill('ведомость');
  await input.press('Enter');
  const results = page.getByRole('listbox', { name: 'Найденные документы' });
  await expect(results.getByRole('option')).toHaveCount(2);
  await results.getByRole('option').first().click();
  const selection = await results.getByRole('option', { selected: true }).innerText();
  const theme = page.getByRole('combobox', { name: 'Тема предпросмотра' });
  await theme.selectOption('system');
  for (const [mode, background, foreground] of [
    ['dark', 'rgb(30, 30, 46)', 'rgb(205, 214, 244)'],
    ['light', 'rgb(239, 241, 245)', 'rgb(76, 79, 105)'],
  ] as const) {
    await page.emulateMedia({ colorScheme: mode });
    await expect(page.locator('html')).toHaveAttribute('data-theme', mode);
    await expect(page.locator('body')).toHaveCSS('background-color', background);
    await expect(page.locator('body')).toHaveCSS('color', foreground);
    await expect(input).toHaveValue('ведомость');
    await expect(results.getByRole('option', { selected: true })).toHaveText(selection, { useInnerText: true });
  }
  await theme.selectOption('dark');
  for (const mode of ['dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: mode });
    // Observe the media event and React's following paint before asserting no manual change.
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(30, 30, 46)');
    await expect(input).toHaveValue('ведомость');
    await expect(results.getByRole('option', { selected: true })).toHaveText(selection, { useInnerText: true });
  }
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
  await expect(page.getByRole('alert')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('error.png'), fullPage: true });
});
