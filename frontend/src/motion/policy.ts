export interface MotionPolicy { reduced: boolean; visible: boolean }
export function resolveMotionPolicy(nativeReduced: boolean | null, mediaReduced: boolean, visible: boolean): MotionPolicy {
  return { reduced: nativeReduced === true || mediaReduced, visible };
}
