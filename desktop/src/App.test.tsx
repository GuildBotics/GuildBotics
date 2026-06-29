import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { TFunction } from "i18next";
import { HashRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  App,
  buildCommandArgs,
  buildCommandTimeline,
  commandFailureDetail,
  DEFAULT_SERVICE_PREFERENCES,
  loadServicePreferences,
  saveServicePreferences,
  SERVICE_PREFERENCES_KEY,
  decodeTraceText,
  eventBadgeColor,
  eventTypeLabel,
  formatCommandEvent,
  isStopTimeoutPending,
  localFileHref,
  logBadgeColor,
  matchesRecordFilter,
  matchesRecordScopeFilter,
  parseTraceSearch,
  openLocalFile,
  parseTicketQuery,
  recordBadgeColor,
  recordBadgeLabel,
  recordAttributeRows,
  recordDisplayMessage,
  ticketChipInfo,
  selectTraceFile,
  splitCommandLine,
  traceBrainLabel,
  traceFieldRows,
  traceGroupMetadata,
  traceStatusColor,
  traceDuration,
  shortTraceId,
  upsertCommandRecord,
  type CommandRunRecord,
} from "./App";
import type {
  CommandOption,
  PromptTraceEntry,
  RuntimeEvent,
  RuntimeUnitStatus,
} from "./api/client";
import i18n from "./i18n";
import "./i18n";
import { makeRuntimeEvent, makeRuntimeLog, makeTraceRecord } from "./test/factories";
import { buildTraceGroups, type PromptTraceGroup } from "./trace";

const openShell = vi.fn();
const openDialog = vi.fn();
const saveDialog = vi.fn();

vi.mock("@tauri-apps/plugin-shell", () => ({
  open: (path: string) => openShell(path),
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: (options: unknown) => openDialog(options),
  save: (options: unknown) => saveDialog(options),
}));

function t(): TFunction {
  return i18n.getFixedT("en") as TFunction;
}

vi.mock("./setup/SetupPage", () => ({
  SetupPage: () => <div>Setup Mock</div>,
}));

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    getConfigStatus: vi.fn(async () => ({
      cwd: "/workspace",
      env_file: "/workspace/.env",
      env_file_exists: true,
      config_dir: "/workspace/.guildbotics/config",
      project_file: "/workspace/.guildbotics/config/project.yml",
      project_file_exists: true,
      storage_dir: "/workspace/.guildbotics",
    })),
    getTeam: vi.fn(async () => ({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [{ person_id: "alice", name: "Alice", is_active: true, roles: ["developer"] }],
    })),
    getSchedulerRoutines: vi.fn(async () => ({
      routines: [
        {
          command: "workflows/ticket_driven_workflow",
          label: "Ticket driven workflow",
          description: "",
          requires_github: false,
        },
      ],
    })),
    getSchedulerStatus: vi.fn(async () => ({
      scheduler: runtimeUnit("scheduler"),
      events: runtimeUnit("events"),
    })),
    getProjectConfig: vi.fn(async () => ({
      config_dir: "/workspace/.guildbotics/config",
      env_file_path: "/workspace/.env",
      language: "en",
      description: "Demo project",
      llm_api_type: "openai",
      cli_agent: "codex",
      github_enabled: false,
      github_project_url: "",
      has_google_api_key: false,
      has_openai_api_key: true,
      has_anthropic_api_key: false,
    })),
    getCommandOptions: vi.fn(async () => ({ options: [] })),
    getRoutineCommandOptions: vi.fn(async () => ({ options: [] })),
    getPromptTrace: vi.fn(async () => ({
      enabled: false,
      env_file: "",
      env_file_exists: false,
      trace_file: "",
      output_trace_file: "",
      default_trace_file: "",
      trace_file_exists: false,
      event_count: 0,
      events: [],
    })),
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
    runScenarioDiagnostics: vi.fn(async () => ({
      ok: true,
      active_members: ["alice"],
      checks: [],
      warnings: [],
      errors: [],
    })),
    subscribeEvents: vi.fn(() => () => {}),
    subscribeLogs: vi.fn(() => () => {}),
  };
});

