import i18n from "../../i18n";
// Test helpers: a routed fetch double that records requests, and a renderer.

import { render } from "@testing-library/react";
import { vi } from "vitest";
import { EntryPage } from "../../session/EntryPage";
import { theme } from "../../ui/theme";
import { App } from "../../App";
import { BrowserRouter } from "react-router";
import { QueryClientProvider } from "@tanstack/react-query";
import { TestMantineProvider } from "../../test/TestMantineProvider";
import { createQueryClient } from "../query/hooks";

export interface Recorded {
  method: string;
  path: string;
  search: string;
  headers: Record<string, string>;
  body: unknown;
  credentials: RequestCredentials | undefined;
}

type Answer = { status?: number; body?: unknown };
type Reply = Answer | ((req: Recorded) => Answer | Promise<Answer>);

export function json(status: number, body: unknown) {
  return { status, body };
}

export function apiError(status: number, code: string, message: string) {
  return { status, body: { error: { code, message } } };
}

/**
 * Installs `fetch` answering "<METHOD> <decoded path>" routes. A route value
 * that is an array answers successive calls in turn (the last one repeats); a
 * function may answer later (a promise).
 */
export function fakeApi(routes: Record<string, Reply | Reply[]>) {
  const requests: Recorded[] = [];
  const counters = new Map<string, number>();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const headers = Object.fromEntries(
      Object.entries((init?.headers ?? {}) as Record<string, string>),
    );
    const rec: Recorded = {
      method: init?.method ?? "GET",
      path: decodeURIComponent(url.pathname),
      search: url.search,
      headers,
      // A file (a member's picture) is kept as it was sent.
      body:
        init?.body instanceof Blob
          ? init.body
          : init?.body
            ? JSON.parse(String(init.body))
            : undefined,
      credentials: init?.credentials,
    };
    requests.push(rec);
    const key = `${rec.method} ${rec.path}`;
    const route =
      routes[key] ??
      {
        "GET /-/guildbotics/api/session": json(200, { csrf: "csrf-123" }),
        "GET /-/guildbotics/api/config/status": json(200, { branvil_available: true }),
        "GET /-/guildbotics/api/hub-status": json(200, {}),
      }[key];
    if (route === undefined) {
      return new Response(
        JSON.stringify({ error: { code: "not_found", message: `no route ${key}` } }),
        { status: 404 },
      );
    }
    const n = counters.get(key) ?? 0;
    counters.set(key, n + 1);
    const pick = Array.isArray(route) ? (route[Math.min(n, route.length - 1)] as Reply) : route;
    const reply = typeof pick === "function" ? await pick(rec) : pick;
    const status = reply.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(reply.body ?? {}), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    requests,
    calls: (method: string, path: string) =>
      requests.filter((r) => r.method === method && r.path === path),
  };
}

export const ME = {
  name: "owner",
  display_name: "Owner",
  kind: "owner",
  accounts: [],
  reviewers: [],
};
export const SESSION = { me: ME, csrf: "csrf-123" };

export function renderApp(path: string, language: "en" | "ja" = "en") {
  void i18n.changeLanguage(language);
  window.history.replaceState(null, "", path);
  const client = createQueryClient();
  // Exercise the real retry policy without waiting for its 1s/2s backoff.
  const defaults = client.getDefaultOptions();
  client.setDefaultOptions({ ...defaults, queries: { ...defaults.queries, retryDelay: 0 } });
  const utils = render(
    <TestMantineProvider theme={theme}>
      <QueryClientProvider client={client}>
        <BrowserRouter>
          <EntryPage>
            <App />
          </EntryPage>
        </BrowserRouter>
      </QueryClientProvider>
    </TestMantineProvider>,
  );
  return { ...utils, client };
}
