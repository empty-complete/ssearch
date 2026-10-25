import { Description, Label, ListBox } from '@heroui/react';
import type { ResultMetadata } from '../bridge/types';

interface ResultListProps {
  results: ResultMetadata[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onReveal: (id: string) => void;
}

export function ResultList({ results, selectedId, onSelect, onReveal }: ResultListProps) {
  return (
    <ListBox
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
    >
      {result => (
        <ListBox.Item id={result.result_id} textValue={`${result.name} ${result.full_path}`} className="result-row">
          <Label className="result-name">{result.name}</Label>
          <Description className="result-path">{result.full_path}</Description>
          {result.stale && (
            <Description className="result-warning">Источник недоступен · путь мог измениться</Description>
          )}
        </ListBox.Item>
      )}
    </ListBox>
  );
}
