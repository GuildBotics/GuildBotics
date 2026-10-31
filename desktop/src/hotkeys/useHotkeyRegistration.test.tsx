import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getHotkeys, subscribeEvents, type RuntimeEvent } from "../api/client";
import "../i18n";
import { makeRuntimeEvent } from "../test/factories";
import { syncHotkeys } from "./hotkeyRuntime";
import { useHotkeyRegistration } from "./useHotkeyRegistration";

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  getHotkeys: vi.fn(),
  subscribeEvents: vi.fn(),
}));
vi.mock("./hotkeyRuntime", () => ({ syncHotkeys: vi.fn(async () => []) }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => undefined) }));

let publish: ((event: RuntimeEvent) => void) | undefined;

function renderRegistration() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderHook(() => useHotkeyRegistration(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

describe("useHotkeyRegistration", () => {
  beforeEach(() => {
    publish = undefined;
    vi.mocked(subscribeEvents).mockImplementation((onEvent) => {
      publish = onEvent;
      return () => undefined;
    });
    vi.mocked(getHotkeys)
      .mockResolvedValueOnce({ quick_run: "Control+Alt+Space", commands: {} })
      .mockResolvedValue({ quick_run: "Control+Alt+K", commands: {} });
  });

  afterEach(() => {
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  it("re-registers in the Desktop window when an assignment is saved elsewhere", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    renderRegistration();
    await waitFor(() =>
      expect(syncHotkeys).toHaveBeenCalledWith({ quick_run: "Control+Alt+Space", commands: {} }),
    );

    publish!(makeRuntimeEvent({ type: "hotkeys.changed" }));

    await waitFor(() =>
      expect(syncHotkeys).toHaveBeenLastCalledWith({ quick_run: "Control+Alt+K", commands: {} }),
    );
  });

  it("does not listen in a browser, which registers nothing with the OS", async () => {
    renderRegistration();
    await waitFor(() => expect(getHotkeys).toHaveBeenCalledTimes(1));

    expect(subscribeEvents).not.toHaveBeenCalled();
  });
});
