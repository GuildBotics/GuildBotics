import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import {
  ActivityHistoryPage,
  activityBlockExecutionUrl,
  activityLinkHref,
  activityRange,
  blockModes,
  blockHoverCardPosition,
  buildActivityBlocks,
  matchActivityHistory,
  orderedActivityLinks,
  stackedEventTops,
  weekRowMinHeight,
} from "./ActivityHistory";
import {
  getActivityHistory,
  getCliAgentDetections,
  getCliAgentUsage,
  getIntelligenceConfig,
} from "../api/client";
import type { ActivityHistoryResponse } from "../api/client";
import i18n from "../i18n";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getActivityHistory: vi.fn(),
    getCliAgentDetections: vi.fn(),
    getCliAgentUsage: vi.fn(),
    getIntelligenceConfig: vi.fn(),
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
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-07-01T12:00:00Z"));
  vi.mocked(getActivityHistory).mockResolvedValue(ACTIVITY_FIXTURE);
  vi.mocked(getIntelligenceConfig).mockResolvedValue({
    config_dir: "",
    person_id: "alice",
    inherited: false,
    model_mapping: {},
    models: [],
    cli_agent_mapping: { default: "claude-code.yml" },
    cli_agents: [],
    brain_mapping: [],
    native_agent_policy: {
      codex: { filesystem_access: "workspace" },
    },
  });
  vi.mocked(getCliAgentDetections).mockResolvedValue({
    agents: [
      {
        name: "claude-code",
        label: "Claude Code",
        executable: "claude",
        config_reference: "claude-code.yml",
        detected: true,
        path: "",
      },
    ],
  });
  vi.mocked(getCliAgentUsage).mockResolvedValue({ usages: [] });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
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
    expect(document.querySelector(".lucide-history")).toBeInTheDocument();
    expect(getActivityHistory).toHaveBeenCalledWith(expect.objectContaining({ refresh: true }));
  });

  it("renders issue creation with an issue icon and issue details", async () => {
    const user = userEvent.setup();
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      events: [
        {
          id: "issue-create",
          timestamp: "2026-07-01T02:00:00Z",
          person_id: "alice",
          type: "issue_create",
          title: "Issue #43 Created",
          detail: "Track issue activity",
          url: "https://github.com/owner/repo/issues/43",
          links: [
            {
              kind: "issue",
              label: "Issue #43",
              url: "https://github.com/owner/repo/issues/43",
            },
          ],
        },
      ],
    });
    renderActivity();

    const pin = await screen.findByRole("button", { name: "Issue #43 Created" });
    expect(pin.querySelector(".lucide-circle-dot")).toBeInTheDocument();
    await user.hover(pin);

    expect(await screen.findByText(i18n.t("activity.eventTypes.issue_create"))).toBeInTheDocument();
    expect(screen.getByText("Track issue activity")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Issue #43" })).toHaveAttribute(
      "href",
      "https://github.com/owner/repo/issues/43",
    );
  });

  it("shows each member's AI CLI tool under the name and Human for human members", async () => {
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      members: [
        { person_id: "alice", name: "Alice", person_type: "agent", roles: ["developer"] },
        { person_id: "bob", name: "Bob", person_type: "human", roles: ["designer"] },
      ],
    });
    renderActivity();

    expect(await screen.findByText("Claude Code")).toBeInTheDocument();
    expect(await screen.findByText("Human")).toBeInTheDocument();
    // The member role is no longer rendered.
    expect(screen.queryByText("developer")).toBe(null);
    expect(screen.queryByText("designer")).toBe(null);
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

  it("marks a merged mixed-mode block with the mixed style", async () => {
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      events: [],
      sessions: [
        {
          ...ACTIVITY_FIXTURE.sessions[0],
          trace_id: "wf",
          title: "First task",
          mode: "workflow",
          started_at: "2026-07-01T12:05:00Z",
          ended_at: "2026-07-01T12:20:00Z",
        },
        {
          ...ACTIVITY_FIXTURE.sessions[0],
          trace_id: "chat",
          title: "Second task",
          mode: "interactive",
          started_at: "2026-07-01T12:30:00Z",
          ended_at: "2026-07-01T12:45:00Z",
        },
      ],
    });
    renderActivity();

    const bar = await screen.findByRole("button", { name: "First task +1" });
    expect(bar).toHaveClass("activity-session-mixed");
  });

  it("marks rate limited sessions and shows active member rate limits", async () => {
    const user = userEvent.setup();
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      events: [],
      sessions: [
        {
          ...ACTIVITY_FIXTURE.sessions[0],
          trace_id: "rate",
          title: "Slack thread",
          mode: "workflow",
          status: "rate_limited",
          rate_limit: {
            retry_after_at: "2999-07-01T03:30:00Z",
            retry_after_text: "3:30 AM",
          },
        },
      ],
    });
    renderActivity();

    const bar = await screen.findByRole("button", { name: /Rate limited: Slack thread/ });
    expect(bar).toHaveClass("activity-session-rate-limited");
    expect(bar.querySelector(".activity-session-alert-icon")).not.toBe(null);
    const memberRateLimit = (await screen.findByText("Rate limited")).closest(
      ".activity-member-rate-limit",
    );
    expect(memberRateLimit).not.toBe(null);
    expect(memberRateLimit).toHaveTextContent("7/1");

    await user.hover(bar);

    expect((await screen.findAllByText("Rate limited")).length).toBeGreaterThan(0);
    expect(await screen.findByText(/Reset:/)).toBeInTheDocument();
  });

  it("clears the member rate limit once a later session succeeds", async () => {
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      events: [],
      sessions: [
        {
          ...ACTIVITY_FIXTURE.sessions[0],
          trace_id: "rate",
          title: "Slack thread",
          mode: "workflow",
          status: "rate_limited",
          started_at: "2026-07-01T01:00:00Z",
          ended_at: "2026-07-01T02:30:00Z",
          rate_limit: {
            retry_after_at: "2999-07-01T03:30:00Z",
            retry_after_text: "3:30 AM",
          },
        },
        {
          ...ACTIVITY_FIXTURE.sessions[0],
          trace_id: "recovered",
          title: "Recovered task",
          mode: "workflow",
          status: "success",
          started_at: "2026-07-01T03:00:00Z",
          ended_at: "2026-07-01T03:20:00Z",
        },
      ],
    });
    renderActivity();

    // The historical session keeps its rate-limited styling, but the member
    // header no longer claims the limit is active.
    const bar = await screen.findByRole("button", { name: /Rate limited: Slack thread/ });
    expect(bar).toHaveClass("activity-session-rate-limited");
    expect(document.querySelector(".activity-member-rate-limit")).toBe(null);
  });

  function mockCodexMember(usage: {
    windows: {
      window: string;
      used_percent: number;
      resets_at: string;
      window_minutes: number | null;
    }[];
    limit_reached: boolean;
  }) {
    vi.mocked(getIntelligenceConfig).mockResolvedValue({
      config_dir: "",
      person_id: "alice",
      inherited: false,
      model_mapping: {},
      models: [],
      cli_agent_mapping: { default: "codex-cli.yml" },
      cli_agents: [],
      brain_mapping: [],
      native_agent_policy: {
        codex: { filesystem_access: "workspace" },
      },
    });
    vi.mocked(getCliAgentUsage).mockResolvedValue({
      usages: [{ agent: "codex", checked_at: "2026-07-01T11:59:00Z", ...usage }],
    });
  }

  it("shows usage meters for members whose AI CLI tool reports usage", async () => {
    mockCodexMember({
      windows: [
        {
          window: "primary",
          used_percent: 42,
          resets_at: "2026-07-01T14:00:00Z",
          window_minutes: 300,
        },
        {
          window: "secondary",
          used_percent: 78,
          resets_at: "2026-07-04T09:00:00Z",
          window_minutes: 10080,
        },
      ],
      limit_reached: false,
    });
    renderActivity();

    expect(await screen.findByRole("meter", { name: "5h 42%" })).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "1w 78%" })).toBeInTheDocument();
    // Same-day resets show only the time inline to fit the narrow member
    // cell; the full timestamp lives in the row tooltip.
    expect(screen.getByText(/42%/)).not.toHaveTextContent("7/1");
    expect(screen.getByText(/42%/)).toHaveTextContent(/42% · \d{1,2}:\d{2}/);
    expect(screen.getByText(/42%/).closest(".activity-member-usage-row")).toHaveAttribute(
      "title",
      expect.stringContaining("7/1"),
    );
    // Later resets show only the date inline.
    expect(screen.getByText(/78%/)).toHaveTextContent("7/4");
    expect(screen.getByText(/78%/)).not.toHaveTextContent(/\d{1,2}:\d{2}/);
    expect(document.querySelector(".activity-member-rate-limit")).toBe(null);
  });

  it("prefers measured usage over stale rate-limit events for the member badge", async () => {
    mockCodexMember({
      windows: [
        {
          window: "primary",
          used_percent: 12,
          resets_at: "2026-07-01T14:00:00Z",
          window_minutes: 300,
        },
      ],
      limit_reached: false,
    });
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      events: [],
      sessions: [
        {
          ...ACTIVITY_FIXTURE.sessions[0],
          trace_id: "rate",
          title: "Slack thread",
          mode: "workflow",
          status: "rate_limited",
          rate_limit: {
            retry_after_at: "2999-07-01T03:30:00Z",
            retry_after_text: "3:30 AM",
          },
        },
      ],
    });
    renderActivity();

    await screen.findByRole("meter", { name: "5h 12%" });
    expect(document.querySelector(".activity-member-rate-limit")).toBe(null);
  });

  it("shows the member badge when measured usage reports the limit reached", async () => {
    mockCodexMember({
      windows: [
        {
          window: "primary",
          used_percent: 100,
          resets_at: "2026-07-01T13:17:00Z",
          window_minutes: 300,
        },
        {
          window: "secondary",
          used_percent: 64,
          resets_at: "2026-07-04T09:00:00Z",
          window_minutes: 10080,
        },
      ],
      limit_reached: true,
    });
    renderActivity();

    const memberRateLimit = (await screen.findByText("Rate limited")).closest(
      ".activity-member-rate-limit",
    );
    expect(memberRateLimit).not.toBe(null);
    expect(memberRateLimit).toHaveTextContent("7/1");
  });

  it.each([
    ["retry_scheduled", "Retry scheduled", "activity-session-status-retry_scheduled"],
    ["abandoned", "Abandoned", "activity-session-status-abandoned"],
    ["incomplete", "Incomplete", "activity-session-status-incomplete"],
  ] as const)(
    "shows a %s badge and tinted bar for a dispatch/completion status session",
    async (status, label, cssClass) => {
      vi.mocked(getActivityHistory).mockResolvedValue({
        ...ACTIVITY_FIXTURE,
        events: [],
        sessions: [
          {
            ...ACTIVITY_FIXTURE.sessions[0],
            trace_id: `session-${status}`,
            title: "Slack thread",
            mode: "workflow",
            status,
          },
        ],
      });
      renderActivity();

      const bar = await screen.findByRole("button", { name: new RegExp(`${label}: Slack thread`) });
      expect(bar).toHaveClass(cssClass);
      expect(bar.querySelector(".activity-session-alert-icon")).not.toBe(null);

      const user = userEvent.setup({ delay: null });
      await user.hover(bar);

      expect((await screen.findAllByText(label)).length).toBeGreaterThan(0);
    },
  );

  it("does not mark an ordinary successful session with a status alert", async () => {
    renderActivity();

    const bar = await screen.findByRole("button", { name: "workflows/ticket_driven_workflow" });
    expect(bar).not.toHaveClass("activity-session-status-retry_scheduled");
    expect(bar).not.toHaveClass("activity-session-status-abandoned");
    expect(bar).not.toHaveClass("activity-session-status-incomplete");
    expect(bar.querySelector(".activity-session-alert-icon")).toBe(null);
  });

  it("drops the current-time line in week view but keeps it in day view", async () => {
    const user = userEvent.setup();
    const { container } = renderActivity();

    await screen.findByRole("button", { name: "workflows/ticket_driven_workflow" });
    expect(container.querySelectorAll(".activity-now-line").length).toBeGreaterThan(0);

    await user.click(screen.getByText("1 week"));

    expect(container.querySelector(".activity-now-line")).toBe(null);
  });

  it("hides all event pins in week view", async () => {
    const user = userEvent.setup();
    renderActivity();

    expect(
      await screen.findByRole("button", { name: "Improve activity history event context" }),
    ).toBeInTheDocument();
    await user.click(screen.getByText("1 week"));

    expect(screen.queryByRole("button", { name: "Improve activity history event context" })).toBe(
      null,
    );
    expect(screen.queryByRole("button", { name: "PR #7 Merged" })).toBe(null);
  });
});

