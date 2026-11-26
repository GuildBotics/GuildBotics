import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HubStatusBand, type HubStatus } from "./ui/HubStatus";
import { TestMantineProvider } from "./test/TestMantineProvider";
import i18n from "./i18n";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, waitFor } from "@testing-library/react";
import { HubUpdateNotice, HubVersion } from "./HubStatus";
import { request } from "./api/client";
vi.mock("./api/client", async (original) => ({
  ...(await original<typeof import("./api/client")>()),
  request: vi.fn(),
}));
const status: HubStatus = {
  current: {
    sha: "1234567890",
    subject: "Current title",
    url: "https://example.com/commit/1234567890",
  },
  target: { sha: "abcdef0123", subject: "Next title", url: null },
  releases: null,
  operation: { kind: "update", phase: "preparing", since: "one", waiting: { runs: 3, ci_jobs: 2 } },
};
describe("Hub maintenance banner", () => {
  it("shows versions and links, and returns when the phase changes", () => {
    const t = i18n.getFixedT("en");
    const view = (value: HubStatus) => (
      <TestMantineProvider>
        <HubStatusBand status={value} t={t} />
      </TestMantineProvider>
    );
    const { rerender } = render(view(status));
    expect(screen.getByText(/Preparing an automatic/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "12345678" }).getAttribute("href")).toBe(
      status.current?.url,
    );
    expect(screen.queryByRole("link", { name: "abcdef01" })).toBeNull();
    expect(screen.getByText(/Next title/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText(/Preparing an automatic/)).toBeNull();
    rerender(
      view({ ...status, operation: { ...status.operation!, phase: "draining", since: "two" } }),
    );
    expect(screen.getByText(/3 running tasks and 2 CI jobs/)).toBeTruthy();
  });
  for (const language of ["ja", "en"]) {
    for (const kind of ["update", "restart", "stop"] as const) {
      it(`translates ${kind} in ${language}`, () => {
        const t = i18n.getFixedT(language);
        render(
          <TestMantineProvider>
            <HubStatusBand
              status={{
                ...status,
                current: null,
                target: null,
                operation: { ...status.operation!, kind, phase: "draining" },
              }}
              t={t}
            />
          </TestMantineProvider>,
        );
        expect(screen.getByText(t(`hub.${kind}`, { runs: 3, ci_jobs: 2 }))).toBeTruthy();
      });
    }
  }
});

it.each([true, false])(
  "shows the current build on the service screen only when known: %s",
  async (known) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.mocked(request).mockResolvedValue({ ...status, current: known ? status.current : null });
    render(
      <QueryClientProvider client={client}>
        <TestMantineProvider>
          <HubVersion />
        </TestMantineProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(client.getQueryState(["hub-status"])?.status).toBe("success"));
    expect(screen.queryByRole("link", { name: "12345678" }) !== null).toBe(known);
    client.clear();
  },
);

it.each([
  [
    ["release/2026-11-27-1", "release/2026-11-28-1"],
    /Releases not applied yet: release\/2026-11-28-1, release\/2026-11-27-1\./,
  ],
  [[], /No release is waiting to be applied\./],
] as const)("names the running and pending releases: %s", async (pending, text) => {
  await i18n.changeLanguage("en");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(request).mockResolvedValue({
    ...status,
    releases: {
      url: "https://example.com/releases",
      running: "release/2026-11-26-1",
      pending: [...pending].reverse().map((name) => ({ name, sha: "a".repeat(40), subject: "s" })),
    },
  });
  render(
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <HubVersion />
      </TestMantineProvider>
    </QueryClientProvider>,
  );
  expect(await screen.findByText(text)).toBeTruthy();
  expect(screen.getByText(/· release\/2026-11-26-1/)).toBeTruthy();
  expect(screen.getByRole("link", { name: "Releases" }).getAttribute("href")).toBe(
    "https://example.com/releases",
  );
  client.clear();
});

it.each(["en", "ja"])("delimits version subjects inside the banner in %s", (language) => {
  render(
    <TestMantineProvider>
      <HubStatusBand status={status} t={i18n.getFixedT(language)} />
    </TestMantineProvider>,
  );
  const banner = screen.getByText(/Current title/);
  expect(banner.textContent).toContain(
    language === "ja" ? "12345678（Current title）" : '12345678 ("Current title")',
  );
  expect(banner.textContent).toContain(
    language === "ja" ? "abcdef01（Next title）" : 'abcdef01 ("Next title")',
  );
});

it("polls Hub status slowly when idle and quickly only during maintenance", async () => {
  vi.useFakeTimers();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let value: HubStatus = { ...status, operation: null };
  vi.mocked(request).mockReset().mockResolvedValue(value);
  try {
    render(
      <QueryClientProvider client={client}>
        <TestMantineProvider>
          <HubVersion />
        </TestMantineProvider>
      </QueryClientProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(request).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(29999);
    });
    expect(request).toHaveBeenCalledTimes(2);
    value = status;
    vi.mocked(request).mockResolvedValue(value);
    await act(async () => {
      client.setQueryData(["hub-status"], value);
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(request).toHaveBeenCalledTimes(3);
    value = { ...status, operation: null };
    vi.mocked(request).mockResolvedValue(value);
    await act(async () => {
      client.setQueryData(["hub-status"], value);
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(request).toHaveBeenCalledTimes(3);
  } finally {
    client.clear();
    vi.useRealTimers();
  }
});

it("links every manual step and records completion for the displayed release", async () => {
  await i18n.changeLanguage("en");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const update = {
    kind: "manual",
    sha: "b".repeat(40),
    release: "release/2",
    steps: [1, 2].map((version) => ({
      version,
      ticket: `https://hub.example/tickets/${version}`,
    })),
    confirmed: false,
  } as const;
  vi.mocked(request)
    .mockReset()
    .mockImplementation(async (_, options) => {
      if (options?.method === "POST") return {};
      return { ...status, update };
    });
  render(
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <HubVersion />
      </TestMantineProvider>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Manual work is required for release/2")).toBeTruthy();
  for (const { ticket } of update.steps) {
    expect(screen.getByRole("link", { name: ticket }).getAttribute("href")).toBe(ticket);
  }
  fireEvent.click(screen.getByRole("button", { name: "Manual work is complete. Update the Hub" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Completion recorded");
  expect(request).toHaveBeenCalledWith("/hub-status/confirm", {
    method: "POST",
    body: { sha: update.sha },
  });
  client.clear();
});

it.each(["banner", "service"] as const)(
  "shows the stage, reason, and next action for a failed update in the %s",
  async (placement) => {
    await i18n.changeLanguage("en");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const update = {
      kind: "failed",
      sha: "c".repeat(40),
      stage: "update",
      reason: "The new build did not start.",
      next: "Run recover.",
    } as const;
    vi.mocked(request).mockResolvedValue({ ...status, operation: null, update });
    render(
      <QueryClientProvider client={client}>
        <TestMantineProvider>
          {placement === "banner" ? <HubUpdateNotice update={update} /> : <HubVersion />}
        </TestMantineProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Automatic Hub update failed")).toBeTruthy();
    expect(screen.getByText("Stage: update")).toBeTruthy();
    expect(screen.getByText(update.reason)).toBeTruthy();
    expect(screen.getByText(`Next: ${update.next}`)).toBeTruthy();
    client.clear();
  },
);
