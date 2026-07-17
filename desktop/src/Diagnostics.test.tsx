import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import {
  getConfigStatus,
  getGlobalRecords,
  getMemoryEvents,
  getProjectConfig,
  getRuntimeDebug,
  getTeam,
  getTraceDetail,
  getTraces,
  getTranscriptSettings,
  runScenarioDiagnostics,
  subscribeEvents,
  subscribeLogs,
  updateTranscriptSettings,
  updateRuntimeDebug,
  type ConfigStatus,
  type DiagnosticCheck,
  type MemoryEvent,
  type ProjectConfig,
  type RuntimeStatus,
  type RuntimeUnitStatus,
  type ScenarioDiagnosticsResponse,
  type TranscriptSettingsStatus,
} from "./api/client";
import i18n from "./i18n";
import { makeTraceRecord } from "./test/factories";
import "./i18n";

const t = i18n.getFixedT("en");

// jsdom lacks scrollIntoView, which Mantine's Combobox calls when opening.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

vi.mock("@tauri-apps/plugin-shell", () => ({ open: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(), save: vi.fn() }));
vi.mock("./setup/SetupPage", () => ({ SetupPage: () => <div>Setup Mock</div> }));

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    getConfigStatus: vi.fn(),
    getTeam: vi.fn(),
    getProjectConfig: vi.fn(),
    getSchedulerStatus: vi.fn(async () => runtimeStatus()),
    getSchedulerRoutines: vi.fn(async () => ({
      routines: [{ command: "workflows/ticket_driven_workflow", requires_github: false }],
    })),
    getCommandOptions: vi.fn(async () => ({ options: [] })),
    getRoutineCommandOptions: vi.fn(async () => ({ options: [] })),
    getTranscriptSettings: vi.fn(),
    getMemoryEvents: vi.fn(),
    updateTranscriptSettings: vi.fn(),
    getRuntimeDebug: vi.fn(async () => ({
      enabled: false,
      log_level: "INFO",
      agno_debug: false,
      env_file: "/workspace/.env",
      env_file_exists: true,
    })),
    updateRuntimeDebug: vi.fn(async (body: { enabled: boolean }) => ({
      enabled: body.enabled,
      log_level: body.enabled ? "DEBUG" : "INFO",
      agno_debug: body.enabled,
      env_file: "/workspace/.env",
      env_file_exists: true,
    })),
    verify: vi.fn(),
    runScenarioDiagnostics: vi.fn(),
    getTraces: vi.fn(),
    getTraceDetail: vi.fn(),
    getGlobalRecords: vi.fn(),
    subscribeEvents: vi.fn(),
    subscribeLogs: vi.fn(),
  };
});

beforeEach(() => {
  vi.mocked(getConfigStatus).mockReset().mockResolvedValue(configStatus());
  vi.mocked(getTeam)
    .mockReset()
    .mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [
        {
          person_id: "alice",
          name: "Alice",
          person_type: "agent",
          is_active: true,
          roles: ["developer"],
        },
        {
          person_id: "bob",
          name: "Bob",
          person_type: "agent",
          is_active: false,
          roles: ["reviewer"],
        },
        {
          person_id: "hana",
          name: "Hana",
          person_type: "human",
          is_active: false,
          roles: ["product"],
        },
      ],
    });
  vi.mocked(getProjectConfig).mockReset().mockResolvedValue(projectConfig());
  vi.mocked(getTranscriptSettings).mockReset().mockResolvedValue(transcriptSettings());
  vi.mocked(getRuntimeDebug).mockReset().mockResolvedValue({
    enabled: false,
    log_level: "INFO",
    agno_debug: false,
    env_file: "/workspace/.env",
    env_file_exists: true,
  });
  vi.mocked(getMemoryEvents)
    .mockReset()
    .mockResolvedValue({
      event_count: 1,
      events: [
        memoryEvent({
          title: "Retry note",
          summary: "Refresh before retry.",
          body_preview: "Retry after refreshing the token.",
        }),
      ],
    });
  vi.mocked(updateTranscriptSettings)
    .mockReset()
    .mockResolvedValue(transcriptSettings({ detail: "full" }));
  vi.mocked(updateRuntimeDebug)
    .mockReset()
    .mockImplementation(async (body) => ({
      enabled: body.enabled,
      log_level: body.enabled ? "DEBUG" : "INFO",
      agno_debug: body.enabled,
      env_file: "/workspace/.env",
      env_file_exists: true,
    }));
  vi.mocked(runScenarioDiagnostics).mockReset().mockResolvedValue(scenarioResponse());
  vi.mocked(getTraces).mockReset().mockResolvedValue({ traces: [] });
  vi.mocked(getTraceDetail)
    .mockReset()
    .mockResolvedValue({ trace_id: "", summary: null, records: [] });
  vi.mocked(getGlobalRecords)
    .mockReset()
    .mockResolvedValue({ trace_id: "", summary: null, records: [] });
  vi.mocked(subscribeEvents)
    .mockReset()
    .mockReturnValue(() => {});
  vi.mocked(subscribeLogs)
    .mockReset()
    .mockReturnValue(() => {});
});