describe("activityRange", () => {
  it("returns a Monday-starting week range", () => {
    const range = activityRange(new Date(2026, 6, 1), "week");

    expect(range.start.getDay()).toBe(1);
    expect(range.end.getTime() - range.start.getTime()).toBe(7 * 24 * 60 * 60 * 1000);
  });
});

describe("stackedEventTops", () => {
  it("stacks near-simultaneous mixed-offset timestamps in chronological order", () => {
    const tops = stackedEventTops(
      [
        { ...ACTIVITY_FIXTURE.events[0], id: "later", timestamp: "2026-07-01T01:01:00Z" },
        { ...ACTIVITY_FIXTURE.events[0], id: "first", timestamp: "2026-07-01T10:00:30+09:00" },
      ],
      10,
    );

    expect(tops.get("first")).toBe(10);
    expect(tops.get("later")).toBe(28);
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
    expect(
      activityLinkHref({
        kind: "doc",
        label: "Memory note",
        url: "/diagnostics?tab=memory&doc_id=doc-1",
      }),
    ).toBe("/diagnostics?tab=memory&doc_id=doc-1");
    expect(activityLinkHref({ kind: "doc", label: "Memory note", url: "" })).toBe(null);
  });
});

describe("orderedActivityLinks", () => {
  it("orders links by ascending timestamp so newer items appear lower", () => {
    const links = orderedActivityLinks([
      {
        kind: "doc",
        label: "Newer memory",
        url: "/diagnostics?tab=memory&doc_id=new",
        timestamp: "2026-07-01T10:05:00Z",
      },
      {
        kind: "commit",
        label: "Older commit",
        url: "https://github.com/owner/repo/commit/abc",
        timestamp: "2026-07-01T10:00:00Z",
      },
      {
        kind: "pull_request",
        label: "Middle PR",
        url: "https://github.com/owner/repo/pull/1",
        timestamp: "2026-07-01T10:03:00Z",
      },
    ]);

    expect(links.map((link) => link.label)).toEqual(["Older commit", "Middle PR", "Newer memory"]);
  });
});

