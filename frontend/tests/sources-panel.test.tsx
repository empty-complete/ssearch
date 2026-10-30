import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { JsonObject, SettingsValidation } from '../src/bridge/types';
import { SearchClientError } from '../src/bridge/client';
import { SourcesPanel } from '../src/settings/SourcesPanel';
import type { SourcesSettingsPort } from '../src/settings/sourcesDraft';
import { settingsSnapshot, validSettings } from './fixtures/settings';

afterEach(cleanup);
const name = 'Название папки: C:\\Учебные документы';
function fixture(overrides: Partial<SourcesSettingsPort> = {}) {
  const saved = settingsSnapshot();
  const validate = vi.fn(async (request: { candidate: JsonObject; expected_revision: number }) =>
    validSettings(request.candidate, request.expected_revision));
  const apply = vi.fn(async (request: { candidate: JsonObject; expected_revision: number }) => ({
    ...settingsSnapshot(request.expected_revision + 1), settings: request.candidate,
  }));
  const client: SourcesSettingsPort = { settingsGet: async () => saved,
    settingsValidate: validate, settingsApply: apply, ...overrides };
  return { saved, client, validate, apply };
}
async function show(client: SourcesSettingsPort) {
  const onClose = vi.fn();
  let view!: ReturnType<typeof render>;
  await act(async () => { view = render(<SourcesPanel client={client} onClose={onClose} />); });
  return { ...view, onClose };
}

describe('sources settings surface over the actual typed P2 port', () => {
  it('shows the full saved root and editable label with explicit native unavailability and no auto save', async () => {
    const f = fixture();
    await show(f.client);
    expect(screen.queryByRole('textbox', { name })).toHaveValue('Документы');
    expect(screen.queryByText('C:\\Учебные документы')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Добавить папку' })).toBeDisabled();
    expect(screen.queryByText('Выбор папки в этом подключении недоступен.')).toBeVisible();
    expect(f.validate).not.toHaveBeenCalled();
    expect(f.apply).not.toHaveBeenCalled();
  });

  it('edits only the draft and requires validate then explicit save with the original revision', async () => {
    const f = fixture();
    await show(f.client);
    const user = userEvent.setup();
    const input = screen.queryByRole('textbox', { name });
    expect(input).toBeInTheDocument();
    await user.clear(input!); await user.type(input!, 'Мои чертежи');
    expect(f.saved.settings.sources).toEqual(settingsSnapshot().settings.sources);
    expect(screen.queryByRole('button', { name: 'Сохранить' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Проверить изменения' }));
    expect(f.validate).toHaveBeenCalledTimes(1);
    expect(f.validate.mock.calls[0][0]).toMatchObject({ expected_revision: 7,
      candidate: { revision: 7, sources: [{ label: 'Мои чертежи' }] } });
    expect(f.apply).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Сохранить' }));
    expect(f.apply).toHaveBeenCalledTimes(1);
    expect(f.apply.mock.calls[0][0].expected_revision).toBe(7);
    expect(screen.getByRole('textbox', { name })).toHaveValue('Мои чертежи');
  });

  it('cancel discards staged text and does not call the P2 writer', async () => {
    const f = fixture();
    await show(f.client);
    const user = userEvent.setup();
    const input = screen.queryByRole('textbox', { name });
    expect(input).toBeInTheDocument();
    await user.clear(input!); await user.type(input!, 'Не сохранять');
    await user.click(screen.getByRole('button', { name: 'Отменить изменения' }));
    expect(screen.getByRole('textbox', { name })).toHaveValue('Документы');
    expect(f.apply).not.toHaveBeenCalled();
    expect(f.validate).not.toHaveBeenCalled();
  });

  it('shows P2 duplicate errors and nested warnings as text and never saves an invalid candidate', async () => {
    const invalid: SettingsValidation = { valid: false, expected_revision: 7, normalized: null, diff: [], warnings: [],
      errors: [{ path: 'sources.0.path', code: 'duplicate_root', message: 'Папка уже добавлена.' }] };
    const validate = vi.fn().mockResolvedValueOnce(invalid)
      .mockImplementationOnce(async (request: { candidate: JsonObject; expected_revision: number }) => ({
        ...validSettings(request.candidate, request.expected_revision), warnings: ['source_roots_overlap'],
      }));
    const f = fixture({ settingsValidate: validate });
    await show(f.client);
    const user = userEvent.setup();
    const input = screen.queryByRole('textbox', { name });
    expect(input).toBeInTheDocument();
    await user.type(input!, ' — копия');
    await user.click(screen.getByRole('button', { name: 'Проверить изменения' }));
    expect(screen.queryByText('Папка уже добавлена.')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Сохранить' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Проверить изменения' }));
    expect(screen.queryByText('Некоторые папки вложены друг в друга. Их содержимое может пересекаться.')).toBeVisible();
    expect(f.apply).not.toHaveBeenCalled();
  });

  it('read-only projection shows sources without enabling editing, validate or save', async () => {
    const saved = { ...settingsSnapshot(), mutable_fields: [] };
    const f = fixture({ settingsGet: async () => saved });
    await show(f.client);
    expect(screen.queryByRole('textbox', { name })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Проверить изменения' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Сохранить' })).toBeDisabled();
    expect(screen.queryByText('Настройки доступны только для чтения в этом подключении.')).toBeVisible();
    expect(f.apply).not.toHaveBeenCalled();
  });

  it('unknown apply outcome keeps staged text visible with explicit reconciliation instead of a retry', async () => {
    const apply = vi.fn(async () => { throw new SearchClientError('request_timeout', { outcome: 'unknown' }); });
    const f = fixture({ settingsApply: apply });
    await show(f.client);
    const user = userEvent.setup();
    const input = screen.queryByRole('textbox', { name });
    expect(input).toBeInTheDocument();
    await user.clear(input!); await user.type(input!, 'Сохранить позже');
    await user.click(screen.getByRole('button', { name: 'Проверить изменения' }));
    await user.click(screen.getByRole('button', { name: 'Сохранить' }));
    expect(screen.getByRole('textbox', { name })).toHaveValue('Сохранить позже');
    expect(screen.queryByText('Неизвестно, сохранены ли изменения. Обновите сохранённые настройки перед новой попыткой.')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Обновить сохранённые настройки' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Сохранить' })).toBeDisabled();
    expect(apply).toHaveBeenCalledTimes(1);
  });
});
