import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { setCsrfToken } from "../api/client";

// Production query waits reached 1.50s including RTL's event-loop drain.
configure({ asyncUtilTimeout: 3_000 });

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  setCsrfToken(null);
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

// Mantine reads media queries. Reduced motion is reported as preferred, so its
// transitions run with a zero duration (the synchronous path; see
// GuildBotics' desktop/src/test/TestMantineProvider.tsx for why timers left
// by a transition can fail a later file).
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === "(prefers-reduced-motion: reduce)",
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
Object.defineProperty(window, "ResizeObserver", { writable: true, value: ResizeObserverMock });

// jsdom has no scrollIntoView, which Mantine's Combobox calls after a choice.
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