describe("buildActivityBlocks", () => {
  it("rounds display ranges to enclosing hourly slots", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "short",
        started_at: "2026-07-01T12:31:00Z",
        ended_at: "2026-07-01T12:34:00Z",
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].display_started_at).toBe("2026-07-01T12:00:00.000Z");
    expect(blocks[0].display_ended_at).toBe("2026-07-01T13:00:00.000Z");
    expect(blocks[0].started_at).toBe("2026-07-01T12:31:00Z");
    expect(blocks[0].ended_at).toBe("2026-07-01T12:34:00Z");
  });

  it("merges sessions whose rounded display ranges overlap", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "first",
        title: "First task",
        started_at: "2026-07-01T12:31:00Z",
        ended_at: "2026-07-01T12:34:00Z",
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "second",
        title: "Second task",
        started_at: "2026-07-01T12:55:00Z",
        ended_at: "2026-07-01T13:08:00Z",
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].display_started_at).toBe("2026-07-01T12:00:00.000Z");
    expect(blocks[0].display_ended_at).toBe("2026-07-01T14:00:00.000Z");
    expect(blocks[0].title).toBe("First task +1");
    expect(blocks[0].sessions.map((session) => session.trace_id)).toEqual(["first", "second"]);
  });

  it("labels a merged block by the meaningful title, not a command fallback", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "setup",
        command: "guildbotics member context",
        title: "guildbotics member context",
        started_at: "2026-07-01T12:31:00Z",
        ended_at: "2026-07-01T12:34:00Z",
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "work",
        command: "guildbotics member context",
        title: "PR #246 Slack event filter",
        started_at: "2026-07-01T12:55:00Z",
        ended_at: "2026-07-01T13:08:00Z",
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].title).toBe("PR #246 Slack event filter +1");
  });

  it("dedupes the same PR url across merged sessions with different labels", () => {
    const url = "https://github.com/o/r/pull/246";
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "first",
        started_at: "2026-07-01T12:31:00Z",
        ended_at: "2026-07-01T12:34:00Z",
        links: [{ kind: "pull_request", label: "PR #246", url }],
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "second",
        started_at: "2026-07-01T12:55:00Z",
        ended_at: "2026-07-01T13:08:00Z",
        links: [{ kind: "pull_request", label: "PR #246 note のタイトル", url }],
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].links).toEqual([{ kind: "pull_request", label: "PR #246", url }]);
  });

  it("prefers the canonical PR label even when it merges in after a memory-source label", () => {
    const url = "https://github.com/o/r/pull/246";
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "first",
        started_at: "2026-07-01T12:31:00Z",
        ended_at: "2026-07-01T12:34:00Z",
        links: [{ kind: "pull_request", label: "PR #246 note のタイトル", url }],
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "second",
        started_at: "2026-07-01T12:55:00Z",
        ended_at: "2026-07-01T13:08:00Z",
        links: [{ kind: "pull_request", label: "PR #246", url }],
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].links).toEqual([{ kind: "pull_request", label: "PR #246", url }]);
  });

  it("keeps sessions separate when rounded display ranges only touch", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "first",
        title: "First task",
        started_at: "2026-07-01T12:31:00Z",
        ended_at: "2026-07-01T12:34:00Z",
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "second",
        title: "Second task",
        started_at: "2026-07-01T13:01:00Z",
        ended_at: "2026-07-01T13:08:00Z",
      },
    ]);

    expect(blocks).toHaveLength(2);
    expect(blocks.map((block) => block.title)).toEqual(["First task", "Second task"]);
  });

  it("merges mixed-offset timestamps by instant instead of raw string order", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "first",
        title: "First task",
        started_at: "2026-07-01T09:00:00+09:00",
        ended_at: "2026-07-01T09:15:00+09:00",
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "second",
        title: "Second task",
        started_at: "2026-07-01T00:30:00Z",
        ended_at: "2026-07-01T00:45:00Z",
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].started_at).toBe("2026-07-01T09:00:00+09:00");
    expect(blocks[0].ended_at).toBe("2026-07-01T00:45:00Z");
  });

  it("marks a merged block as mixed when session modes differ", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "wf",
        mode: "workflow",
        started_at: "2026-07-01T12:05:00Z",
        ended_at: "2026-07-01T12:20:00Z",
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "chat",
        mode: "interactive",
        started_at: "2026-07-01T12:30:00Z",
        ended_at: "2026-07-01T12:45:00Z",
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].mode).toBe("mixed");
    expect(blockModes(blocks[0].sessions)).toEqual(["workflow", "interactive"]);
  });

  it("keeps the strongest status alert (abandoned) when merging sessions", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "retry",
        status: "retry_scheduled",
        started_at: "2026-07-01T12:05:00Z",
        ended_at: "2026-07-01T12:20:00Z",
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "abandoned",
        status: "abandoned",
        started_at: "2026-07-01T12:30:00Z",
        ended_at: "2026-07-01T12:45:00Z",
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].status_alert).toBe("abandoned");
  });

  it("has no status alert when no merged session carries one", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "ok-1",
        status: "success",
        started_at: "2026-07-01T12:05:00Z",
        ended_at: "2026-07-01T12:20:00Z",
      },
    ]);

    expect(blocks[0].status_alert).toBe(null);
  });

  it("keeps a single mode when merged sessions share it", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "wf-1",
        mode: "workflow",
        started_at: "2026-07-01T12:05:00Z",
        ended_at: "2026-07-01T12:20:00Z",
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "wf-2",
        mode: "workflow",
        started_at: "2026-07-01T12:30:00Z",
        ended_at: "2026-07-01T12:45:00Z",
      },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].mode).toBe("workflow");
    expect(blockModes(blocks[0].sessions)).toEqual(["workflow"]);
  });
});