describe("Diagnostics readiness tab", () => {
  it("renders the config / env / member / github readiness badges", async () => {
    renderApp();
    await screen.findByRole("heading", { name: t("diagnostics.title") });

    expect(await screen.findByText(t("overview.ready"))).toBeInTheDocument();
    expect(screen.getByText(t("overview.found"))).toBeInTheDocument();
    // The GitHub badge depends on the project-config query, which resolves after
    // the config query, so await it rather than asserting synchronously.
    expect(await screen.findByText(t("overview.enabled"))).toBeInTheDocument();
    // Only the active member is counted.
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("runs readiness diagnostics and reports an all-ok summary", async () => {
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("heading", { name: t("diagnostics.title") });

    await user.click(screen.getByRole("button", { name: t("overview.scenarioDiagnostics.run") }));

    await waitFor(() => expect(runScenarioDiagnostics).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(t("overview.scenarioDiagnostics.ok"))).toBeInTheDocument();
  });

  it("renders warning and error scenario checks with their messages", async () => {
    const user = userEvent.setup();
    vi.mocked(runScenarioDiagnostics).mockResolvedValue(
      scenarioResponse({
        ok: false,
        checks: [
          diagnosticCheck({ status: "ok", code: "config_load" }),
          diagnosticCheck({
            status: "warning",
            section: "slack",
            code: "slack_missing",
            message: "Slack token is not configured",
          }),
          diagnosticCheck({
            status: "error",
            section: "llm",
            code: "llm_missing",
            message: "LLM API key is missing",
          }),
        ],
      }),
    );
    renderApp();
    await screen.findByRole("heading", { name: t("diagnostics.title") });

    await user.click(screen.getByRole("button", { name: t("overview.scenarioDiagnostics.run") }));

    // No localized title/description exists for these codes, so the raw message
    // appears in both the alert title and body.
    expect((await screen.findAllByText("Slack token is not configured")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("LLM API key is missing").length).toBeGreaterThan(0);
    // The passing check is not surfaced as an issue alert.
    expect(screen.queryByText(t("overview.scenarioDiagnostics.ok"))).not.toBeInTheDocument();
  });

  it("shows an alert when scenario diagnostics fail", async () => {
    const user = userEvent.setup();
    vi.mocked(runScenarioDiagnostics).mockRejectedValue(new Error("scenario blew up"));
    renderApp();
    await screen.findByRole("heading", { name: t("diagnostics.title") });

    await user.click(screen.getByRole("button", { name: t("overview.scenarioDiagnostics.run") }));

    expect(await screen.findByText(t("overview.scenarioDiagnostics.failed"))).toBeInTheDocument();
    expect(screen.getByText("scenario blew up")).toBeInTheDocument();
  });
});

describe("Diagnostics settings tab", () => {
  it("updates retention and displays storage usage", async () => {
    const user = userEvent.setup();
    vi.mocked(getTranscriptSettings).mockResolvedValue(
      transcriptSettings({
        total_size_bytes: 2048,
        index_size_bytes: 512,
        memory_size_bytes: 256,
      }),
    );
    renderApp();
    await openTab(user, t("diagnostics.tabs.settings"));

    expect(await screen.findByText("2.0 KiB")).toBeInTheDocument();
    expect(screen.getByText("512 B (rebuild threshold: 8.0 MiB)")).toBeInTheDocument();
    expect(screen.getByText("256 B / 8.0 MiB")).toBeInTheDocument();
    const retention = await screen.findByLabelText(t("diagnostics.transcripts.retentionDays"));
    await user.clear(retention);
    await user.type(retention, "14");
    await user.tab();

    await waitFor(() => expect(updateTranscriptSettings).toHaveBeenCalled());
    const calls = vi.mocked(updateTranscriptSettings).mock.calls;
    expect(calls[calls.length - 1]?.[0]).toEqual({
      detail: "standard",
      retention_days: 14,
    });
  });

  it("explains the selected transcript detail level", async () => {
    const user = userEvent.setup();
    renderApp();
    await openTab(user, t("diagnostics.tabs.settings"));

    expect(
      await screen.findByText(t("diagnostics.transcripts.standardDescription")),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("textbox", { name: t("diagnostics.transcripts.detail") }));
    await user.click(screen.getByRole("option", { name: t("diagnostics.transcripts.full") }));

    expect(
      await screen.findByText(t("diagnostics.transcripts.fullDescription")),
    ).toBeInTheDocument();
    expect(vi.mocked(updateTranscriptSettings).mock.calls[0]?.[0]).toEqual({
      detail: "full",
      retention_days: 30,
    });
  });

  it("updates runtime debug", async () => {
    const user = userEvent.setup();
    renderApp();
    await openTab(user, t("diagnostics.tabs.settings"));

    await user.click(
      await screen.findByRole("switch", { name: t("diagnostics.runtimeDebug.disabled") }),
    );

    await waitFor(() => expect(updateRuntimeDebug).toHaveBeenCalledWith({ enabled: true }));
  });
});

describe("Diagnostics memory tab", () => {
  it("lists memory events and applies visible filters", async () => {
    const user = userEvent.setup();
    renderApp();

    await openTab(user, t("diagnostics.tabs.memory"));

    expect((await screen.findAllByText("Retry note")).length).toBeGreaterThan(0);
    expect(
      screen.getByText(t("diagnostics.memory.displayedValue", { count: 1, limit: 500 })),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Refresh before retry.").length).toBeGreaterThan(0);
    expect(screen.getByText("Retry after refreshing the token.")).toBeInTheDocument();

    await user.click(screen.getAllByLabelText(t("diagnostics.memory.person"))[0]);
    expect(await screen.findByText("Alice (alice)")).toBeInTheDocument();
    expect(screen.getByText("Bob (bob)")).toBeInTheDocument();
    expect(screen.queryByText("Hana (hana)")).not.toBeInTheDocument();

    await user.click(screen.getAllByLabelText(t("diagnostics.memory.action"))[0]);
    await user.click(await screen.findByText(t("diagnostics.memory.actions.touch")));

    await waitFor(() => {
      const calls = vi.mocked(getMemoryEvents).mock.calls;
      expect(calls[calls.length - 1]?.[0]).toMatchObject({
        action: "touch",
      });
    });

    await user.type(screen.getByLabelText(t("diagnostics.memory.search")), "retry");
    await user.keyboard("{Enter}");

    await waitFor(() => {
      const calls = vi.mocked(getMemoryEvents).mock.calls;
      expect(calls[calls.length - 1]?.[0]).toMatchObject({
        action: "touch",
        query: "retry",
      });
    });

    await user.click(screen.getByRole("button", { name: t("diagnostics.memory.searchClear") }));

    await waitFor(() => {
      const calls = vi.mocked(getMemoryEvents).mock.calls;
      expect(calls[calls.length - 1]?.[0]).toMatchObject({
        action: "touch",
        query: undefined,
      });
    });
  });

  it("sorts memory events by timestamp descending", async () => {
    const user = userEvent.setup();
    vi.mocked(getMemoryEvents).mockResolvedValue({
      event_count: 2,
      events: [
        memoryEvent({
          timestamp: "2026-06-21T09:30:00+09:00",
          title: "Offset older",
          doc_id: "doc-older",
        }),
        memoryEvent({
          timestamp: "2026-06-21T01:00:00Z",
          title: "UTC newer",
          doc_id: "doc-newer",
        }),
      ],
    });

    renderApp();
    await openTab(user, t("diagnostics.tabs.memory"));

    await screen.findAllByText("UTC newer");
    const rows = screen
      .getAllByRole("button")
      .filter((button) => button.classList.contains("memory-row"));
    expect(rows[0]).toHaveTextContent("UTC newer");
    expect(rows[1]).toHaveTextContent("Offset older");
  });

  it("opens a focused memory event from diagnostics query parameters", async () => {
    vi.mocked(getMemoryEvents).mockResolvedValue({
      event_count: 2,
      events: [
        memoryEvent({
          timestamp: "2026-01-02T00:00:00Z",
          title: "Other memory",
          doc_id: "other-doc",
          trace_id: "other-trace",
        }),
        memoryEvent({
          timestamp: "2026-01-01T00:00:00Z",
          action: "touch",
          person_id: "bob",
          title: "Focused memory",
          doc_id: "doc-2",
          trace_id: "trace-2",
          body_preview: "Focused body",
        }),
      ],
    });

    renderApp(
      "/diagnostics?tab=memory&doc_id=doc-2&memory_trace_id=trace-2&timestamp=2026-01-01T00%3A00%3A00Z&action=touch&person_id=bob",
    );

    expect(await screen.findByRole("tab", { name: t("diagnostics.tabs.memory") })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => {
      const calls = vi.mocked(getMemoryEvents).mock.calls;
      expect(calls[calls.length - 1]?.[0]).toMatchObject({
        personId: "bob",
        action: "touch",
        docId: "doc-2",
        traceId: "trace-2",
      });
    });
    expect(await screen.findByRole("heading", { name: "Focused memory" })).toBeInTheDocument();
    expect(screen.getByText("Focused body")).toBeInTheDocument();
  });
});

describe("Diagnostics executions tab", () => {
  it("lists traces and shows the selected trace timeline", async () => {
    const user = userEvent.setup();
    vi.mocked(getTraces).mockResolvedValue({
      traces: [
        {
          trace_id: "trace-1",
          source: "manual",
          person_id: "alice",
          command: "workflows/demo",
          workflow: "",
          started_at: "2026-06-12T00:00:01Z",
          updated_at: "2026-06-12T00:00:03Z",
          status: "success",
          event_count: 2,
          log_count: 1,
          error_count: 0,
          span_count: 0,
          attributes: {},
        },
      ],
    });
    vi.mocked(getTraceDetail).mockResolvedValue({
      trace_id: "trace-1",
      summary: null,
      records: [
        makeTraceRecord({
          kind: "event",
          type: "command.started",
          message: "command started",
          timestamp: "2026-06-12T00:00:01Z",
        }),
        makeTraceRecord({
          kind: "log",
          level: "INFO",
          message: "working on it",
          timestamp: "2026-06-12T00:00:02Z",
        }),
      ],
    });

    renderApp("/diagnostics?tab=executions&memory_trace_id=stale-trace");
    await openTab(user, t("diagnostics.tabs.executions"));

    const traceButton = await screen.findByText("workflows/demo");
    expect(
      screen.getByText(t("diagnostics.executions.displayedValue", { count: 1, limit: 200 })),
    ).toBeInTheDocument();
    await user.click(traceButton);

    expect(await screen.findByText("working on it")).toBeInTheDocument();
    expect(vi.mocked(getTraceDetail)).toHaveBeenCalledWith("trace-1");

    // The summary header shows the selected trace's id and computed duration.
    expect(screen.getAllByText("trace-1").length).toBeGreaterThan(0);
    expect(screen.getByText(/2\.0s/)).toBeInTheDocument();

    // The timeline is newest-first: the later log appears before the earlier
    // started event so live updates surface at the top without scrolling.
    const live = screen.getByText("working on it");
    const started = screen.getByText("command.started");
    expect(live.compareDocumentPosition(started) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      screen.queryByRole("link", { name: t("diagnostics.tabs.memory") }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: t("diagnostics.tabs.memory") }));
    await waitFor(() => {
      const calls = vi.mocked(getMemoryEvents).mock.calls;
      expect(calls[calls.length - 1]?.[0]).toMatchObject({ traceId: undefined });
    });
  });

  it("keeps memory records visible when the transcript has expired", async () => {
    const user = userEvent.setup();
    vi.mocked(getTraces).mockResolvedValue({
      traces: [
        {
          trace_id: "trace-1",
          source: "manual",
          person_id: "alice",
          command: "workflows/demo",
          workflow: "",
          started_at: "2026-06-12T00:00:01Z",
          updated_at: "2026-06-12T00:00:03Z",
          status: "success",
          event_count: 2,
          log_count: 0,
          error_count: 0,
          span_count: 0,
          attributes: {},
        },
      ],
    });
    vi.mocked(getTraceDetail).mockResolvedValue({
      trace_id: "trace-1",
      summary: null,
      transcript_available: false,
      records: [
        makeTraceRecord({
          kind: "memory",
          type: "memory.record",
          message: "retained memory audit",
          timestamp: "2026-06-12T00:00:02Z",
        }),
      ],
    });

    renderApp();
    await openTab(user, t("diagnostics.tabs.executions"));
    await user.click(await screen.findByText("workflows/demo"));

    expect(
      await screen.findByText(t("diagnostics.executions.transcriptDeleted")),
    ).toBeInTheDocument();
    expect(screen.getByText("retained memory audit")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: t("diagnostics.tabs.memory") }));

    expect(screen.getByRole("tab", { name: t("diagnostics.tabs.memory") })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => {
      const calls = vi.mocked(getMemoryEvents).mock.calls;
      expect(calls[calls.length - 1]?.[0]).toMatchObject({ traceId: "trace-1" });
    });
  });

  it("opens a combined execution timeline from trace_ids query parameters", async () => {
    vi.mocked(getTraceDetail).mockImplementation(async (traceId: string) => ({
      trace_id: traceId,
      summary: {
        trace_id: traceId,
        source: "manual",
        person_id: "alice",
        command: traceId === "trace-a" ? "first task" : "second task",
        workflow: "",
        started_at: traceId === "trace-a" ? "2026-06-12T00:00:01Z" : "2026-06-12T00:00:03Z",
        updated_at: traceId === "trace-a" ? "2026-06-12T00:00:02Z" : "2026-06-12T00:00:04Z",
        status: "success",
        event_count: 1,
        log_count: 0,
        error_count: 0,
        span_count: 0,
        attributes: {},
      },
      records: [
        makeTraceRecord({
          trace_id: traceId,
          kind: "event",
          type: traceId === "trace-a" ? "first.record" : "second.record",
          timestamp: traceId === "trace-a" ? "2026-06-12T00:00:01Z" : "2026-06-12T00:00:03Z",
        }),
      ],
    }));

    renderApp("/diagnostics?tab=executions&trace_ids=trace-a%2Ctrace-b");

    expect(
      (await screen.findAllByText(t("diagnostics.executions.compositeTitle"))).length,
    ).toBeGreaterThan(0);
    expect((await screen.findAllByText("second.record")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("first.record").length).toBeGreaterThan(0);
    await waitFor(() => {
      expect(vi.mocked(getTraceDetail)).toHaveBeenCalledWith("trace-a");
      expect(vi.mocked(getTraceDetail)).toHaveBeenCalledWith("trace-b");
    });
  });

  it("filters the timeline to logs only", async () => {
    const user = userEvent.setup();
    vi.mocked(getTraces).mockResolvedValue({
      traces: [
        {
          trace_id: "trace-1",
          source: "routine",
          person_id: "alice",
          command: "workflows/demo",
          workflow: "",
          started_at: "2026-06-12T00:00:01Z",
          updated_at: "2026-06-12T00:00:03Z",
          status: "failed",
          event_count: 1,
          log_count: 1,
          error_count: 1,
          span_count: 0,
          attributes: {},
        },
      ],
    });
    vi.mocked(getTraceDetail).mockResolvedValue({
      trace_id: "trace-1",
      summary: null,
      records: [
        makeTraceRecord({
          kind: "event",
          type: "command.failed",
          message: "",
          payload: { code: "command_error", message: "ticket lookup failed" },
          timestamp: "2026-06-12T00:00:01Z",
        }),
        makeTraceRecord({
          kind: "log",
          level: "ERROR",
          message: "boom happened",
          timestamp: "2026-06-12T00:00:02Z",
        }),
      ],
    });

    renderApp();
    await openTab(user, t("diagnostics.tabs.executions"));
    await user.click(await screen.findByText("workflows/demo"));
    expect(await screen.findByText("boom happened")).toBeInTheDocument();
    // The failed event surfaces its payload reason on the timeline.
    expect(screen.getByText("ticket lookup failed")).toBeInTheDocument();

    await user.click(screen.getByText(t("diagnostics.executions.recordFilters.log")));

    expect(screen.getByText("boom happened")).toBeInTheDocument();
    expect(screen.queryByText("ticket lookup failed")).not.toBeInTheDocument();
  });

  it("shows the full record message in the drawer and filters by span", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    const longMessage =
      "This is a long AI CLI tool log message that must be readable in the detail drawer even when the timeline row truncates it.";
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    vi.mocked(getTraces).mockResolvedValue({
      traces: [
        {
          trace_id: "trace-1",
          source: "manual",
          person_id: "alice",
          command: "workflows/demo",
          workflow: "",
          started_at: "2026-06-12T00:00:01Z",
          updated_at: "2026-06-12T00:00:03Z",
          status: "failed",
          event_count: 1,
          log_count: 2,
          error_count: 1,
          span_count: 2,
          attributes: {},
        },
      ],
    });
    vi.mocked(getTraceDetail).mockResolvedValue({
      trace_id: "trace-1",
      summary: null,
      records: [
        makeTraceRecord({
          kind: "log",
          level: "INFO",
          message: "different span log",
          span_id: "span-other",
          timestamp: "2026-06-12T00:00:01Z",
        }),
        makeTraceRecord({
          kind: "log",
          level: "ERROR",
          message: longMessage,
          source: "manual",
          span_id: "span-cli",
          parent_id: "span-parent",
          call_id: "call-1",
          timestamp: "2026-06-12T00:00:02Z",
        }),
      ],
    });

    renderApp();
    await openTab(user, t("diagnostics.tabs.executions"));
    await user.click(await screen.findByText("workflows/demo"));
    await user.click(await screen.findByText(longMessage));

    await waitFor(() => expect(screen.getAllByText(longMessage).length).toBeGreaterThan(1));
    expect(screen.getAllByText(t("diagnostics.executions.sources.manual")).length).toBeGreaterThan(
      0,
    );
    expect(screen.getByText(t("diagnostics.executions.developer.title"))).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("diagnostics.executions.copyMessage") }));
    expect(writeText).toHaveBeenCalledWith(longMessage);

    await user.click(
      screen.getByRole("button", { name: t("diagnostics.executions.recordScope.span") }),
    );

    expect(screen.getByText(t("diagnostics.executions.recordScope.span"))).toBeInTheDocument();
    expect(screen.getByText(longMessage)).toBeInTheDocument();
    expect(screen.queryByText("different span log")).not.toBeInTheDocument();
  });

  it("looks up a ticket number from the unified search field with an exact attribute filter", async () => {
    const user = userEvent.setup();
    vi.mocked(getTraces).mockResolvedValue({ traces: [] });

    renderApp();
    await openTab(user, t("diagnostics.tabs.executions"));

    const field = await screen.findByLabelText(t("diagnostics.executions.search"));
    await user.type(field, "42{Enter}");

    // The list refetches scoped to the exact github.number attribute (not q).
    await waitFor(() =>
      expect(
        vi
          .mocked(getTraces)
          .mock.calls.some(
            (call) => call[0]?.attrKey === "github.number" && call[0]?.attrValue === "42",
          ),
      ).toBe(true),
    );
    // An active-filter pill appears.
    expect(screen.getByText("#42")).toBeInTheDocument();
  });

  it("shows source filters as execution launch methods", async () => {
    const user = userEvent.setup();
    vi.mocked(getTraces).mockResolvedValue({ traces: [] });

    renderApp();
    await openTab(user, t("diagnostics.tabs.executions"));

    expect(screen.getByText(t("diagnostics.executions.sources.manual"))).toBeInTheDocument();
    expect(screen.getByText(t("diagnostics.executions.sources.routine"))).toBeInTheDocument();
    expect(screen.getByText(t("diagnostics.executions.sources.scheduled"))).toBeInTheDocument();
    expect(
      screen.getByText(t("diagnostics.executions.sources.event_listener")),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: t("diagnostics.executions.sources.diagnostics") }),
    ).not.toBeInTheDocument();
  });

  it("clears the unified search input and active filters", async () => {
    const user = userEvent.setup();
    vi.mocked(getTraces).mockResolvedValue({ traces: [] });

    renderApp();
    await openTab(user, t("diagnostics.tabs.executions"));

    const field = await screen.findByLabelText(t("diagnostics.executions.search"));
    await user.type(field, "#42 timeout{Enter}");

    await waitFor(() =>
      expect(
        vi.mocked(getTraces).mock.calls.some((call) => {
          const params = call[0];
          return (
            params?.attrKey === "github.number" &&
            params.attrValue === "42" &&
            params.query === "timeout"
          );
        }),
      ).toBe(true),
    );

    await user.click(screen.getByRole("button", { name: t("diagnostics.executions.searchClear") }));

    expect(field).toHaveValue("");
    await waitFor(() =>
      expect(
        vi.mocked(getTraces).mock.calls.some((call) => {
          const params = call[0];
          return !params?.attrKey && !params?.attrValue && !params?.query;
        }),
      ).toBe(true),
    );
  });

  it("renders a trace with empty timestamps without crashing", async () => {
    const user = userEvent.setup();
    // A prompt-only trace has empty started_at/updated_at; formatting these must
    // not throw (which previously blanked the whole screen).
    vi.mocked(getTraces).mockResolvedValue({
      traces: [
        {
          trace_id: "p1",
          source: "",
          person_id: "",
          command: "",
          workflow: "",
          started_at: "",
          updated_at: "",
          status: "info",
          event_count: 0,
          log_count: 0,
          error_count: 0,
          span_count: 0,
          attributes: {},
        },
      ],
    });

    renderApp();
    await openTab(user, t("diagnostics.tabs.executions"));

    // The row falls back to the trace id and renders (no crash).
    expect(await screen.findByText("p1")).toBeInTheDocument();
  });

  it("shows unscoped service events and global logs in the Global view", async () => {
    const user = userEvent.setup();
    vi.mocked(getTraces).mockResolvedValue({ traces: [] });
    vi.mocked(getGlobalRecords).mockResolvedValue({
      trace_id: "",
      summary: null,
      records: [
        makeTraceRecord({
          kind: "event",
          type: "scheduler.running",
          source: "scheduler",
          trace_id: null,
          timestamp: "2026-06-12T00:00:01Z",
        }),
        makeTraceRecord({
          kind: "log",
          level: "INFO",
          message: "application started",
          trace_id: null,
          timestamp: "2026-06-12T00:00:02Z",
        }),
      ],
    });

    renderApp();
    await openTab(user, t("diagnostics.tabs.executions"));
    // The Global view is selected by default, so its records load without the
    // user having to click the pinned Global entry.

    expect(await screen.findByText("application started")).toBeInTheDocument();
    expect(screen.getByText("scheduler.running")).toBeInTheDocument();
    expect(vi.mocked(getGlobalRecords)).toHaveBeenCalled();

    // The Global entry belongs only to the "all" source filter: narrowing to a
    // specific source hides it.
    await user.click(screen.getByText(t("diagnostics.executions.sources.manual")));
    expect(screen.queryByText(t("diagnostics.executions.global.title"))).not.toBeInTheDocument();
  });
});

