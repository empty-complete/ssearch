import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';
Object.defineProperty(window, 'matchMedia', { value: vi.fn(query => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} })) });
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
HTMLElement.prototype.scrollIntoView = () => {};
