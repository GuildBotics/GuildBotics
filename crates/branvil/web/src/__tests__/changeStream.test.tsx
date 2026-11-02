import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LOST_AFTER_MS, useChangeStream } from "../query/events";
import { keys } from "../query/keys";

// Each channel instance is a separate document's endpoint. Messages are queued,
// are never delivered to their sender, and disappear when an endpoint closes.
class Channel {
  static all = new Set<Channel>();
  onmessage: ((event: { data: unknown }) => void) | null = null;
  constructor(readonly name: string) { Channel.all.add(this); }
  postMessage(data: unknown) {
    for (const peer of Channel.all) {
      if (peer !== this && peer.name === this.name) {
        queueMicrotask(() => { if (Channel.all.has(peer)) peer.onmessage?.({ data }); });
      }
    }
  }
  close() { Channel.all.delete(this); }
}

// Exclusive FIFO locks; abort removes only a waiting request. An acquired lock
// stays held until the callback's promise settles, like the browser API.
class Locks {
  held = false;
  waiting: Array<() => void> = [];
  request = vi.fn((_name: string, { signal }: { signal: AbortSignal }, callback: () => Promise<void>) =>
    new Promise<void>((resolve, reject) => {
      const abort = () => {
        this.waiting = this.waiting.filter((entry) => entry !== acquire);
        reject(new DOMException("Aborted", "AbortError"));
      };
      const acquire = () => {
        signal.removeEventListener("abort", abort);
        this.held = true;
        void callback().then(resolve, reject).finally(() => {
          this.held = false;
          this.waiting.shift()?.();
        });
      };
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) abort();
      else if (this.held) this.waiting.push(acquire);
      else acquire();
    }),
  );
}

class Source extends EventTarget {
  static CLOSED = 2;
  static all: Source[] = [];
  readyState = 0;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string, readonly options: EventSourceInit) {
    super();
    Source.all.push(this);
  }
  open() { this.readyState = 1; this.onopen?.(); }
  fail(state = 0) { this.readyState = state; this.onerror?.(); }
  change(data = '{"kind":"ticket","path":"acme/demo","id":"1"}') {
    this.dispatchEvent(new MessageEvent("change", { data }));
  }
  reset() { this.dispatchEvent(new Event("reset")); }
  close() { this.closed = true; this.readyState = Source.CLOSED; }
}

async function flush(action: () => void = () => {}) {
  await act(async () => {
    action();
    // Drain channel delivery, lock release, and query refetch promises.
    for (let i = 0; i < 12; i++) await Promise.resolve();
  });
}

const clients: QueryClient[] = [];
function tab(enabled = true, strict = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  clients.push(client);
  const shown = vi.fn(async () => ({}));
  const observer = new QueryObserver(client, { queryKey: keys.ticket("acme/demo", "1"), queryFn: shown, initialData: {}, staleTime: Infinity });
  const stopObserving = observer.subscribe(() => {});
  client.setQueryData(keys.ticket("acme/demo", "2"), {});
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const hook = renderHook(({ enabled }) => useChangeStream(client, enabled), {
    initialProps: { enabled },
    wrapper: strict ? StrictMode : undefined,
  });
  return { ...hook, client, shown, invalidate, stopObserving };
}

beforeEach(() => {
  vi.useFakeTimers();
  Source.all = [];
  vi.stubGlobal("EventSource", Source);
  vi.stubGlobal("BroadcastChannel", Channel);
  vi.stubGlobal("navigator", { locks: new Locks() });
});

afterEach(async () => {
  cleanup();
  await flush();
  clients.splice(0).forEach((client) => client.clear());
  expect(Channel.all.size).toBe(0);
  expect(Source.all.every((source) => source.closed)).toBe(true);
});

