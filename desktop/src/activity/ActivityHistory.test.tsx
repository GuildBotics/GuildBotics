import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

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
  getSchedulerStatus,
  getTraceDetail,
  getWorkspaceLive,
} from "../api/client";
import type {
  ActivityHistoryResponse,
  CliAgentUsageWindow,
  RuntimeActiveWork,
  RuntimeStatus,
  RuntimeUnitStatus,
  TraceRecord,
  WorkspaceLiveState,
} from "../api/client";
import i18n from "../i18n";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getActivityHistory: vi.fn(),
    getCliAgentDetections: vi.fn(),
    getCliAgentUsage: vi.fn(),
    getIntelligenceConfig: vi.fn(),
    getSchedulerStatus: vi.fn(),
    getTraceDetail: vi.fn(),
    getWorkspaceLive: vi.fn(),
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

function runtimeUnitStatus(): RuntimeUnitStatus {
  return {
    target: "scheduler",
    state: "running",
    running: true,
    started_at: null,
    stopped_at: null,
    error: null,
    max_consecutive_errors: null,
    routine_interval_minutes: null,
    active_member_count: null,
    worker_count: null,
    scheduled_source_enabled: null,
    routine_source_enabled: null,
    event_queue_source_enabled: null,
    subscription_count: null,
    listener_count: null,
    cycle_count: null,
    cycle_failure_count: null,
    events_drained_count: null,
    events_auth_failed_count: null,
    events_auth_failed_persons: [],
    member_routines: [],
  };
}

function runtimeStatus(activeWorks: RuntimeActiveWork[]): RuntimeStatus {
  return {
    scheduler: runtimeUnitStatus(),
    events: { ...runtimeUnitStatus(), target: "events" },
    active_works: activeWorks,
  };
}

const ACTIVE_WORK: RuntimeActiveWork = {
  id: "trace-live",
  source: "routine",
  person_id: "alice",
  command: "workflows/ticket_driven_workflow",
  started_at: "2026-07-01T11:58:00Z",
};

const ALICE_ROUTINE = {
  person_id: "alice",
  last_routine_at: "2026-07-01T11:49:00Z",
  next_routine_at: "2026-07-01T11:59:00Z",
};

const REMOTE_LIVE: WorkspaceLiveState = {
  schema_version: 1,
  workspace_id: "workspace-1",
  device_id: "device-2",
  publisher_id: "publisher-2",
  observed_at: "2026-07-01T11:59:30Z",
  status: "delayed",
  works: [
    {
      work_id: "remote-work",
      run_id: "remote-run",
      member_id: "alice",
      workflow_name: "workflows/ticket_driven_workflow",
      presentation: {
        label_key: "",
        label_fallback: "",
        message_key: "",
        message: "remote step",
        message_params: {},
        tone: "info",
        effort: "",
      },
      retry_at: null,
    },
  ],
};

