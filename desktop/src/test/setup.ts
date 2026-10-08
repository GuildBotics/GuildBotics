import "@testing-library/jest-dom/vitest";
import { createElement, forwardRef } from "react";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import type { TextareaProps } from "@mantine/core";

import { configureApi } from "../api/client";

// Components render inside an app whose backend announced its port.
beforeEach(() => {
  configureApi("test-token", "http://127.0.0.1:8765");
});

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

// jsdom has no layout, so an autosizing Textarea measures nothing, yet it runs
// `getComputedStyle` on every commit and a large form pays for that on each
// keystroke. Mantine means to render a plain textarea under test, but its build
// inlined NODE_ENV as "development" into `getEnv()`, so its own
// `autosize && getEnv() !== "test"` check never turns autosizing off. Remove
// this once Mantine's Textarea stops autosizing under Vitest again.
vi.mock("@mantine/core", async (importOriginal) => {
  const mantine = await importOriginal<typeof import("@mantine/core")>();
  const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>((props, ref) =>
    createElement(mantine.Textarea, { ...props, autosize: false, ref }),
  );
  return { ...mantine, Textarea };
});
