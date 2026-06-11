import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import {
  getConfigStatus,
  getProjectConfig,
  getPromptTrace,
  getTeam,
  runScenarioDiagnostics,
  subscribeEvents,
  subscribeLogs,
  updatePromptTrace,
  type ConfigStatus,
  type DiagnosticCheck,
  type ProjectConfig,
  type PromptTraceEntry,
  type PromptTraceStatus,
  type RuntimeEvent,
  type RuntimeLog,
  type RuntimeStatus,
  type RuntimeUnitStatus,
  type ScenarioDiagnosticsResponse,
  type StreamStatus,
} from "./api/client";
import i18n from "./i18n";
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
    getPromptTrace: vi.fn(),
    updatePromptTrace: vi.fn(),
    verify: vi.fn(),
    runScenarioDiagnostics: vi.fn(),
    subscribeEvents: vi.fn(),
    subscribeLogs: vi.fn(),
  };
});

// Drivers captured from the mocked websocket subscriptions, so tests can push
// events/logs and toggle stream status exactly like the backend would.
let eventStreams: Array<{
  onEvent: (event: RuntimeEvent) => void;
  onStatus?: (status: StreamStatus) => void;
}>;
let logStreams: Array<{
  onLog: (log: RuntimeLog) => void;
  onStatus?: (status: StreamStatus) => void;
}>;

beforeEach(() => {
  eventStreams = [];
  logStreams = [];
  vi.mocked(getConfigStatus).mockReset().mockResolvedValue(configStatus());
  vi.mocked(getTeam)
    .mockReset()
    .mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [
        { person_id: "alice", name: "Alice", is_active: true, roles: ["developer"] },
        { person_id: "bob", name: "Bob", is_active: false, roles: ["reviewer"] },
      ],
    });
  vi.mocked(getProjectConfig).mockReset().mockResolvedValue(projectConfig());
  vi.mocked(getPromptTrace).mockReset().mockResolvedValue(promptTrace());
  vi.mocked(updatePromptTrace)
    .mockReset()
    .mockResolvedValue(promptTrace({ enabled: true, output_trace_file: "/workspace/trace.jsonl" }));
  vi.mocked(runScenarioDiagnostics).mockReset().mockResolvedValue(scenarioResponse());
  vi.mocked(subscribeEvents)
    .mockReset()
    .mockImplementation((onEvent, onStatus) => {
      eventStreams.push({ onEvent, onStatus });
      onStatus?.("connecting");
      return () => {};
    });
  vi.mocked(subscribeLogs)
    .mockReset()
    .mockImplementation((onLog, onStatus) => {
      logStreams.push({ onLog, onStatus });
      onStatus?.("connecting");
      return () => {};
    });
});

function emitEvent(event: RuntimeEvent) {
  act(() => {
    for (const stream of eventStreams) {
      stream.onEvent(event);
    }
  });
}

function emitLog(log: RuntimeLog) {
  act(() => {
    for (const stream of logStreams) {
      stream.onLog(log);
    }
  });
}

function setEventStreamStatus(status: StreamStatus) {
  act(() => {
    for (const stream of eventStreams) {
      stream.onStatus?.(status);
    }
  });
}

