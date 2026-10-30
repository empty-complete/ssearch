import { Button, Input, Label, TextField } from '@heroui/react';
import type { SourceDraftRow } from './sourcesDraft';

interface SourceRowProps {
  source: SourceDraftRow;
  editable: boolean;
  onEdit: (changes: { label?: string; enabled?: boolean }) => void;
  onRemove: () => void;
}

export function SourceRow({ source, editable, onEdit, onRemove }: SourceRowProps) {
  return (
    <li className="source-row">
      <TextField className="source-label" value={source.label} onChange={label => onEdit({ label })}
        isDisabled={!editable} aria-label={`Название папки: ${source.path}`}>
        <Label>Название</Label><Input autoComplete="off" />
      </TextField>
      <p className="source-path">{source.path}</p>
      <div className="source-actions">
        <label className="source-enabled">
          <input type="checkbox" checked={source.enabled} disabled={!editable}
            onChange={event => onEdit({ enabled: event.target.checked })} />
          Искать в этой папке
        </label>
        <Button variant="ghost" isDisabled aria-label={`Проверить доступ: ${source.path}`}>Проверить доступ</Button>
        <Button variant="ghost" isDisabled={!editable} onPress={onRemove}
          aria-label={`Убрать папку ${source.path}`}>Убрать</Button>
      </div>
    </li>
  );
}