describe("App", () => {
  it("renders the service page with runtime controls", async () => {
    window.location.hash = "#/service";
    renderApp();

    expect(await screen.findByRole("heading", { name: "Service Runtime" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Service/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument();
  });
});

describe("buildTraceGroups", () => {
  it("pairs response entries with later request entries from the same trace target", () => {
    const response = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:02Z",
      response: "done",
      fields: { model: "gpt" },
    });
    const request = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:01Z",
      prompt: "hello",
      fields: { model: "gpt" },
    });

    const groups = buildTraceGroups([response, request]);

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      kind: "llm",
      request,
      response,
      single: null,
      personId: "alice",
      brain: "brains/default.yml",
    });
  });

  it("keeps unmatched trace entries as individual groups", () => {
    const entry = traceEntry({ event: "chat.message", timestamp: "2026-06-04T01:00:03Z" });

    const groups = buildTraceGroups([entry]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.single).toBe(entry);
  });
});

describe("buildCommandArgs", () => {
  function option(overrides: Partial<CommandOption> = {}): CommandOption {
    return {
      command: "demo",
      label: "Demo",
      description: "",
      category: "function",
      source: "workspace",
      path: "/cmd.py",
      arguments: [],
      supports_raw_args: true,
      recommended_input: "",
      requirements: [],
      ...overrides,
    };
  }

  it("builds positional args and keyword args from option values", () => {
    const opt = option({
      arguments: [
        { name: "first", kind: "positional", required: true, default: "" },
        { name: "mode", kind: "keyword", required: false, default: "" },
      ],
    });

    const args = buildCommandArgs(opt, { first: " hello ", mode: "fast" }, "");

    expect(args).toEqual(["hello", "mode=fast"]);
  });

  it("skips empty or whitespace-only values", () => {
    const opt = option({
      arguments: [
        { name: "first", kind: "positional", required: true, default: "" },
        { name: "mode", kind: "keyword", required: false, default: "" },
      ],
    });

    const args = buildCommandArgs(opt, { first: "", mode: "   " }, "");

    expect(args).toEqual([]);
  });

  it("appends parsed raw args after structured args", () => {
    const opt = option({
      arguments: [{ name: "first", kind: "positional", required: true, default: "" }],
    });

    const args = buildCommandArgs(opt, { first: "x" }, 'a "b c" key=value');

    expect(args).toEqual(["x", "a", "b c", "key=value"]);
  });

  it("returns only raw args when no option is selected", () => {
    expect(buildCommandArgs(null, { ignored: "x" }, "raw")).toEqual(["raw"]);
  });
});

describe("splitCommandLine", () => {
  it("splits on whitespace", () => {
    expect(splitCommandLine("a b   c")).toEqual(["a", "b", "c"]);
  });

  it("preserves double-quoted segments", () => {
    expect(splitCommandLine('a "b c" d')).toEqual(["a", "b c", "d"]);
  });

  it("preserves single-quoted segments", () => {
    expect(splitCommandLine("a 'b c' d")).toEqual(["a", "b c", "d"]);
  });

  it("returns an empty array for empty input", () => {
    expect(splitCommandLine("")).toEqual([]);
    expect(splitCommandLine("   ")).toEqual([]);
  });
});