async function openTab(user: ReturnType<typeof userEvent.setup>, name: string) {
  await screen.findByRole("heading", { name: t("diagnostics.title") });
  await user.click(screen.getByRole("tab", { name }));
}

function renderApp(initialPath = "/diagnostics") {
  const theme = createTheme({ primaryColor: "dark", defaultRadius: "md" });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialPath]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

function configStatus(overrides: Partial<ConfigStatus> = {}): ConfigStatus {
  return {
    cwd: "/workspace",
    env_file: "/workspace/.env",
    env_file_exists: true,
    config_dir: "/workspace/.guildbotics/config",
    project_file: "/workspace/.guildbotics/config/project.yml",
    project_file_exists: true,
    storage_dir: "/workspace/.guildbotics",
    ...overrides,
  };
}

function projectConfig(overrides: Partial<ProjectConfig> = {}): ProjectConfig {
  return {
    config_dir: "/workspace/.guildbotics/config",
    env_file_path: "/workspace/.env",
    language: "en",
    description: "Demo project",
    llm_api_type: "openai",
    cli_agent: "codex",
    github_enabled: true,
    github_project_url: "",
    lane_map: { ready: "Todo", working: "In Progress", done: "Done" },
    provider_api_keys: { openai: true, gemini: false, anthropic: false },
    ...overrides,
  };
}

