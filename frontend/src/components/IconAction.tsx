import { Button, Tooltip } from '@heroui/react';
import type { ReactNode } from 'react';
export function IconAction({ label, icon, onPress, disabled = false }: { label: string; icon: ReactNode; onPress: () => void; disabled?: boolean }) {
  return <Tooltip delay={400}><Tooltip.Trigger<'button'> render={props => <Button {...props} className="icon-action" isIconOnly aria-label={label} variant="ghost" isDisabled={disabled} onPress={onPress}>{icon}</Button>} /><Tooltip.Content>{label}</Tooltip.Content></Tooltip>;
}