describe("Diagnostics readiness tab", () => {
  it("renders the config / env / member / github readiness badges", async () => {
    renderApp();
    await screen.findByRole("heading", { name: t("diagnostics.title") });

    expect(await screen.findByText(t("overview.ready"))).toBeInTheDocument();
    expect(screen.getByText(t("overview.found"))).toBeInTheDocument();
    expect(screen.getByText(t("overview.enabled"))).toBeInTheDocument();
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

describe("Diagnostics prompt trace tab", () => {
  it("applies an edited read path and refetches with the new path", async () => {
    const user = userEvent.setup();
    renderApp();
    await openTab(user, t("diagnostics.tabs.promptTrace"));

    const field = await screen.findByLabelText(t("overview.promptTrace.readPath"));
    await user.clear(field);
    await user.type(field, "/custom/trace.jsonl");
    await user.keyboard("{Enter}");

    await waitFor(() =>
      expect(
        vi.mocked(getPromptTrace).mock.calls.some((call) => call[1] === "/custom/trace.jsonl"),
      ).toBe(true),
    );
  });

  it("resets the read path back to the default trace file", async () => {
    const user = userEvent.setup();
    renderApp();
    await openTab(user, t("diagnostics.tabs.promptTrace"));

    const field = await screen.findByLabelText(t("overview.promptTrace.readPath"));
    await waitFor(() => expect(field).toHaveValue("/workspace/.guildbotics/trace.jsonl"));
    await user.clear(field);
    await user.type(field, "/other/path.jsonl");

    await user.click(
      screen.getByRole("button", { name: t("overview.promptTrace.resetDefaultPath") }),
    );

    // Reset loads the default trace file and refetches the trace list with it.
    await waitFor(() =>
      expect(
        vi
          .mocked(getPromptTrace)
          .mock.calls.some((call) => call[1] === "/workspace/.guildbotics/default.jsonl"),
      ).toBe(true),
    );
  });

  // The prompt-trace OUTPUT settings (enable switch + output path) live in the
  // PromptTraceOutputSettings panel rendered on the Service Runtime page, not on
  // the Diagnostics prompt-trace tab (which only shows the read path). See the
  // spec-vs-source note in the session report.
  it("toggles the runtime output trace on and persists the choice", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(
      await screen.findByRole("switch", { name: t("overview.promptTrace.disabled") }),
    );

    await waitFor(() => expect(updatePromptTrace).toHaveBeenCalledTimes(1));
    expect(vi.mocked(updatePromptTrace).mock.calls[0][0]).toMatchObject({ enabled: true });
  });

  it("updates the output trace path through updatePromptTrace", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const field = await screen.findByLabelText(t("overview.promptTrace.outputPath"));
    await user.clear(field);
    await user.type(field, "/workspace/new-output.jsonl");
    await user.keyboard("{Enter}");

    await waitFor(() => expect(updatePromptTrace).toHaveBeenCalledTimes(1));
    expect(vi.mocked(updatePromptTrace).mock.calls[0][0]).toMatchObject({
      trace_path: "/workspace/new-output.jsonl",
    });
  });

  it("opens the trace details drawer when a trace row is selected", async () => {
    const user = userEvent.setup();
    vi.mocked(getPromptTrace).mockResolvedValue(
      promptTrace({
        event_count: 2,
        // Trace events arrive newest-first, so the response precedes its request;
        // buildTraceGroups pairs a response with the earlier-listed request.
        events: [
          traceEntry({
            event: "llm.response",
            person_id: "alice",
            brain: "brains/writer.yml",
            response: "Here is the summary",
          }),
          traceEntry({
            event: "llm.request",
            person_id: "alice",
            brain: "brains/writer.yml",
            prompt: "Write a summary",
          }),
        ],
      }),
    );
    renderApp();
    await openTab(user, t("diagnostics.tabs.promptTrace"));

    const row = await screen.findByRole("button", { name: /writer/ });
    await user.click(row);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Write a summary")).toBeInTheDocument();
    expect(within(dialog).getByText("Here is the summary")).toBeInTheDocument();
  });
});

describe("Diagnostics runtime stream tab", () => {
  it("renders streamed events and switches to the logs view", async () => {
    const user = userEvent.setup();
    renderApp();
    await openTab(user, t("diagnostics.tabs.runtimeStream"));

    emitEvent(runtimeEvent({ type: "scheduler.running" }));
    expect(
      await screen.findByText(t("overview.eventSummaries.schedulerRunning")),
    ).toBeInTheDocument();

    emitLog(runtimeLog({ message: "worker started", level: "INFO" }));
    // Logs are not shown while the events view is active.
    expect(screen.queryByText("worker started")).not.toBeInTheDocument();

    // The events/logs view switcher is a SegmentedControl; its "Logs" segment
    // label is unique on the runtime stream tab.
    await user.click(screen.getByText(t("overview.logs")));
    expect(await screen.findByText("worker started")).toBeInTheDocument();
  });

  it("filters the event feed by the selected category", async () => {
    const user = userEvent.setup();
    renderApp();
    await openTab(user, t("diagnostics.tabs.runtimeStream"));

    emitEvent(runtimeEvent({ type: "command.failed", payload: { message: "boom" } }));
    emitEvent(runtimeEvent({ type: "scheduler.running" }));
    expect(await screen.findByText("boom")).toBeInTheDocument();

    await user.click(screen.getByText(t("overview.feedFilters.error")));

    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(
      screen.queryByText(t("overview.eventSummaries.schedulerRunning")),
    ).not.toBeInTheDocument();
  });

  it("reflects the websocket connection status in the stream badge", async () => {
    const user = userEvent.setup();
    renderApp();
    await openTab(user, t("diagnostics.tabs.runtimeStream"));

    expect(
      (await screen.findAllByText(t("overview.streamStates.connecting"))).length,
    ).toBeGreaterThan(0);

    setEventStreamStatus("connected");
    expect(await screen.findByText(t("overview.streamStates.connected"))).toBeInTheDocument();
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
    has_google_api_key: false,
    has_openai_api_key: true,
    has_anthropic_api_key: false,
    ...overrides,
  };
}

function promptTrace(overrides: Partial<PromptTraceStatus> = {}): PromptTraceStatus {
  return {
    enabled: false,
    env_file: "/workspace/.env",
    env_file_exists: true,
    trace_file: "/workspace/.guildbotics/trace.jsonl",
    output_trace_file: "/workspace/.guildbotics/trace.jsonl",
    default_trace_file: "/workspace/.guildbotics/default.jsonl",
    trace_file_exists: true,
    event_count: 0,
    events: [],
    ...overrides,
  };
}

function traceEntry(overrides: Partial<PromptTraceEntry> = {}): PromptTraceEntry {
  return {
    event: "llm.request",
    timestamp: "2026-01-01T00:00:00Z",
    person_id: "alice",
    brain: "brains/writer.yml",
    command: "",
    target: "",
    cwd: "",
    description: "",
    transcript: "",
    prompt: "",
    response: "",
    error: "",
    fields: {},
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

function runtimeEvent(overrides: Partial<RuntimeEvent> = {}): RuntimeEvent {
  return {
    type: "scheduler.running",
    request_id: null,
    payload: {},
    timestamp: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function runtimeLog(overrides: Partial<RuntimeLog> = {}): RuntimeLog {
  return {
    level: "INFO",
    message: "log line",
    request_id: null,
    timestamp: "2026-01-01T00:00:00Z",
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