function liveTraceRecord(message: string): TraceRecord {
  return {
    kind: "event",
    timestamp: "2026-07-01T11:59:00Z",
    trace_id: "trace-live",
    span_id: null,
    parent_id: null,
    call_id: null,
    span: "",
    source: "routine",
    person_id: "alice",
    command: "workflows/ticket_driven_workflow",
    workflow: "",
    type: "command.progress",
    level: "info",
    message,
    attributes: {},
    payload: {},
    presentation: {
      label_key: "",
      label_fallback: "Running",
      message_key: "",
      message,
      message_params: {},
      tone: "info",
      effort: "",
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-07-01T12:00:00Z"));
  vi.mocked(getActivityHistory).mockResolvedValue(ACTIVITY_FIXTURE);
  vi.mocked(getIntelligenceConfig).mockResolvedValue({
    config_dir: "",
    revisions: {},
    person_id: "alice",
    inherited: false,
    model_mapping: {},
    models: [],
    cli_agent_mapping: { default: "cli_agents/claude/default.yml" },
    cli_agents: [],
    brain_mapping: [],
    native_agent_policy: {
      codex: { filesystem_access: "workspace" },
      grok: { filesystem_access: "workspace" },
      copilot: { filesystem_access: "workspace" },
    },
  });
  vi.mocked(getCliAgentDetections).mockResolvedValue({
    agents: [
      {
        name: "claude",
        label: "Claude Code",
        executable: "claude",
        config_reference: "cli_agents/claude/default.yml",
        detected: true,
        path: "",
      },
    ],
  });
  vi.mocked(getCliAgentUsage).mockResolvedValue({ usages: [] });
  vi.mocked(getSchedulerStatus).mockResolvedValue(runtimeStatus([]));
  vi.mocked(getTraceDetail).mockResolvedValue({
    trace_id: "trace-live",
    summary: null,
    records: [],
  });
  vi.mocked(getWorkspaceLive).mockResolvedValue([]);
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
    expect(document.querySelector(".lucide-rotate-ccw-clock")).toBeInTheDocument();
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

  it("shows the member's running work as a link to its trace in diagnostics", async () => {
    vi.mocked(getSchedulerStatus).mockResolvedValue(runtimeStatus([ACTIVE_WORK]));
    vi.mocked(getTraceDetail).mockResolvedValue({
      trace_id: "trace-live",
      summary: null,
      records: [liveTraceRecord("first step"), liveTraceRecord("reviewing the pull request")],
    });
    renderActivity();

    const status = await screen.findByRole("link", { name: "Current work" });
    expect(status).toHaveTextContent("reviewing the pull request");
    expect(status).toHaveAttribute("href", "/diagnostics?tab=executions&trace_id=trace-live");
    expect(getTraceDetail).toHaveBeenCalledWith("trace-live");
  });

  it("falls back to the running command name until the trace reports records", async () => {
    vi.mocked(getSchedulerStatus).mockResolvedValue(runtimeStatus([ACTIVE_WORK]));
    renderActivity();

    const status = await screen.findByRole("link", { name: "Current work" });
    expect(status).toHaveTextContent("workflows/ticket_driven_workflow");
  });

  it("shows remote current work and its delayed device status", async () => {
    vi.mocked(getWorkspaceLive).mockResolvedValue([REMOTE_LIVE]);
    renderActivity();

    expect(await screen.findByText("Updates delayed: remote step")).toBeInTheDocument();
    expect(
      await screen.findByText("Updates from device device-2 are delayed."),
    ).toBeInTheDocument();
  });

  it("shows remote current work without a status prefix while the device is online", async () => {
    vi.mocked(getWorkspaceLive).mockResolvedValue([{ ...REMOTE_LIVE, status: "online" }]);
    renderActivity();

    expect(await screen.findByText("remote step")).toBeInTheDocument();
    expect(screen.queryByText("Live: remote step")).toBe(null);
  });

  it("shows no running-work status for idle members", async () => {
    renderActivity();

    await screen.findByText("Alice");
    expect(screen.queryByRole("link", { name: "Current work" })).toBe(null);
  });

  it("shows the patrol heartbeat for idle members while the scheduler runs", async () => {
    const status = runtimeStatus([]);
    status.scheduler.member_routines = [ALICE_ROUTINE];
    vi.mocked(getSchedulerStatus).mockResolvedValue(status);
    renderActivity();

    expect(await screen.findByText(/Last patrol/)).toBeInTheDocument();
  });

  it("prefers the running-work status over the patrol heartbeat", async () => {
    const status = runtimeStatus([ACTIVE_WORK]);
    status.scheduler.member_routines = [ALICE_ROUTINE];
    vi.mocked(getSchedulerStatus).mockResolvedValue(status);
    renderActivity();

    await screen.findByRole("link", { name: "Current work" });
    expect(screen.queryByText(/Last patrol/)).toBe(null);
  });

  it("hides the patrol heartbeat when the scheduler is stopped", async () => {
    const status = runtimeStatus([]);
    status.scheduler.state = "stopped";
    status.scheduler.running = false;
    status.scheduler.member_routines = [ALICE_ROUTINE];
    vi.mocked(getSchedulerStatus).mockResolvedValue(status);
    renderActivity();

    await screen.findByText("Alice");
    expect(screen.queryByText(/Last patrol/)).toBe(null);
  });

  it("keeps the full session title on the bar, including a PR prefix", async () => {
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      events: [],
      sessions: [
        {
          ...ACTIVITY_FIXTURE.sessions[0],
          title: "PR #385 のレビュー指摘 2 件へ対応し、修正を push する",
        },
      ],
    });
    renderActivity();

    expect(
      await screen.findByRole("button", {
        name: "PR #385 のレビュー指摘 2 件へ対応し、修正を push する",
      }),
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

  // Mirror the component's Intl formatting options so date assertions stay
  // valid whatever locale the test runtime uses.
  function localeShortDate(iso: string): string {
    return new Date(iso).toLocaleDateString([], { month: "numeric", day: "numeric" });
  }

  function localeShortDateTime(iso: string): string {
    return new Date(iso).toLocaleString([], {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

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
    expect(memberRateLimit).toHaveTextContent(localeShortDateTime("2999-07-01T03:30:00Z"));

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

  function mockMemberUsage(
    agent: string,
    usage: {
      windows: (Partial<CliAgentUsageWindow> & Pick<CliAgentUsageWindow, "window">)[];
      limit_reached: boolean;
    },
  ) {
    vi.mocked(getIntelligenceConfig).mockResolvedValue({
      config_dir: "",
      revisions: {},
      person_id: "alice",
      inherited: false,
      model_mapping: {},
      models: [],
      cli_agent_mapping: { default: `cli_agents/${agent}/default.yml` },
      cli_agents: [],
      brain_mapping: [],
      native_agent_policy: {
        codex: { filesystem_access: "workspace" },
        grok: { filesystem_access: "workspace" },
        copilot: { filesystem_access: "workspace" },
      },
    });
    vi.mocked(getCliAgentUsage).mockResolvedValue({
      usages: [
        {
          agent,
          checked_at: "2026-07-01T11:59:00Z",
          limit_reached: usage.limit_reached,
          windows: usage.windows.map((window) => ({
            used_percent: null,
            resets_at: "",
            window_minutes: null,
            label: "",
            detail: false,
            ...window,
          })),
        },
      ],
    });
  }

  const mockCodexMember = (usage: Parameters<typeof mockMemberUsage>[1]) =>
    mockMemberUsage("codex", usage);

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
    expect(screen.getByText(/42%/)).not.toHaveTextContent(localeShortDate("2026-07-01T14:00:00Z"));
    expect(screen.getByText(/42%/)).toHaveTextContent(/42% · \d{1,2}:\d{2}/);
    expect(screen.getByText(/42%/).closest(".activity-member-usage-row")).toHaveAttribute(
      "title",
      expect.stringContaining(localeShortDateTime("2026-07-01T14:00:00Z")),
    );
    // Later resets show only the date inline.
    expect(screen.getByText(/78%/)).toHaveTextContent(localeShortDate("2026-07-04T09:00:00Z"));
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
    expect(memberRateLimit).toHaveTextContent(localeShortDateTime("2026-07-01T13:17:00Z"));
  });

  it("keeps detail windows out of the meters and shows them on hover", async () => {
    // Claude reports per-model weekly budgets as detail windows: the member
    // cell shows only the summary meters, and hovering reveals every window.
    mockMemberUsage("claude", {
      windows: [
        {
          window: "session",
          used_percent: 24,
          resets_at: "2026-07-01T14:00:00Z",
          window_minutes: 300,
        },
        {
          window: "week",
          used_percent: 56,
          resets_at: "2026-07-04T09:00:00Z",
          window_minutes: 10080,
        },
        {
          window: "current_week_fable",
          used_percent: 59,
          resets_at: "2026-07-04T09:00:00Z",
          window_minutes: 10080,
          label: "Fable",
          detail: true,
        },
      ],
      limit_reached: false,
    });
    renderActivity();

    expect(await screen.findByRole("meter", { name: "5h 24%" })).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "1w 56%" })).toBeInTheDocument();
    expect(screen.queryByRole("meter", { name: "1w Fable 59%" })).toBe(null);
    expect(screen.queryByText("1w Fable")).toBe(null);

    const user = userEvent.setup();
    await user.hover(screen.getByRole("meter", { name: "5h 24%" }));
    expect(await screen.findByText("1w Fable")).toBeInTheDocument();
    expect(
      screen.getByText("1w Fable").closest(".activity-member-usage-detail-row"),
    ).toHaveTextContent("59%");
  });

  it("shows a percentless window as its reset time without a meter", async () => {
    // Grok reports no used percent for the subscription quota, only the
    // weekly period's reset time.
    mockMemberUsage("grok", {
      windows: [
        {
          window: "subscription",
          used_percent: null,
          resets_at: "2026-07-04T09:00:00Z",
          window_minutes: 10080,
        },
      ],
      limit_reached: false,
    });
    renderActivity();

    const row = await screen.findByText("1w");
    expect(row.closest(".activity-member-usage-row")).toHaveTextContent(
      localeShortDate("2026-07-04T09:00:00Z"),
    );
    expect(screen.queryByRole("meter")).toBe(null);
    expect(row.closest(".activity-member-usage-row")).toHaveAttribute(
      "title",
      expect.stringContaining(localeShortDateTime("2026-07-04T09:00:00Z")),
    );
    expect(document.querySelector(".activity-member-rate-limit")).toBe(null);
  });

  it("shows the member badge from a percentless window when the limit is reached", async () => {
    mockMemberUsage("grok", {
      windows: [
        {
          window: "subscription",
          used_percent: null,
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
    expect(memberRateLimit).toHaveTextContent(localeShortDateTime("2026-07-04T09:00:00Z"));
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

  it("hides a member's event pins in week view but keeps the shared ones", async () => {
    // A member row spends the week view stacking session chips in day columns,
    // and a pin positioned over them lands on top. The shared row holds no
    // sessions, and for events with no member of their own it is the only row
    // there is -- hiding those made a week's worth reachable from nowhere.
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
    <MantineProvider theme={theme} env="test">
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ActivityHistoryPage />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

describe("a change the hub did not accept", () => {
  const rejectionEvent = {
    id: "rejection-1",
    timestamp: "2026-07-01T11:00:00+00:00",
    person_id: "",
    type: "sync_rejected" as const,
    title: "Update not applied: 2 file(s)",
    detail: "config/team/project.yml, state/devices/d1.json",
    url: "",
    links: [],
    rejection: {
      rejection_id: "01932a7c-0000-7000-8000-00000000000f",
      paths: ["config/team/project.yml", "state/devices/d1.json"],
      source_device_id: "1f0a0000-0000-7000-8000-0000000000d1",
    },
  };

  it("shows the paths, the device that made it, and the recovery id", async () => {
    const user = userEvent.setup();
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      sessions: [],
      events: [rejectionEvent],
    });
    renderActivity();

    await user.hover(await screen.findByRole("button", { name: rejectionEvent.title }));

    expect(await screen.findByText(i18n.t("activity.rejection.title"))).toBeInTheDocument();
    expect(screen.getByText("config/team/project.yml")).toBeInTheDocument();
    expect(screen.getByText("state/devices/d1.json")).toBeInTheDocument();
    expect(screen.getByText("1f0a0000-0000-7000-8000-0000000000d1")).toBeInTheDocument();
    expect(screen.getByText("01932a7c-0000-7000-8000-00000000000f")).toBeInTheDocument();
  });

  it("stays reachable in the week view, where it has no member row to sit in", async () => {
    // Events with no member of their own were drawn only in the day view, so a
    // week's worth of rejections could be reached from nowhere.
    const user = userEvent.setup();
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      sessions: [],
      events: [rejectionEvent],
    });
    renderActivity();
    await user.click(await screen.findByText("1 week"));

    await user.hover(await screen.findByRole("button", { name: rejectionEvent.title }));

    expect(await screen.findByText(i18n.t("activity.rejection.title"))).toBeInTheDocument();
  });

  it("points recovery at the machine that made the change, not at this screen", async () => {
    // The held-back content exists on one device only, and nothing here fetches
    // it, so the guidance has to say where to go instead of offering a button.
    const user = userEvent.setup();
    vi.mocked(getActivityHistory).mockResolvedValue({
      ...ACTIVITY_FIXTURE,
      sessions: [],
      events: [rejectionEvent],
    });
    renderActivity();

    await user.hover(await screen.findByRole("button", { name: rejectionEvent.title }));

    expect(await screen.findByText(i18n.t("activity.rejection.recovery"))).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /restore|recover/i })).not.toBeInTheDocument();
  });
});
