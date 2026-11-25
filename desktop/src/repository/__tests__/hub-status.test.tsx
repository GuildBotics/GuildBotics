import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useHubStatus } from "../../HubStatus";
import { act, renderHook, screen } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { fakeApi, json, renderApp, SESSION } from "../test/fakeApi";

it.each([true, false])(
  "shows Hub maintenance instead of the standalone stop message: %s",
  async (embedded) => {
    fakeApi({
      "GET /api/v1/session": json(200, SESSION),
      "GET /api/v1/workspaces": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/members": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/admin/diagnostics": json(200, {
        runner: { state: "stopping", mode: "auto", detail: "" },
      }),
      "GET /-/guildbotics/api/hub-status": json(200, {
        current: {
          sha: "abcdef012345",
          subject: "Build title",
          url: "https://example.com/commit/abcdef012345",
        },
        target: null,
        operation: embedded
          ? {
              kind: "update",
              phase: "draining",
              since: "one",
              waiting: { runs: 3, ci_jobs: 2 },
            }
          : null,
      }),
    });
    renderApp("/", "en");
    if (embedded) {
      expect(await screen.findByText(/3 running tasks and 2 CI jobs/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "abcdef01" })).toHaveAttribute(
        "href",
        "https://example.com/commit/abcdef012345",
      );
      expect(screen.queryByText(/GuildBotics is stopping/)).not.toBeInTheDocument();
    } else {
      expect(await screen.findByText(/GuildBotics is stopping/)).toBeInTheDocument();
    }
  },
);

it("polls Hub status faster during maintenance", async () => {
  vi.useFakeTimers();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let operation: unknown = null;
  const calls = fakeApi({
    "GET /-/guildbotics/api/hub-status": () => json(200, { operation }),
  });
  try {
    renderHook(() => useHubStatus(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(calls.requests).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(29999);
    });
    expect(calls.requests).toHaveLength(2);
    operation = { kind: "update", phase: "preparing" };
    await act(async () => {
      client.setQueryData(["hub-status"], { operation });
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(calls.requests).toHaveLength(3);
    operation = null;
    await act(async () => {
      client.setQueryData(["hub-status"], { operation });
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(calls.requests).toHaveLength(3);
  } finally {
    client.clear();
    vi.useRealTimers();
  }
});
