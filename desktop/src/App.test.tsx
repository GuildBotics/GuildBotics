import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { HashRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { PromptTraceEntry } from "./api/client";
import "./i18n";
import { buildTraceGroups } from "./trace";

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
      github_repository_url: "",
      repo_base_url: "https://github.com",
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

function runtimeUnit(target: "scheduler" | "events") {
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
