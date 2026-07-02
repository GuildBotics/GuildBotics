import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ActivityHistoryPage,
  activityLinkHref,
  activityRange,
  matchActivityHistory,
} from "./ActivityHistory";
import { getActivityHistory } from "../api/client";
import type { ActivityHistoryResponse } from "../api/client";
import "../i18n";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getActivityHistory: vi.fn(),
    memberAvatarUrl: (personId: string) => `http://avatar.test/${personId}`,
  };
});

const ACTIVITY_FIXTURE: ActivityHistoryResponse = {
  start: "2026-07-01T00:00:00Z",
  end: "2026-07-02T00:00:00Z",
  members: [
    {
      person_id: "alice",
      name: "Alice",
      person_type: "agent",
      roles: ["developer"],
    },
  ],
  sessions: [
    {
      trace_id: "trace-1",
      person_id: "alice",
      source: "manual",
      command: "workflows/ticket_driven_workflow",
      workflow: "",
      title: "workflows/ticket_driven_workflow",
      mode: "interactive",
      status: "success",
      started_at: "2026-07-01T01:00:00Z",
      ended_at: "2026-07-01T02:30:00Z",
      duration_seconds: 5400,
      links: [
        {
          kind: "doc",
          label: "Desktop API 仕様書",
          url: "",
        },
        {
          kind: "issue",
          label: "Issue #42",
          url: "https://github.com/owner/repo/issues/42",
        },
      ],
    },
  ],
  events: [
    {
      id: "event-1",
      timestamp: "2026-07-01T02:00:00Z",
      person_id: "",
      type: "pr_merge",
      title: "PR #7 Merged",
      detail: "Add activity",
      url: "https://github.com/owner/repo/pull/7",
      links: [
        {
          kind: "pull_request",
          label: "PR #7",
          url: "https://github.com/owner/repo/pull/7",
        },
      ],
    },
    {
      id: "event-2",
      timestamp: "2026-07-01T02:10:00Z",
      person_id: "alice",
      type: "push",
      title: "Improve activity history event context",
      detail: "refs/heads/feature",
      url: "",
      links: [],
    },
  ],
  unsupported_event_sources: [],
};

beforeEach(() => {
  vi.mocked(getActivityHistory).mockResolvedValue(ACTIVITY_FIXTURE);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ActivityHistoryPage", () => {
  it("renders sessions and recorded GitHub events", async () => {
    renderActivity();

    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(await screen.findByText("workflows/ticket_driven_workflow")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "PR #7 Merged" })).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Improve activity history event context" }),
    ).toBeInTheDocument();
  });

  it("dims nonmatching activity and highlights matching activity by query", async () => {
    const user = userEvent.setup();
    renderActivity();

    const session = await screen.findByRole("button", {
      name: "workflows/ticket_driven_workflow",
    });
    const event = await screen.findByRole("button", { name: "PR #7 Merged" });
    await user.type(screen.getByLabelText("Search activity"), "PR #7");

    expect(session).toHaveClass("activity-filtered-out");
    expect(event).toHaveClass("activity-highlighted");
  });

  it("stacks sessions in day columns in week view", async () => {
    const user = userEvent.setup();
    renderActivity();

    await screen.findByRole("button", { name: "workflows/ticket_driven_workflow" });
    await user.click(screen.getByText("1 week"));

    expect(screen.getByRole("button", { name: "workflows/ticket_driven_workflow" })).toHaveClass(
      "activity-session-week",
    );
  });

  it("hides member event pins in week view", async () => {
    const user = userEvent.setup();
    renderActivity();

    expect(
      await screen.findByRole("button", { name: "Improve activity history event context" }),
    ).toBeInTheDocument();
    await user.click(screen.getByText("1 week"));

    expect(screen.queryByRole("button", { name: "Improve activity history event context" })).toBe(
      null,
    );
    expect(screen.getByRole("button", { name: "PR #7 Merged" })).toBeInTheDocument();
  });
});

describe("activityRange", () => {
  it("returns a Monday-starting week range", () => {
    const range = activityRange(new Date(2026, 6, 1), "week");

    expect(range.start.getDay()).toBe(1);
    expect(range.end.getTime() - range.start.getTime()).toBe(7 * 24 * 60 * 60 * 1000);
  });
});

describe("activityLinkHref", () => {
  it("uses normalized backend link urls consistently", () => {
    expect(
      activityLinkHref({
        kind: "pull_request",
        label: "PR #240 Activity history",
        url: "https://github.com/owner/repo/pull/240",
      }),
    ).toBe("https://github.com/owner/repo/pull/240");
    expect(activityLinkHref({ kind: "doc", label: "Memory note", url: "" })).toBe(null);
  });
});

describe("matchActivityHistory", () => {
  it("matches linked GitHub issue text", () => {
    const matches = matchActivityHistory(ACTIVITY_FIXTURE, "#42");

    expect(matches.sessionIds.has("trace-1")).toBe(true);
    expect(matches.eventIds.has("event-1")).toBe(false);
  });

  it("matches linked docs text", () => {
    const matches = matchActivityHistory(ACTIVITY_FIXTURE, "Desktop API");

    expect(matches.sessionIds.has("trace-1")).toBe(true);
  });
});

function renderActivity() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const theme = createTheme({
    primaryColor: "dark",
    defaultRadius: "md",
  });
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={queryClient}>
        <ActivityHistoryPage />
      </QueryClientProvider>
    </MantineProvider>,
  );
}
