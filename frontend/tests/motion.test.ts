import { expect, it } from 'vitest';
import { resolveMotionPolicy } from '../src/motion/policy';
it.each([[true,false,true,true], [false,true,true,true], [null,true,true,true], [null,false,true,false], [false,false,false,false]] as const)('combines native %s and media %s without losing visibility', (native, media, visible, reduced) => {
  expect(resolveMotionPolicy(native, media, visible)).toEqual({ reduced, visible });
});
