import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { TFunction } from "i18next";
import { HashRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  App,
  buildCommandArgs,
  commandFailureDetail,
  decodeTraceText,
  eventBadgeColor,
  eventTypeLabel,
  formatCommandEvent,
  formatRuntimeEvent,
  isStopTimeoutPending,
  localFileHref,
  logBadgeColor,
  matchesFeedFilter,
  matchesLogFilter,
  openLocalFile,
  selectTraceFile,
  splitCommandLine,
  traceBrainLabel,
  traceFieldRows,
  traceGroupMetadata,
  upsertCommandRecord,
  type CommandRunRecord,
} from "./App";
import type {
  CommandOption,
  PromptTraceEntry,
  RuntimeEvent,
  RuntimeLog,
  RuntimeUnitStatus,
} from "./api/client";
import i18n from "./i18n";
import "./i18n";
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
      primary_config_dir: "/workspace/.guildbotics/config",
      primary_config_location: "workspace",
      primary_project_file: "/workspace/.guildbotics/config/project.yml",
      primary_project_file_exists: true,
      home_config_dir: "/home/.guildbotics/config",
      home_project_file: "/home/.guildbotics/config/project.yml",
      home_project_file_exists: false,
      active_config_dir: "/workspace/.guildbotics/config",
      active_config_location: "workspace",
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
      requestId: "req-1",
      person: "alice",
      command: "demo",
      startedAt: "2026-06-04T01:00:00Z",
      status: "running",
      ...overrides,
    };
  }

  it("prepends a new request", () => {
    const existing = [record({ requestId: "req-0", startedAt: "2026-06-04T00:00:00Z" })];

    const next = upsertCommandRecord(existing, record({ requestId: "req-1" }));

    expect(next.map((entry) => entry.requestId)).toEqual(["req-1", "req-0"]);
  });

  it("updates an existing request and keeps prior output when not provided", () => {
    const existing = [record({ requestId: "req-1", status: "running", output: "partial" })];

    const next = upsertCommandRecord(existing, record({ requestId: "req-1", status: "success" }));

    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({ status: "success", output: "partial" });
  });

  it("sorts records by startedAt descending and caps at 20", () => {
    const many = Array.from({ length: 25 }, (_, index) =>
      record({
        requestId: `req-${index}`,
        startedAt: `2026-06-04T00:${String(index).padStart(2, "0")}:00Z`,
      }),
    );

    const next = upsertCommandRecord([], many[0]);
    const filled = many.slice(1).reduce((acc, item) => upsertCommandRecord(acc, item), next);

    expect(filled).toHaveLength(20);
    expect(filled[0]?.requestId).toBe("req-24");
  });
});