describe("upsertCommandRecord", () => {
  function record(overrides: Partial<CommandRunRecord>): CommandRunRecord {
    return {
      traceId: "req-1",
      person: "alice",
      command: "demo",
      startedAt: "2026-06-04T01:00:00Z",
      status: "running",
      ...overrides,
    };
  }

  it("prepends a new request", () => {
    const existing = [record({ traceId: "req-0", startedAt: "2026-06-04T00:00:00Z" })];

    const next = upsertCommandRecord(existing, record({ traceId: "req-1" }));

    expect(next.map((entry) => entry.traceId)).toEqual(["req-1", "req-0"]);
  });

  it("updates an existing request and keeps prior output when not provided", () => {
    const existing = [record({ traceId: "req-1", status: "running", output: "partial" })];

    const next = upsertCommandRecord(existing, record({ traceId: "req-1", status: "success" }));

    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({ status: "success", output: "partial" });
  });

  it("sorts records by startedAt descending and caps at 20", () => {
    const many = Array.from({ length: 25 }, (_, index) =>
      record({
        traceId: `req-${index}`,
        startedAt: `2026-06-04T00:${String(index).padStart(2, "0")}:00Z`,
      }),
    );

    const next = upsertCommandRecord([], many[0]);
    const filled = many.slice(1).reduce((acc, item) => upsertCommandRecord(acc, item), next);

    expect(filled).toHaveLength(20);
    expect(filled[0]?.traceId).toBe("req-24");
  });
});

describe("commandFailureDetail", () => {
  it("serializes request id, type, and payload as pretty JSON", () => {
    const detail = commandFailureDetail(
      runtimeEvent({
        type: "command.failed",
        trace_id: "req-9",
        payload: { code: "boom", message: "oops" },
      }),
    );

    expect(JSON.parse(detail)).toEqual({
      trace_id: "req-9",
      type: "command.failed",
      payload: { code: "boom", message: "oops" },
    });
    expect(detail).toContain("\n");
  });
});

describe("formatCommandEvent", () => {
  it("prefers payload.message", () => {
    expect(formatCommandEvent(runtimeEvent({ payload: { message: "hi", command: "demo" } }))).toBe(
      "hi",
    );
  });

  it("falls back to payload.command", () => {
    expect(formatCommandEvent(runtimeEvent({ payload: { command: "demo" } }))).toBe("demo");
  });

  it("falls back to request id then type", () => {
    expect(formatCommandEvent(runtimeEvent({ trace_id: "req-7", payload: {} }))).toBe("req-7");
    expect(
      formatCommandEvent(runtimeEvent({ type: "command.failed", trace_id: null, payload: {} })),
    ).toBe("command.failed");
  });
});

describe("matchesRecordFilter", () => {
  it("passes everything for all", () => {
    expect(matchesRecordFilter(makeTraceRecord({ kind: "event" }), "all")).toBe(true);
  });

  it("matches error logs and failed events for the error filter", () => {
    expect(matchesRecordFilter(makeTraceRecord({ kind: "log", level: "ERROR" }), "error")).toBe(
      true,
    );
    expect(
      matchesRecordFilter(makeTraceRecord({ kind: "event", type: "command.failed" }), "error"),
    ).toBe(true);
    expect(matchesRecordFilter(makeTraceRecord({ kind: "log", level: "INFO" }), "error")).toBe(
      false,
    );
  });

  it("matches llm and cli_agent prompt traces by type prefix", () => {
    expect(
      matchesRecordFilter(makeTraceRecord({ kind: "prompt_trace", type: "llm.request" }), "llm"),
    ).toBe(true);
    expect(
      matchesRecordFilter(
        makeTraceRecord({ kind: "prompt_trace", type: "cli_agent.response" }),
        "cli_agent",
      ),
    ).toBe(true);
    expect(
      matchesRecordFilter(
        makeTraceRecord({ kind: "prompt_trace", type: "llm.request" }),
        "cli_agent",
      ),
    ).toBe(false);
  });

  it("matches logs emitted within the agent span for llm/cli_agent filters", () => {
    expect(
      matchesRecordFilter(makeTraceRecord({ kind: "log", span: "cli_agent" }), "cli_agent"),
    ).toBe(true);
    expect(matchesRecordFilter(makeTraceRecord({ kind: "log", span: "llm" }), "llm")).toBe(true);
    // A log without the matching span is not pulled into the agent filter.
    expect(matchesRecordFilter(makeTraceRecord({ kind: "log", span: "" }), "cli_agent")).toBe(
      false,
    );
    expect(matchesRecordFilter(makeTraceRecord({ kind: "log", span: "cli_agent" }), "llm")).toBe(
      false,
    );
  });

  it("matches by kind for event and log filters", () => {
    expect(matchesRecordFilter(makeTraceRecord({ kind: "event" }), "event")).toBe(true);
    expect(matchesRecordFilter(makeTraceRecord({ kind: "log" }), "log")).toBe(true);
    expect(matchesRecordFilter(makeTraceRecord({ kind: "event" }), "log")).toBe(false);
  });
});

