import { useEffect, useState, type ReactNode, type CSSProperties } from 'react';
import { resolveMotionPolicy } from './policy';
import { motionTokens } from './tokens';

interface MotionProviderProps {
  children: ReactNode;
  nativeReduced?: boolean | null;
}

export function MotionProvider({ children, nativeReduced = null }: MotionProviderProps) {
  const [media, setMedia] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [visible, setVisible] = useState(() => !document.hidden);

  useEffect(() => {
    const query = matchMedia('(prefers-reduced-motion: reduce)');
    const changed = () => setMedia(query.matches);
    const visibility = () => setVisible(!document.hidden);
    query.addEventListener('change', changed);
    document.addEventListener('visibilitychange', visibility);
    return () => {
      query.removeEventListener('change', changed);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, []);

  const policy = resolveMotionPolicy(nativeReduced, media, visible);
  useEffect(() => {
    // Portalled HeroUI overlays inherit the same policy as inline content.
    document.documentElement.dataset.motion = policy.reduced ? 'reduced' : 'full';
    document.documentElement.dataset.visible = String(policy.visible);
  }, [policy.reduced, policy.visible]);

  const style = {
    '--motion-feedback': `${motionTokens.feedback}ms`,
    '--motion-transition': `${motionTokens.transition}ms`,
    '--motion-overlay': `${motionTokens.overlay}ms`,
  } as CSSProperties;
  return (
    <div className="motion-root" data-motion={policy.reduced ? 'reduced' : 'full'}
      data-visible={policy.visible} style={style}>
      {children}
    </div>
  );
}