describe("commandFailureDetail", () => {
  it("serializes request id, type, and payload as pretty JSON", () => {
    const detail = commandFailureDetail(
      runtimeEvent({
        type: "command.failed",
        request_id: "req-9",
        payload: { code: "boom", message: "oops" },
      }),
    );

    expect(JSON.parse(detail)).toEqual({
      request_id: "req-9",
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
    expect(formatCommandEvent(runtimeEvent({ request_id: "req-7", payload: {} }))).toBe("req-7");
    expect(
      formatCommandEvent(runtimeEvent({ type: "command.failed", request_id: null, payload: {} })),
    ).toBe("command.failed");
  });
});

describe("matchesFeedFilter", () => {
  it("passes everything for all", () => {
    expect(matchesFeedFilter(runtimeEvent({ type: "scheduler.running" }), "all")).toBe(true);
  });

  it("matches failed or error events for the error filter", () => {
    expect(matchesFeedFilter(runtimeEvent({ type: "command.failed" }), "error")).toBe(true);
    expect(matchesFeedFilter(runtimeEvent({ type: "runtime.error" }), "error")).toBe(true);
    expect(matchesFeedFilter(runtimeEvent({ type: "command.finished" }), "error")).toBe(false);
  });

  it("matches by event prefix for command/scheduler/events filters", () => {
    expect(matchesFeedFilter(runtimeEvent({ type: "command.started" }), "command")).toBe(true);
    expect(matchesFeedFilter(runtimeEvent({ type: "scheduler.running" }), "scheduler")).toBe(true);
    expect(matchesFeedFilter(runtimeEvent({ type: "events.running" }), "events")).toBe(true);
    expect(matchesFeedFilter(runtimeEvent({ type: "scheduler.running" }), "command")).toBe(false);
  });
});

describe("matchesLogFilter", () => {
  function log(overrides: Partial<RuntimeLog>): RuntimeLog {
    return {
      level: "INFO",
      message: "hello world",
      request_id: null,
      timestamp: "2026-06-04T01:00:00Z",
      ...overrides,
    };
  }

  it("passes everything for all", () => {
    expect(matchesLogFilter(log({}), "all")).toBe(true);
  });

  it("matches error-level logs for the error filter", () => {
    expect(matchesLogFilter(log({ level: "error" }), "error")).toBe(true);
    expect(matchesLogFilter(log({ level: "WARNING" }), "error")).toBe(true);
    expect(matchesLogFilter(log({ level: "INFO" }), "error")).toBe(false);
  });

  it("matches logs with a request id for the command filter", () => {
    expect(matchesLogFilter(log({ request_id: "req-1" }), "command")).toBe(true);
    expect(matchesLogFilter(log({ request_id: null }), "command")).toBe(false);
  });

  it("falls back to a message substring match", () => {
    expect(matchesLogFilter(log({ message: "Hello World" }), "world")).toBe(true);
    expect(matchesLogFilter(log({ message: "nope" }), "world")).toBe(false);
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

describe("formatRuntimeEvent", () => {
  it("prefers payload message", () => {
    expect(formatRuntimeEvent(t(), runtimeEvent({ payload: { message: "hi" } }))).toBe("hi");
  });

  it("formats a command summary from payload.command", () => {
    expect(formatRuntimeEvent(t(), runtimeEvent({ payload: { command: "demo" } }))).toBe(
      i18n.t("overview.eventSummaries.command", { command: "demo", lng: "en" }),
    );
  });

  it("prefers payload.error over family summaries", () => {
    expect(
      formatRuntimeEvent(t(), runtimeEvent({ type: "scheduler.running", payload: { error: "x" } })),
    ).toBe("x");
  });

  it("returns family summaries when payload is empty", () => {
    expect(formatRuntimeEvent(t(), runtimeEvent({ type: "scheduler.running", payload: {} }))).toBe(
      i18n.t("overview.eventSummaries.schedulerRunning", { lng: "en" }),
    );
    expect(formatRuntimeEvent(t(), runtimeEvent({ type: "events.stopped", payload: {} }))).toBe(
      i18n.t("overview.eventSummaries.eventsStopped", { lng: "en" }),
    );
    expect(formatRuntimeEvent(t(), runtimeEvent({ type: "scheduler.failed", payload: {} }))).toBe(
      i18n.t("overview.eventSummaries.failed", { lng: "en" }),
    );
  });

  it("returns the raw type when nothing else matches", () => {
    expect(formatRuntimeEvent(t(), runtimeEvent({ type: "weird.thing", payload: {} }))).toBe(
      "weird.thing",
    );
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
    workflow_command: null,
    subscription_count: 0,
    listener_count: 0,
    cycle_count: 0,
    cycle_failure_count: 0,
    events_drained_count: 0,
    events_delivered_count: 0,
    events_skipped_processed_count: 0,
  };
}

function runtimeEvent(overrides: Partial<RuntimeEvent>): RuntimeEvent {
  return {
    type: "command.started",
    request_id: "req-1",
    payload: {},
    timestamp: "2026-06-04T01:00:00Z",
    ...overrides,
  };
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