describe("matchesRecordScopeFilter", () => {
  it("matches an exact span", () => {
    expect(
      matchesRecordScopeFilter(makeTraceRecord({ span_id: "s1" }), {
        kind: "span",
        value: "s1",
        label: "This step only",
      }),
    ).toBe(true);
    expect(
      matchesRecordScopeFilter(makeTraceRecord({ span_id: "s2" }), {
        kind: "span",
        value: "s1",
        label: "This step only",
      }),
    ).toBe(false);
  });

  it("matches a call id", () => {
    expect(
      matchesRecordScopeFilter(makeTraceRecord({ call_id: "c1" }), {
        kind: "call",
        value: "c1",
        label: "This call only",
      }),
    ).toBe(true);
  });

  it("matches a span and its direct children for subtree filters", () => {
    const filter = { kind: "subtree" as const, value: "parent", label: "This step and children" };
    expect(matchesRecordScopeFilter(makeTraceRecord({ span_id: "parent" }), filter)).toBe(true);
    expect(matchesRecordScopeFilter(makeTraceRecord({ parent_id: "parent" }), filter)).toBe(true);
    expect(matchesRecordScopeFilter(makeTraceRecord({ span_id: "other" }), filter)).toBe(false);
  });
});

describe("buildCommandTimeline", () => {
  it("merges command events and logs scoped to the trace, newest-first", () => {
    const events = [
      makeRuntimeEvent({
        type: "command.started",
        trace_id: "t1",
        payload: { command: "demo" },
        timestamp: "2026-06-04T01:00:00Z",
      }),
      makeRuntimeEvent({
        type: "command.finished",
        trace_id: "t1",
        payload: { command: "demo" },
        timestamp: "2026-06-04T01:00:03Z",
      }),
      // Different trace and non-command events are excluded.
      makeRuntimeEvent({ type: "command.started", trace_id: "other" }),
      makeRuntimeEvent({ type: "scheduler.running", trace_id: "t1" }),
    ];
    const logs = [
      makeRuntimeLog({
        trace_id: "t1",
        level: "INFO",
        message: "working",
        timestamp: "2026-06-04T01:00:01Z",
      }),
      makeRuntimeLog({ trace_id: "other", level: "INFO", message: "elsewhere" }),
    ];

    const timeline = buildCommandTimeline(events, logs, "t1");

    // Newest-first, merged across events + logs, scoped to t1.
    expect(timeline.map((item) => item.label)).toEqual(["finished", "INFO", "started"]);
    expect(timeline.some((item) => item.message === "working")).toBe(true);
    expect(timeline.some((item) => item.message === "elsewhere")).toBe(false);
  });

  it("returns nothing without an active trace", () => {
    expect(buildCommandTimeline([makeRuntimeEvent({ type: "command.started" })], [], null)).toEqual(
      [],
    );
  });
});