describe("blockHoverCardPosition", () => {
  it("opens week-view cards to the already-scanned left side", () => {
    expect(blockHoverCardPosition("week")).toBe("left-start");
  });

  it("keeps day-view cards below the bar", () => {
    expect(blockHoverCardPosition("day")).toBe("bottom");
  });
});

describe("weekRowMinHeight", () => {
  it("keeps the base minimum height for empty or sparse rows", () => {
    expect(weekRowMinHeight(0)).toBe(86);
    expect(weekRowMinHeight(1)).toBe(86);
    expect(weekRowMinHeight(2)).toBe(86);
  });

  it("fits the stacked bars exactly once they exceed the base height", () => {
    // Mirrors .activity-week-day geometry: 24px bars, 4px gaps, 6px padding.
    expect(weekRowMinHeight(3)).toBe(3 * 24 + 2 * 4 + 2 * 6); // 92
    expect(weekRowMinHeight(4)).toBe(4 * 24 + 3 * 4 + 2 * 6); // 120
  });
});

describe("activityBlockExecutionUrl", () => {
  it("builds a composite diagnostics URL from merged block trace ids", () => {
    const blocks = buildActivityBlocks([
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "first",
        started_at: "2026-07-01T12:31:00Z",
        ended_at: "2026-07-01T12:34:00Z",
      },
      {
        ...ACTIVITY_FIXTURE.sessions[0],
        trace_id: "second",
        started_at: "2026-07-01T12:55:00Z",
        ended_at: "2026-07-01T13:08:00Z",
      },
    ]);

    expect(activityBlockExecutionUrl(blocks[0])).toBe(
      "/diagnostics?tab=executions&trace_ids=first%2Csecond",
    );
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

  it("matches member names", () => {
    const matches = matchActivityHistory(ACTIVITY_FIXTURE, "Alice");

    expect(matches.sessionIds.has("trace-1")).toBe(true);
    expect(matches.eventIds.has("event-2")).toBe(true);
    expect(matches.eventIds.has("event-1")).toBe(false);
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
        <MemoryRouter>
          <ActivityHistoryPage />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}
