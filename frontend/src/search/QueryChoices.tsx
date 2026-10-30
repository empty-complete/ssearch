import { Button } from '@heroui/react';
import type { ProjectOption, SearchResponse } from '../bridge/types';

interface QueryChoicesProps {
  ambiguities: SearchResponse['ambiguities'];
  selected: ProjectOption | null;
  onChoose: (id: string | null) => void;
  disabled?: boolean;
}

export function QueryChoices({ ambiguities, selected, onChoose, disabled }: QueryChoicesProps) {
  const options = [...new Map(ambiguities.flatMap(item => item.options)
    .map(option => [option.project_id, option])).values()];
  if (!options.length && !selected) return null;
  return (
    <section className="query-choices" aria-label="Уточнить проект">
      <p>{selected ? `Проект: ${selected.canonical_name}` : 'Уточните проект, если нужно:'}</p>
      <div className="project-options">
        {options.map(option => (
          <Button key={option.project_id} variant="secondary" size="sm"
            aria-pressed={selected?.project_id === option.project_id}
            isDisabled={disabled} onPress={() => onChoose(option.project_id)}>
            {option.canonical_name}
          </Button>
        ))}
        {selected && <Button variant="ghost" size="sm" isDisabled={disabled}
          onPress={() => onChoose(null)}>Все проекты</Button>}
      </div>
    </section>
  );
}