describe("parseTicketQuery", () => {
  it("matches a GitHub issue/PR URL exactly on github.url", () => {
    expect(parseTicketQuery("https://github.com/owner/repo/issues/42")).toEqual({
      key: "github.url",
      value: "https://github.com/owner/repo/issues/42",
      label: "#42",
    });
    // A trailing comment fragment is stripped so it matches the stored url.
    expect(parseTicketQuery("https://github.com/o/r/pull/7#issuecomment-1")).toEqual({
      key: "github.url",
      value: "https://github.com/o/r/pull/7",
      label: "#7",
    });
  });

  it("matches a number / #number / owner/repo#number on github.number", () => {
    expect(parseTicketQuery("42")).toEqual({
      key: "github.number",
      value: "42",
      label: "#42",
    });
    expect(parseTicketQuery("#42")).toEqual({
      key: "github.number",
      value: "42",
      label: "#42",
    });
    expect(parseTicketQuery("owner/repo#42")).toEqual({
      key: "github.number",
      value: "42",
      label: "#42",
    });
  });

  it("returns null for empty or non-ticket input", () => {
    expect(parseTicketQuery("")).toBeNull();
    expect(parseTicketQuery("   ")).toBeNull();
    expect(parseTicketQuery("not a ticket")).toBeNull();
  });
});

describe("parseTraceSearch", () => {
  it("keeps ordinary text as a fuzzy query", () => {
    expect(parseTraceSearch("timeout failed")).toEqual({
      query: "timeout failed",
      attrFilter: null,
    });
  });

  it("splits an explicit ticket token from the fuzzy query", () => {
    expect(parseTraceSearch("#42 timeout failed")).toEqual({
      query: "timeout failed",
      attrFilter: { key: "github.number", value: "42", label: "#42" },
    });
  });

  it("keeps a bare number as a ticket lookup only when it is the whole input", () => {
    expect(parseTraceSearch("42")).toEqual({
      query: "",
      attrFilter: { key: "github.number", value: "42", label: "#42" },
    });
    expect(parseTraceSearch("42 timeout")).toEqual({
      query: "42 timeout",
      attrFilter: null,
    });
  });
});

describe("ticketChipInfo", () => {
  it("prefers the url for filtering and labels issues/PRs", () => {
    expect(
      ticketChipInfo({
        "github.number": "42",
        "github.url": "https://github.com/o/r/issues/42",
        "github.kind": "issue",
      }),
    ).toEqual({
      label: "#42",
      key: "github.url",
      value: "https://github.com/o/r/issues/42",
      url: "https://github.com/o/r/issues/42",
    });
    expect(ticketChipInfo({ "github.number": "7", "github.kind": "pull_request" })).toEqual({
      label: "PR #7",
      key: "github.number",
      value: "7",
      url: "",
    });
  });

  it("returns null when there is no github attribute", () => {
    expect(ticketChipInfo({})).toBeNull();
    expect(ticketChipInfo({ service_run_id: "x" })).toBeNull();
  });
});

describe("traceStatusColor", () => {
  it("maps statuses to colors", () => {
    expect(traceStatusColor("success")).toBe("green");
    expect(traceStatusColor("failed")).toBe("red");
    expect(traceStatusColor("running")).toBe("blue");
    expect(traceStatusColor("info")).toBe("gray");
  });
});

describe("recordDisplayMessage", () => {
  it("uses the log message for log records", () => {
    expect(recordDisplayMessage(makeTraceRecord({ kind: "log", message: "disk full" }))).toBe(
      "disk full",
    );
  });

  it("surfaces the payload failure reason for failed events", () => {
    expect(
      recordDisplayMessage(
        makeTraceRecord({
          kind: "event",
          type: "command.failed",
          message: "",
          payload: { code: "command_error", message: "boom" },
        }),
      ),
    ).toBe("boom");
  });

  it("falls back to the event type when there is no payload detail", () => {
    expect(
      recordDisplayMessage(
        makeTraceRecord({ kind: "event", type: "command.started", message: "", payload: {} }),
      ),
    ).toBe("command.started");
  });

  it("uses description or type for prompt traces", () => {
    expect(
      recordDisplayMessage(
        makeTraceRecord({ kind: "prompt_trace", type: "llm.request", message: "" }),
      ),
    ).toBe("llm.request");
  });
});