describe("shared change stream", () => {
  it("uses one credentialed connection for seven tabs and refetches each tab's shown query", async () => {
    const tabs = Array.from({ length: 7 }, () => tab());
    await flush();
    expect(Source.all).toHaveLength(1);
    expect(Source.all[0]!.url).toBe("/api/v1/events");
    expect(Source.all[0]!.options).toEqual({ withCredentials: true });
    await flush(() => Source.all[0]!.open());
    expect(tabs.map((tab) => tab.shown.mock.calls.length)).toEqual(Array(7).fill(1));
    // The initial catch-up invalidated hidden data too. Seed it again to
    // distinguish the scoped change below from that initial invalidation.
    tabs.forEach((tab) => tab.client.setQueryData(keys.ticket("acme/demo", "2"), {}));
    await flush(() => Source.all[0]!.change());
    for (const tab of tabs) {
      expect(tab.shown).toHaveBeenCalledTimes(2);
      expect(tab.client.getQueryState(keys.ticket("acme/demo", "2"))?.isInvalidated).toBe(false);
    }
    await flush(() => Source.all[0]!.reset());
    for (const tab of tabs) {
      expect(tab.shown).toHaveBeenCalledTimes(3);
      expect(tab.client.getQueryState(keys.ticket("acme/demo", "2"))?.isInvalidated).toBe(true);
    }
    await flush(() => Source.all[0]!.change("invalid JSON"));
    expect(tabs.map((tab) => tab.shown.mock.calls.length)).toEqual(Array(7).fill(4));
  });

  it("catches up a late joiner without refreshing existing tabs or opening another connection", async () => {
    const leader = tab();
    await flush(() => Source.all[0]!.open());
    const follower = tab();
    await flush();
    expect(Source.all).toHaveLength(1);
    expect(follower.shown).toHaveBeenCalledTimes(1);
    expect(leader.shown).toHaveBeenCalledTimes(1);
    expect(follower.result.current).toBe(false);
  });

  it("hands the connection to one waiting tab when its owner closes, covering the event gap", async () => {
    const leader = tab();
    const departing = tab();
    const followers = [tab(), tab()];
    await flush(() => Source.all[0]!.open());
    await flush(() => departing.unmount());
    expect(Source.all).toHaveLength(1);
    await flush(() => leader.unmount());
    expect(Source.all[0]!.closed).toBe(true);
    expect(Source.all).toHaveLength(2);
    await flush(() => Source.all[1]!.open());
    await flush(() => Source.all[1]!.change());
    expect(followers.map((tab) => tab.shown.mock.calls.length)).toEqual([3, 3]);
    expect(Source.all.filter((source) => !source.closed)).toHaveLength(1);
  });

  it("warns all tabs at the original outage deadline and clears warnings/refetches on reconnect", async () => {
    const tabs = [tab(), tab()];
    await flush(() => Source.all[0]!.open());
    await flush(() => Source.all[0]!.fail()); // CONNECTING: browser retries itself.
    await flush(() => vi.advanceTimersByTime(LOST_AFTER_MS - 1));
    const late = tab();
    tabs.push(late);
    await flush();
    await flush(() => Source.all[0]!.fail()); // repeated errors do not postpone warnings.
    expect(tabs.map((tab) => tab.result.current)).toEqual([false, false, false]);
    await flush(() => vi.advanceTimersByTime(1));
    expect(tabs.map((tab) => tab.result.current)).toEqual([true, true, true]);
    expect(Source.all).toHaveLength(1);
    await flush(() => Source.all[0]!.open());
    expect(tabs.map((tab) => tab.result.current)).toEqual([false, false, false]);
    expect(tabs.map((tab) => tab.shown.mock.calls.length)).toEqual([2, 2, 1]);
  });

  it("retries CLOSED streams with bounded backoff, resets it on open, and cancels retries on cleanup", async () => {
    const leader = tab();
    tab();
    await flush(() => Source.all[0]!.open());
    for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
      const count = Source.all.length;
      await flush(() => Source.all.at(-1)!.fail(Source.CLOSED));
      await flush(() => vi.advanceTimersByTime(delay - 1));
      expect(Source.all).toHaveLength(count);
      await flush(() => vi.advanceTimersByTime(1));
      expect(Source.all).toHaveLength(count + 1);
    }
    await flush(() => Source.all.at(-1)!.open());
    await flush(() => Source.all.at(-1)!.fail(Source.CLOSED));
    const count = Source.all.length;
    await flush(() => vi.advanceTimersByTime(1000));
    expect(Source.all).toHaveLength(count + 1);
    await flush(() => Source.all.at(-1)!.fail(Source.CLOSED));
    await flush(() => leader.unmount());
    const afterHandover = Source.all.length;
    await flush(() => vi.advanceTimersByTime(30000));
    expect(Source.all).toHaveLength(afterHandover);
  });

  it("keeps the outage deadline when an owner closes during reconnection", async () => {
    const owner = tab();
    const follower = tab();
    await flush(() => Source.all[0]!.open());
    await flush(() => Source.all[0]!.fail());
    await flush(() => vi.advanceTimersByTime(LOST_AFTER_MS - 1));
    await flush(() => owner.unmount());
    expect(Source.all).toHaveLength(2);
    await flush(() => vi.advanceTimersByTime(1));
    expect(follower.result.current).toBe(true);
    await flush(() => Source.all[1]!.open());
    expect(follower.result.current).toBe(false);
    expect(follower.shown).toHaveBeenCalledTimes(2);
  });

  it("warns even when the initial connection has never opened", async () => {
    const tabs = [tab(), tab()];
    await flush(() => vi.advanceTimersByTime(LOST_AFTER_MS));
    expect(tabs.map((tab) => tab.result.current)).toEqual([true, true]);
    await flush(() => Source.all[0]!.open());
    expect(tabs.map((tab) => tab.result.current)).toEqual([false, false]);
  });

  it("does not connect if cleanup wins the race with a queued lock grant", async () => {
    let grant: () => Promise<void> = async () => {};
    vi.stubGlobal("navigator", { locks: {
      request: (_name: string, _options: LockOptions, callback: () => Promise<void>) => {
        grant = callback;
        return Promise.resolve();
      },
    } });
    const page = tab();
    page.unmount();
    await act(async () => { await grant(); });
    expect(Source.all).toHaveLength(0);
  });

  it("releases on pagehide and rejoins once on pageshow with a catch-up refetch", async () => {
    const page = tab();
    await flush(() => Source.all[0]!.open());
    await flush(() => window.dispatchEvent(new Event("pagehide")));
    expect(Source.all[0]!.closed).toBe(true);
    expect(Channel.all.size).toBe(0);
    await flush(() => window.dispatchEvent(new Event("pageshow")));
    await flush(() => window.dispatchEvent(new Event("pageshow")));
    expect(Source.all).toHaveLength(2);
    await flush(() => Source.all[1]!.open());
    expect(page.shown).toHaveBeenCalledTimes(2);
  });

  it("cancels waiting and active subscriptions on disable, including StrictMode effect replay", async () => {
    const owner = tab(true, true);
    const follower = tab();
    await flush();
    expect(Source.all.filter((source) => !source.closed)).toHaveLength(1);
    await flush(() => follower.rerender({ enabled: false }));
    await flush(() => owner.rerender({ enabled: false }));
    expect(Source.all.every((source) => source.closed)).toBe(true);
    expect(Channel.all.size).toBe(0);
    await flush(() => vi.advanceTimersByTime(60000));
    expect(follower.result.current).toBe(false);
    await flush(() => follower.rerender({ enabled: true }));
    await flush(() => Source.all.at(-1)!.open());
    expect(follower.shown).toHaveBeenCalledTimes(1);
  });
});
