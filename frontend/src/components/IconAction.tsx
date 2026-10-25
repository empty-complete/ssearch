import { Button, Tooltip } from '@heroui/react';
import type { ReactNode } from 'react';

interface IconActionProps {
  label: string;
  icon: ReactNode;
  onPress: () => void;
  disabled?: boolean;
}

export function IconAction({ label, icon, onPress, disabled = false }: IconActionProps) {
  // HeroUI Button wraps React Aria Button, which consumes Tooltip's focus context.
  // Keeping it as the direct trigger avoids an extra interactive wrapper and
  // avoids translating DOM focus events into React Aria's press/focus types.
  return (
    <Tooltip delay={400}>
      <Button
        className="icon-action"
        isIconOnly
        aria-label={label}
        variant="ghost"
        isDisabled={disabled}
        onPress={onPress}
      >
        {icon}
      </Button>
      <Tooltip.Content>{label}</Tooltip.Content>
    </Tooltip>
  );
}
