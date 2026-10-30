import { test, expect } from '@playwright/test';

const listName = 'Найденные документы';

test('project choices preserve the original query and input in both compact themes', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 640, height: 400 });
  await page.goto('/');
  const input = page.getByRole('searchbox');
  await input.fill('ПМ спецификация');
  const originalInput = await input.elementHandle();
  await input.press('Enter');
  await expect(page.getByRole('button', { name: 'Пермские моторы', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Поставка материалов', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  for (const theme of ['light', 'dark']) {
    await page.getByRole('combobox', { name: 'Тема предпросмотра' }).selectOption(theme);
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    await page.screenshot({ path: testInfo.outputPath(`ambiguity-${theme}-640x400.png`), animations: 'disabled' });
  }
  await page.getByRole('button', { name: 'Пермские моторы', exact: true }).click();
  await expect(input).toHaveValue('ПМ спецификация');
  await expect(input).toBeFocused();
  expect(await originalInput?.evaluate(element => element.isConnected)).toBe(true);
  await expect(page.getByText('Проект: Пермские моторы', { exact: true })).toBeVisible();
  const results = page.getByRole('listbox', { name: listName });
  await expect(results.getByRole('option')).toHaveCount(2);
  await input.press('ArrowDown');
  await expect(results.getByRole('option', { selected: true })).toContainText('АР');
  await page.keyboard.press('ArrowDown');
  await expect(results.getByRole('option', { selected: true })).toContainText('КМ');
  for (const theme of ['light', 'dark']) {
    await page.getByRole('combobox', { name: 'Тема предпросмотра' }).selectOption(theme);
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    await page.screenshot({ path: testInfo.outputPath(`results-${theme}-640x400.png`), animations: 'disabled' });
  }
});

test('long results scroll the active row while query stays visible and Tab leaves the composite', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 640, height: 400 });
  await page.goto('/');
  const input = page.getByRole('searchbox');
  await input.fill('длинный'); await input.press('Enter');
  const results = page.getByRole('listbox', { name: listName });
  await expect(results.getByRole('option')).toHaveCount(50);
  await input.press('ArrowDown');
  await expect(results.getByRole('option', { selected: true })).toContainText('Длинный документ 1.xlsx');
  await page.keyboard.press('End');
  await expect(results.getByRole('option', { selected: true })).toContainText('Длинный документ 50.xlsx');
  await expect(results.getByRole('option', { selected: true })).toBeInViewport();
  await expect(input).toBeInViewport();
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 640);
  await page.screenshot({ path: testInfo.outputPath('long-results-active-last.png'), animations: 'disabled' });
  await page.keyboard.press('Tab');
  expect(await results.evaluate(element => element.contains(document.activeElement))).toBe(false);
});

test('editing and composition cannot activate a result from the previous completed query', async ({ page }) => {
  await page.goto('/');
  const input = page.getByRole('searchbox');
  await input.fill('ведомость'); await input.press('Enter');
  const results = page.getByRole('listbox', { name: listName });
  await expect(results.getByRole('option')).toHaveCount(2);
  await input.press('ArrowDown');
  await expect(page.getByRole('button', { name: 'Показать в Проводнике' })).toBeDisabled();
  await input.fill('медленно');
  await input.dispatchEvent('compositionstart');
  await input.dispatchEvent('keydown', { key: 'Enter', isComposing: true, keyCode: 229 });
  await expect(page.locator('.result-area')).toHaveAttribute('aria-busy', 'false');
  await expect(page.getByRole('listbox', { name: listName })).toHaveCount(0);
  await input.dispatchEvent('compositionend');
  await input.press('Enter');
  await expect(page.locator('.result-area')).toHaveAttribute('aria-busy', 'true');
  await expect(page.getByRole('listbox', { name: listName })).toHaveCount(0);
  await expect(input).toHaveValue('медленно');
  await expect(results.getByRole('option')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'Показать в Проводнике' })).toHaveCount(0);
  await input.press('ArrowDown');
  await expect(page.getByRole('button', { name: 'Показать в Проводнике' })).toBeDisabled();
});