describe("shortTraceId", () => {
  it("keeps short ids unchanged", () => {
    expect(shortTraceId("trace-1")).toBe("trace-1");
  });

  it("abbreviates long ids keeping the head and tail", () => {
    expect(shortTraceId("9926d6da8a844dd8a39a2fc686ba9d36")).toBe("9926d6da…ba9d36");
  });
});

describe("traceDuration", () => {
  it("formats sub-second durations in milliseconds", () => {
    expect(
      traceDuration({
        started_at: "2026-06-12T00:00:00.000Z",
        updated_at: "2026-06-12T00:00:00.250Z",
      }),
    ).toBe("250ms");
  });

  it("formats durations of a second or more in seconds", () => {
    expect(
      traceDuration({
        started_at: "2026-06-12T00:00:00.000Z",
        updated_at: "2026-06-12T00:00:02.500Z",
      }),
    ).toBe("2.5s");
  });

  it("returns a dash for missing or invalid timestamps", () => {
    expect(traceDuration({ started_at: "", updated_at: "" })).toBe("—");
  });
});

describe("recordBadgeColor and recordBadgeLabel", () => {
  it("colors prompt traces violet and labels them by type", () => {
    const record = makeTraceRecord({ kind: "prompt_trace", type: "llm.request" });
    expect(recordBadgeColor(record)).toBe("violet");
    expect(recordBadgeLabel(t(), record)).toBe("llm.request");
  });

  it("labels logs by level", () => {
    const record = makeTraceRecord({ kind: "log", level: "WARNING" });
    expect(recordBadgeLabel(t(), record)).toBe("WARNING");
  });
});

describe("recordAttributeRows", () => {
  it("labels known diagnostic attributes for display", () => {
    expect(
      recordAttributeRows(t(), {
        "github.repo": "owner/repo",
        "github.number": "42",
        unknown: "kept in raw json only",
      }),
    ).toEqual([
      ["GitHub repository", "owner/repo"],
      ["Ticket / PR number", "42"],
    ]);
  });
});

describe("eventTypeLabel", () => {
  it("labels command events from the command_ namespace", () => {
    expect(eventTypeLabel(t(), "command.started")).toBe(
      i18n.t("overview.eventTypes.command_started", { lng: "en" }),
    );
    expect(eventTypeLabel(t(), "command.finished")).toBe(
      i18n.t("overview.eventTypes.command_finished", { lng: "en" }),
    );
  });

  it("labels scheduler and events families", () => {
    expect(eventTypeLabel(t(), "scheduler.running")).toBe(
      i18n.t("overview.eventTypes.scheduler", { lng: "en" }),
    );
    expect(eventTypeLabel(t(), "events.stopped")).toBe(
      i18n.t("overview.eventTypes.events", { lng: "en" }),
    );
  });

  it("returns the raw type for unknown families", () => {
    expect(eventTypeLabel(t(), "mystery.thing")).toBe("mystery.thing");
  });
});

describe("badge colors", () => {
  it("maps event types to colors", () => {
    expect(eventBadgeColor("command.failed")).toBe("red");
    expect(eventBadgeColor("scheduler.running")).toBe("teal");
    expect(eventBadgeColor("command.started")).toBe("teal");
    expect(eventBadgeColor("command.finished")).toBe("teal");
    expect(eventBadgeColor("scheduler.stopping")).toBe("orange");
    expect(eventBadgeColor("scheduler.stopped")).toBe("gray");
  });

  it("maps log levels to colors", () => {
    expect(logBadgeColor("error")).toBe("red");
    expect(logBadgeColor("CRITICAL")).toBe("red");
    expect(logBadgeColor("warning")).toBe("orange");
    expect(logBadgeColor("info")).toBe("gray");
  });
});

