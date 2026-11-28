import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HubStatusBand, type HubStatus } from "./ui/HubStatus";
import { TestMantineProvider } from "./test/TestMantineProvider";
import i18n from "./i18n";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { HubUpdateNotice, useHubStatus } from "./HubStatus";
import { request } from "./api/client";
vi.mock("./api/client", async (original) => ({
  ...(await original<typeof import("./api/client")>()),
  request: vi.fn(),
}));
const status: HubStatus = {
  current: {
    sha: "1234567890",
    tag: null,
    subject: "Current title",
    url: "https://example.com/commit/1234567890",
  },
  target: { sha: "abcdef0123", subject: "Next title", url: null },
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
    renderHook(useHubStatus, {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
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
  vi.mocked(request).mockReset().mockResolvedValue({});
  render(
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <HubUpdateNotice update={update} />
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

it.each([
  ["en", "update", "Automatic Hub update failed", "Stage: Applying the update"],
  ["ja", "update", "Hub の自動更新に失敗しました", "止まった段階：更新の適用"],
  ["ja", "requirements", "Hub の自動更新に失敗しました", "止まった段階：保存形式と手作業の確認"],
] as const)(
  "shows the translated stage and log details for a failed update in %s",
  async (language, stageCode, title, stage) => {
    await i18n.changeLanguage(language);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const update = {
      kind: "failed",
      sha: "c".repeat(40),
      stage: stageCode,
      reason: "The new build did not start.",
      next: "Run recover.",
    } as const;
    render(
      <QueryClientProvider client={client}>
        <TestMantineProvider>
          <HubUpdateNotice update={update} />
        </TestMantineProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText(title)).toBeTruthy();
    expect(screen.getByText(stage)).toBeTruthy();
    expect(screen.getByText(update.reason)).toBeTruthy();
    expect(screen.getByText(i18n.t("hub.failureNext", { next: update.next }))).toBeTruthy();
    expect(screen.getByText(i18n.t("hub.failureDetails"))).toBeTruthy();
    client.clear();
  },
);
