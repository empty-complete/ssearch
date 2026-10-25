import { Input, Label, TextField } from '@heroui/react';
import { useRef } from 'react';

interface SearchBoxProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  isComposing?: boolean;
}

export function SearchBox({ value, onChange, onSubmit, isComposing = false }: SearchBoxProps) {
  const composing = useRef(false);
  return (
    <TextField className="search-field" value={value} onChange={onChange}
      aria-label="Объект, название или часть пути">
      <Label className="search-label">Найти документ</Label>
      <Input
        autoFocus
        type="search"
        placeholder="Объект, название или часть пути"
        autoComplete="off"
        spellCheck={false}
        onCompositionStart={() => { composing.current = true; }}
        onCompositionEnd={() => { composing.current = false; }}
        onKeyDown={event => {
          const native = event.nativeEvent;
          if (event.key === 'Enter' && !composing.current && !isComposing
            && !native.isComposing && native.keyCode !== 229) {
            event.preventDefault();
            onSubmit();
          }
        }}
      />
    </TextField>
  );
}