function transcriptSettings(
  overrides: Partial<TranscriptSettingsStatus> = {},
): TranscriptSettingsStatus {
  return {
    detail: "standard",
    retention_days: 30,
    env_file: "/workspace/.env",
    env_file_exists: true,
    sessions_dir: "/workspace/.guildbotics/data/run/sessions",
    total_size_bytes: 0,
    index_size_bytes: 0,
    index_rewrite_threshold_bytes: 8 * 1024 * 1024,
    memory_size_bytes: 0,
    memory_max_size_bytes: 8 * 1024 * 1024,
    ...overrides,
  };
}

function memoryEvent(overrides: Partial<MemoryEvent> = {}): MemoryEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    action: "record",
    person_id: "alice",
    scope: "personal",
    doc_id: "doc-1",
    path: "documents/personal/alice/doc-1",
    title: "Memory note",
    summary: "Memory summary",
    kind: "note",
    trace_id: "trace-1",
    run_id: "run-1",
    task_run_id: "task-1",
    source: [{ type: "ticket", url: "https://example.test/issues/1" }],
    changed_fields: [],
    query_keywords: [],
    result_count: null,
    duration_ms: null,
    body_preview: "Memory body",
    ...overrides,
  };
}

function scenarioResponse(
  overrides: Partial<ScenarioDiagnosticsResponse> = {},
): ScenarioDiagnosticsResponse {
  const checks = overrides.checks ?? [diagnosticCheck({ status: "ok", code: "config_load" })];
  return {
    ok: overrides.ok ?? true,
    active_members: overrides.active_members ?? ["alice"],
    checks,
    warnings: overrides.warnings ?? checks.filter((c) => c.status === "warning"),
    errors: overrides.errors ?? checks.filter((c) => c.status === "error"),
  };
}

function diagnosticCheck(overrides: Partial<DiagnosticCheck> = {}): DiagnosticCheck {
  return {
    section: "config",
    code: "config_load",
    status: "ok",
    message: "ok",
    target: "",
    person_id: "",
    context: {},
    ...overrides,
  };
}

function runtimeStatus(): RuntimeStatus {
  return { scheduler: runtimeUnit("scheduler"), events: runtimeUnit("events") };
}

function runtimeUnit(target: "scheduler" | "events"): RuntimeUnitStatus {
  return {
    target,
    state: "stopped",
    running: false,
    started_at: null,
    stopped_at: null,
    error: null,
    routine_commands: [],
    max_consecutive_errors: null,
    routine_interval_minutes: null,
    active_member_count: 1,
    worker_count: 0,
    scheduled_source_enabled: null,
    routine_source_enabled: null,
    event_queue_source_enabled: null,
    subscription_count: 0,
    listener_count: 0,
    cycle_count: 0,
    cycle_failure_count: 0,
    events_drained_count: 0,
    events_auth_failed_count: 0,
    events_auth_failed_persons: [],
  };
}
