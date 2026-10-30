import { Description, Label, ListBox } from '@heroui/react';
import type { ResultMetadata } from '../bridge/types';
import { Fragment, type Ref } from 'react';

interface ResultListProps {
  results: ResultMetadata[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onReveal: (id: string) => void;
  listRef?: Ref<HTMLDivElement>;
  query?: string;
  disabled?: boolean;
}

/** Literal React text fragments only; filenames and query terms never become HTML. */
export function HighlightedText({ text, query }: { text: string; query: string }) {
  const terms = [...new Set(query.toLocaleLowerCase().split(/\s+/).filter(Boolean))]
    .sort((left, right) => right.length - left.length);
  if (!terms.length) return <>{text}</>;
  const lower = text.toLocaleLowerCase();
  const parts = [];
  let offset = 0;
  while (offset < text.length) {
    let start = -1;
    let length = 0;
    for (const term of terms) {
      const index = lower.indexOf(term, offset);
      if (index >= 0 && (start < 0 || index < start)) { start = index; length = term.length; }
    }
    if (start < 0) { parts.push(<Fragment key={offset}>{text.slice(offset)}</Fragment>); break; }
    if (start > offset) parts.push(<Fragment key={`text-${offset}`}>{text.slice(offset, start)}</Fragment>);
    parts.push(<mark key={`match-${start}`}>{text.slice(start, start + length)}</mark>);
    offset = start + length;
  }
  return <>{parts}</>;
}

export function ResultList({ results, selectedId, onSelect, onReveal, listRef, query = '', disabled }: ResultListProps) {
  return (
    <ListBox
      ref={listRef}
      className="results"
      aria-label="Найденные документы"
      selectionMode="single"
      selectionBehavior="replace"
      selectedKeys={selectedId ? [selectedId] : []}
      onSelectionChange={keys => {
        if (keys !== 'all') onSelect((Array.from(keys)[0] as string | undefined) ?? null);
      }}
      onAction={key => onReveal(String(key))}
      items={results}
      disabledKeys={disabled ? results.map(result => result.result_id) : []}
    >
      {result => (
        <ListBox.Item id={result.result_id} textValue={`${result.name} ${result.full_path}`} className="result-row">
          <Label className="result-name"><HighlightedText text={result.name}
            query={result.matched_fields.includes('name') ? query : ''} /></Label>
          <Description className="result-path">
            <span><HighlightedText text={result.full_path}
              query={result.matched_fields.some(field => field === 'path' || field === 'relative_path') ? query : ''} /></span>
            {result.stale && (
              <span className="result-warning">Источник недоступен. Путь мог измениться.</span>
            )}
          </Description>
        </ListBox.Item>
      )}
    </ListBox>
  );
}