describe("isStopTimeoutPending", () => {
  function unit(overrides: Partial<RuntimeUnitStatus>): RuntimeUnitStatus {
    return { ...runtimeUnit("scheduler"), ...overrides };
  }

  it("is true when a failed unit is still running due to stop timeout", () => {
    expect(
      isStopTimeoutPending(
        unit({ running: true, state: "failed", error: "worker did not stop before timeout" }),
      ),
    ).toBe(true);
  });

  it("is false when not running, not failed, or error unrelated", () => {
    expect(isStopTimeoutPending(undefined)).toBe(false);
    expect(
      isStopTimeoutPending(
        unit({ running: false, state: "failed", error: "did not stop before timeout" }),
      ),
    ).toBe(false);
    expect(
      isStopTimeoutPending(
        unit({ running: true, state: "running", error: "did not stop before timeout" }),
      ),
    ).toBe(false);
    expect(isStopTimeoutPending(unit({ running: true, state: "failed", error: "other" }))).toBe(
      false,
    );
  });
});

describe("decodeTraceText", () => {
  it("decodes unicode escapes, newlines, and tabs", () => {
    expect(decodeTraceText("a\\nb\\tc")).toBe("a\nb\tc");
    expect(decodeTraceText("\\u3042")).toBe("あ");
  });

  it("leaves plain text untouched", () => {
    expect(decodeTraceText("plain text")).toBe("plain text");
  });
});

describe("traceBrainLabel", () => {
  it("returns the file stem of a brain path", () => {
    expect(traceBrainLabel("brains/default.yml")).toBe("default");
    expect(traceBrainLabel("nested/dir/coder.yaml")).toBe("coder");
  });

  it("returns a dash for an empty brain", () => {
    expect(traceBrainLabel("")).toBe("-");
  });
});

describe("traceFieldRows", () => {
  it("includes base fields and extra string/number/boolean fields", () => {
    const rows = traceFieldRows(
      traceEntry({
        brain: "brains/default.yml",
        command: "demo",
        target: "",
        cwd: "/workspace",
        fields: { model: "gpt", count: 3, flag: true, obj: { a: 1 } },
      }),
    );

    expect(rows).toContainEqual(["brain", "brains/default.yml"]);
    expect(rows).toContainEqual(["command", "demo"]);
    expect(rows).toContainEqual(["cwd", "/workspace"]);
    expect(rows).toContainEqual(["model", "gpt"]);
    expect(rows).toContainEqual(["count", "3"]);
    expect(rows).toContainEqual(["flag", "true"]);
    expect(rows.some(([label]) => label === "obj")).toBe(false);
    expect(rows.some(([label]) => label === "target")).toBe(false);
  });

  it("does not duplicate a field already present as a base field", () => {
    const rows = traceFieldRows(
      traceEntry({ brain: "brains/default.yml", fields: { brain: "override" } }),
    );

    expect(rows.filter(([label]) => label === "brain")).toHaveLength(1);
    expect(rows).toContainEqual(["brain", "brains/default.yml"]);
  });
});

describe("traceGroupMetadata", () => {
  function group(overrides: Partial<PromptTraceGroup>): PromptTraceGroup {
    return {
      id: "g1",
      kind: "llm",
      request: null,
      response: null,
      single: null,
      timestamp: "2026-06-04T01:00:00Z",
      personId: "alice",
      brain: "brains/default.yml",
      ...overrides,
    };
  }

  it("merges request and response field rows and adds the decoded brain", () => {
    const rows = traceGroupMetadata(
      group({
        request: traceEntry({ command: "demo", fields: { model: "gpt" } }),
        response: traceEntry({ fields: { tokens: 12 } }),
      }),
    );

    const map = new Map(rows);
    expect(map.get("command")).toBe("demo");
    expect(map.get("model")).toBe("gpt");
    expect(map.get("tokens")).toBe("12");
    expect(map.get("brain")).toBe("brains/default.yml");
  });
});

