import { Input, Label, TextField } from '@heroui/react';
import { useRef, type Ref } from 'react';

interface SearchBoxProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  isComposing?: boolean;
  inputRef?: Ref<HTMLInputElement>;
  onNavigate?: (edge: 'first' | 'last') => boolean;
}

export function SearchBox({ value, onChange, onSubmit, isComposing = false, inputRef, onNavigate }: SearchBoxProps) {
  const composing = useRef(false);
  return (
    <TextField className="search-field" value={value} onChange={onChange}
      aria-label="Объект, название или часть пути">
      <Label className="search-label">Найти документ</Label>
      <Input
        ref={inputRef}
        autoFocus
        type="search"
        placeholder="Объект, название или часть пути"
        autoComplete="off"
        spellCheck={false}
        onCompositionStart={() => { composing.current = true; }}
        onCompositionEnd={() => { composing.current = false; }}
        onKeyDown={event => {
          const native = event.nativeEvent;
          if (composing.current || isComposing || native.isComposing || native.keyCode === 229) return;
          if ((event.key === 'ArrowDown' || event.key === 'ArrowUp')
            && onNavigate?.(event.key === 'ArrowDown' ? 'first' : 'last')) {
            event.preventDefault();
          } else if (event.key === 'Enter') {
            event.preventDefault();
            onSubmit();
          }
        }}
      />
    </TextField>
  );
}
