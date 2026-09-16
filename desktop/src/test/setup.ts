import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});

// Reduced motion is reported as the user's preference so that Mantine runs its
// transitions with a zero duration, which is the synchronous path through
// `useTransition`. See `src/test/TestMantineProvider.tsx` for why a transition
// that schedules timers can fail a suite whose tests all passed.
const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === REDUCED_MOTION,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(window, "ResizeObserver", {
  writable: true,
  value: ResizeObserverMock,
});

// jsdom does not implement scrollIntoView, which Mantine's Combobox calls on a
// timer after an option is selected. Provide a no-op so it does not surface as
// an unhandled error during tests.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// jsdom implements no CSS Font Loading API, so `document.fonts` is undefined.
// Mantine's autosizing Textarea subscribes to it to re-measure once webfonts
// finish loading, and every component test that renders one throws without it.
if (!document.fonts) {
  Object.defineProperty(document, "fonts", {
    writable: true,
    value: { addEventListener: () => {}, removeEventListener: () => {} },
  });
}