describe("localFileHref", () => {
  it("builds a file URL for an absolute POSIX path", () => {
    expect(localFileHref("/workspace/trace.log")).toBe("file:///workspace/trace.log");
  });

  it("builds a file URL for a relative path", () => {
    expect(localFileHref("logs/trace.log")).toBe("file:///logs/trace.log");
  });

  it("normalizes Windows backslashes and encodes spaces", () => {
    expect(localFileHref("C:\\Users\\a b\\trace.log")).toBe("file:///C:/Users/a%20b/trace.log");
  });
});

describe("file helpers outside Tauri runtime", () => {
  beforeEach(() => {
    openShell.mockClear();
    openDialog.mockClear();
    saveDialog.mockClear();
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  it("openLocalFile is a no-op when not in Tauri", async () => {
    await openLocalFile("/workspace/trace.log");
    expect(openShell).not.toHaveBeenCalled();
  });

  it("selectTraceFile resolves to null when not in Tauri", async () => {
    await expect(selectTraceFile("open", "/workspace/trace.log")).resolves.toBeNull();
    await expect(selectTraceFile("save", "/workspace/trace.log")).resolves.toBeNull();
    expect(openDialog).not.toHaveBeenCalled();
    expect(saveDialog).not.toHaveBeenCalled();
  });
});

describe("service preferences persistence", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("returns the defaults when nothing is stored", () => {
    expect(loadServicePreferences()).toEqual(DEFAULT_SERVICE_PREFERENCES);
  });

  it("round-trips saved preferences", () => {
    saveServicePreferences({
      scheduledSourceEnabled: false,
      routineSourceEnabled: true,
      eventQueueSourceEnabled: true,
      routineIntervalMinutes: 25,
      maxConsecutiveErrors: 7,
    });
    expect(loadServicePreferences()).toEqual({
      scheduledSourceEnabled: false,
      routineSourceEnabled: true,
      eventQueueSourceEnabled: true,
      routineIntervalMinutes: 25,
      maxConsecutiveErrors: 7,
    });
  });

  it("falls back to defaults for missing or wrongly typed fields", () => {
    window.localStorage.setItem(
      SERVICE_PREFERENCES_KEY,
      JSON.stringify({ schedulerEnabled: false, selectedRoutine: 42 }),
    );
    expect(loadServicePreferences()).toEqual({
      ...DEFAULT_SERVICE_PREFERENCES,
      scheduledSourceEnabled: false,
      routineSourceEnabled: false,
    });
  });

  it("clamps out-of-range numbers to the input bounds", () => {
    window.localStorage.setItem(
      SERVICE_PREFERENCES_KEY,
      JSON.stringify({ routineIntervalMinutes: 5000, maxConsecutiveErrors: 0 }),
    );
    const prefs = loadServicePreferences();
    expect(prefs.routineIntervalMinutes).toBe(1440);
    expect(prefs.maxConsecutiveErrors).toBe(1);
  });

  it("returns the defaults when stored JSON is corrupt", () => {
    window.localStorage.setItem(SERVICE_PREFERENCES_KEY, "{not json");
    expect(loadServicePreferences()).toEqual(DEFAULT_SERVICE_PREFERENCES);
  });
});

function renderApp() {
  const theme = createTheme({
    primaryColor: "dark",
    defaultRadius: "md",
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={queryClient}>
        <HashRouter future={{ v7_relativeSplatPath: true, v7_startTransition: true }}>
          <App />
        </HashRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
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

function runtimeEvent(overrides: Partial<RuntimeEvent>): RuntimeEvent {
  return makeRuntimeEvent({ trace_id: "req-1", ...overrides });
}

function traceEntry(overrides: Partial<PromptTraceEntry>): PromptTraceEntry {
  return {
    event: "llm.request",
    timestamp: "2026-06-04T01:00:00Z",
    person_id: "alice",
    brain: "brains/default.yml",
    command: "",
    target: "",
    cwd: "/workspace",
    description: "",
    transcript: "",
    prompt: "",
    response: "",
    error: "",
    fields: {},
    ...overrides,
  };
}
