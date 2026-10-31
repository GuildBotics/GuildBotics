import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeEvent } from "./api/client";
import { onAppEvent, onWorkspaceChange } from "./appEvents";
import { makeRuntimeEvent } from "./test/factories";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  close = vi.fn();
  onopen?: () => void;
  onmessage?: (message: { data: string }) => void;
  onerror?: () => void;
  onclose?: () => void;

  constructor() {
    FakeWebSocket.instances.push(this);
  }

  publish(event: RuntimeEvent) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}

const sockets = () => FakeWebSocket.instances;

describe("server announcements", () => {
  // Each screen has its own query cache, so a change on the server reaches
  // every one of them through the event stream, in a browser as in Desktop.
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("runs the handler when the server opens another workspace", () => {
    const handler = vi.fn();
    const unsubscribe = onWorkspaceChange(handler);

    sockets()[0].publish(makeRuntimeEvent({ type: "command.started" }));
    expect(handler).not.toHaveBeenCalled();
    sockets()[0].publish(
      makeRuntimeEvent({ type: "workspace.changed", payload: { workspace: "/w" } }),
    );
    expect(handler).toHaveBeenCalledTimes(1);

    unsubscribe();
    expect(sockets()[0].close).toHaveBeenCalled();
  });

  it("tells hotkey changes apart from a workspace switch", () => {
    const handler = vi.fn();
    onAppEvent("hotkeys.changed", handler);

    sockets()[0].publish(makeRuntimeEvent({ type: "workspace.changed" }));
    sockets()[0].publish(makeRuntimeEvent({ type: "hotkeys.changed" }));

    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("listens again shortly after the server closes the stream", () => {
    const handler = vi.fn();
    onWorkspaceChange(handler);

    sockets()[0].onclose?.();
    vi.advanceTimersByTime(1999);
    expect(sockets()).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets()).toHaveLength(2);

    // A server that is still down closes the new socket too; it keeps trying.
    sockets()[1].onclose?.();
    vi.advanceTimersByTime(2000);
    expect(sockets()).toHaveLength(3);

    sockets()[2].publish(makeRuntimeEvent({ type: "workspace.changed" }));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("stops listening for good once stopped", () => {
    const unsubscribe = onWorkspaceChange(vi.fn());
    sockets()[0].onclose?.();

    // Stopping during the wait cancels the pending reconnect.
    unsubscribe();
    vi.advanceTimersByTime(2000);
    expect(sockets()).toHaveLength(1);

    // The close the stop itself causes does not reconnect either.
    const again = onWorkspaceChange(vi.fn());
    again();
    sockets()[1].onclose?.();
    vi.advanceTimersByTime(2000);
    expect(sockets()).toHaveLength(2);
    expect(sockets()[1].close).toHaveBeenCalledTimes(1);
  });
});
