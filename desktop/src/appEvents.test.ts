import { beforeEach, describe, expect, it, vi } from "vitest";

import { announceWorkspaceChange, onWorkspaceChange } from "./appEvents";

const emit = vi.fn(async () => undefined);
let listener: (() => void) | undefined;
const unlisten = vi.fn();
vi.mock("@tauri-apps/api/event", () => ({
  emit: (...args: unknown[]) => emit(...(args as [])),
  listen: async (_event: string, handler: () => void) => {
    listener = handler;
    return unlisten;
  },
}));

describe("workspace change notifications", () => {
  // Each window has its own query cache, so a switch has to be announced
  // rather than left for the other window to notice.
  beforeEach(() => {
    emit.mockClear();
    unlisten.mockClear();
    listener = undefined;
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  });

  it("announces the switch to the other windows", async () => {
    await announceWorkspaceChange();

    expect(emit).toHaveBeenCalledWith("app://workspace-changed");
  });

  it("runs the handler when another window switches", async () => {
    const handler = vi.fn();
    const stop = onWorkspaceChange(handler);
    await vi.waitFor(() => expect(listener).toBeDefined());

    listener!();

    expect(handler).toHaveBeenCalled();
    stop();
    expect(unlisten).toHaveBeenCalled();
  });

  it("stays quiet outside the desktop shell", async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;

    await announceWorkspaceChange();

    expect(emit).not.toHaveBeenCalled();
    expect(onWorkspaceChange(vi.fn())).toBeTypeOf("function");
  });
});
