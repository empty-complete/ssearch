import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { SearchBox } from '../src/components/SearchBox';
import { ResultList } from '../src/components/ResultList';
import { IconAction } from '../src/components/IconAction';
import type { ResultMetadata } from '../src/bridge/types';

afterEach(cleanup);
it('keeps one named icon button and exposes its tooltip on keyboard focus and hover', async () => {
  const press = vi.fn();
  render(<IconAction label="О программе" icon={<svg aria-hidden="true" />} onPress={press} />);
  expect(screen.getAllByRole('button')).toHaveLength(1);
  const user = userEvent.setup();
  await user.tab();
  expect(screen.getByRole('button', { name: 'О программе' })).toHaveFocus();
  expect(await screen.findByRole('tooltip')).toHaveTextContent('О программе');
  await user.keyboard('{Enter}');
  expect(press).toHaveBeenCalledTimes(1);
  await user.tab();
  // Move outside first: userEvent.hover teleports directly onto the target and
  // dispatches hover-start before its pointer-move. React Aria intentionally
  // ignores hover-start while the last interaction modality is still keyboard.
  await user.pointer({ target: document.body, coords: { clientX: 1, clientY: 1 } });
  await user.hover(screen.getByRole('button', { name: 'О программе' }));
  expect(await screen.findByRole('tooltip')).toHaveTextContent('О программе');
});
it('preserves composition and submits only the committed current input', async () => {
  const submit = vi.fn(); const change = vi.fn();
  render(<SearchBox value="工" onChange={change} onSubmit={submit} />);
  const input = screen.getByRole('searchbox');
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: 'Enter', isComposing: true, keyCode: 229 });
  expect(submit).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  fireEvent.change(input, { target: { value: '工程' } });
  expect(change).toHaveBeenCalledWith('工程');
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(submit).toHaveBeenCalledTimes(1);
});
const result = (id: string, path: string): ResultMetadata => ({ result_id: id, source_id: 'source', name: 'Ведомость.xlsx', full_path: path, relative_path: path, stale: false, size: 1, modified_ns: 0, project_id: null, matched_fields: [], score: null, match_kind: 'lexical' });
it('exposes distinct full duplicate paths and keyboard selection without hover', async () => {
  const select = vi.fn(); const reveal = vi.fn();
  render(<ResultList results={[result('a', 'C:\\Проект\\АР\\СтадияП\\Ведомость.xlsx'), result('b', 'C:\\Проект\\КМ\\СтадияП\\Ведомость.xlsx')]} selectedId={null} onSelect={select} onReveal={reveal} />);
  expect(screen.getByText('C:\\Проект\\АР\\СтадияП\\Ведомость.xlsx')).toBeVisible();
  expect(screen.getByText('C:\\Проект\\КМ\\СтадияП\\Ведомость.xlsx')).toBeVisible();
  await userEvent.tab(); await userEvent.keyboard('{ArrowDown}{Enter}');
  expect(select).toHaveBeenCalled();
  expect(reveal).toHaveBeenCalled();
});
