import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import {
  getConfigStatus,
  getProjectConfig,
  getSchedulerRoutines,
  getSchedulerStatus,
  getTeam,
  startScheduler,
  stopScheduler,
  type ConfigStatus,
  type ProjectConfig,
  type RoutineOption,
  type RuntimeStatus,
  type RuntimeUnitStatus,
} from "./api/client";
import i18n from "./i18n";
import "./i18n";

const t = i18n.getFixedT("en");

// jsdom lacks scrollIntoView, which Mantine's Combobox calls when opening a Select.
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
    getSchedulerRoutines: vi.fn(),
    getSchedulerStatus: vi.fn(),
    getProjectConfig: vi.fn(),
    getCommandOptions: vi.fn(async () => ({ options: [] })),
    getPromptTrace: vi.fn(async () => promptTrace()),
    startScheduler: vi.fn(),
    stopScheduler: vi.fn(),
    subscribeEvents: vi.fn(() => () => {}),
    subscribeLogs: vi.fn(() => () => {}),
  };
});

const getConfigStatusMock = vi.mocked(getConfigStatus);
const getTeamMock = vi.mocked(getTeam);
const getSchedulerRoutinesMock = vi.mocked(getSchedulerRoutines);
const getSchedulerStatusMock = vi.mocked(getSchedulerStatus);
const getProjectConfigMock = vi.mocked(getProjectConfig);
const startSchedulerMock = vi.mocked(startScheduler);
const stopSchedulerMock = vi.mocked(stopScheduler);

beforeEach(() => {
  getConfigStatusMock.mockReset().mockResolvedValue(configStatus());
  getTeamMock.mockReset().mockResolvedValue({
    project: { name: "Demo", language_code: "en", language_name: "English" },
    members: [{ person_id: "alice", name: "Alice", is_active: true, roles: ["developer"] }],
  });
  getSchedulerRoutinesMock.mockReset().mockResolvedValue({ routines: [routine()] });
  getSchedulerStatusMock.mockReset().mockResolvedValue(runtimeStatus());
  getProjectConfigMock.mockReset().mockResolvedValue(projectConfig());
  startSchedulerMock.mockReset().mockResolvedValue(runtimeStatus());
  stopSchedulerMock.mockReset().mockResolvedValue(runtimeStatus());
});

describe("Service Runtime screen", () => {
  it("sends only=scheduler when only the scheduler target is enabled", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(eventsSwitch());
    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(startSchedulerMock).toHaveBeenCalledTimes(1));
    expect(startSchedulerMock.mock.calls[0][0]).toMatchObject({ only: "scheduler" });
  });

  it("includes the selected routine in routine_commands when scheduler is enabled", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(startSchedulerMock).toHaveBeenCalledTimes(1));
    expect(startSchedulerMock.mock.calls[0][0]).toMatchObject({
      routine_commands: ["workflows/ticket_driven_workflow"],
    });
  });

  it("omits routine_commands and sends only=events when events-only is selected", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(schedulerSwitch());
    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(startSchedulerMock).toHaveBeenCalledTimes(1));
    const body = startSchedulerMock.mock.calls[0][0];
    expect(body).toMatchObject({ only: "events" });
    expect(body).not.toHaveProperty("routine_commands");
  });

  it("disables start when both targets are disabled", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(schedulerSwitch());
    await user.click(eventsSwitch());

    expect(screen.getByRole("button", { name: t("overview.start") })).toBeDisabled();
    expect(screen.getByText(t("service.noTargetTitle"))).toBeInTheDocument();
  });

  it("disables start and shows a setup link when project config is missing", async () => {
    getConfigStatusMock.mockResolvedValue(
      configStatus({ primary_project_file_exists: false, home_project_file_exists: false }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: t("overview.start") })).toBeDisabled(),
    );
    expect(screen.getByText(t("overview.setupRequiredTitle"))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: t("overview.openSetup") })).toHaveAttribute(
      "href",
      "/setup",
    );
  });

  it("reflects routine interval and max consecutive errors in the start payload", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const interval = screen.getByRole("textbox", { name: t("overview.routineIntervalMinutes") });
    await user.clear(interval);
    await user.type(interval, "30");
    const maxErrors = screen.getByRole("textbox", { name: t("overview.maxConsecutiveErrors") });
    await user.clear(maxErrors);
    await user.type(maxErrors, "7");

    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(startSchedulerMock).toHaveBeenCalledTimes(1));
    expect(startSchedulerMock.mock.calls[0][0]).toMatchObject({
      routine_interval_minutes: 30,
      max_consecutive_errors: 7,
    });
  });

  it("blocks start when the selected routine requires GitHub but GitHub is disabled", async () => {
    getSchedulerRoutinesMock.mockResolvedValue({
      routines: [routine({ command: "workflows/github_flow", requires_github: true })],
    });
    getProjectConfigMock.mockResolvedValue(projectConfig({ github_enabled: false }));
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: t("overview.start") })).toBeDisabled(),
    );
    expect(screen.getByText(t("overview.startGuardTitle"))).toBeInTheDocument();
    expect(startSchedulerMock).not.toHaveBeenCalled();
  });

  it("renders the running state and offers a stop action", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "running", running: true }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await screen.findByRole("button", { name: t("overview.stop") });
    expect(screen.getAllByText(t("overview.runtimeStates.running")).length).toBeGreaterThan(0);
  });

  it("renders the failed state with the runtime error message", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "failed", error: "worker crashed" }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    expect(await screen.findByText(t("overview.runtimeStates.failed"))).toBeInTheDocument();
    expect(screen.getByText("worker crashed")).toBeInTheDocument();
  });

  it("shows the stop-timeout pending warning instead of an error", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", {
          state: "failed",
          running: true,
          error: "worker did not stop before timeout",
        }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    expect(await screen.findByText(t("overview.stopDelayHint"))).toBeInTheDocument();
    expect(screen.getAllByText(t("overview.runtimeStates.stopping")).length).toBeGreaterThan(0);
  });

  it("shows an alert when the start mutation fails", async () => {
    const user = userEvent.setup();
    startSchedulerMock.mockRejectedValue(new Error("start blew up"));
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    expect(await screen.findByText(t("overview.startError"))).toBeInTheDocument();
    expect(screen.getByText("start blew up")).toBeInTheDocument();
  });

  it("shows an alert when the stop mutation fails", async () => {
    const user = userEvent.setup();
    stopSchedulerMock.mockRejectedValue(new Error("stop blew up"));
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "running", running: true }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(await screen.findByRole("button", { name: t("overview.stop") }));

    expect(await screen.findByText(t("overview.stopError"))).toBeInTheDocument();
    expect(screen.getByText("stop blew up")).toBeInTheDocument();
  });
});

describe("App routing and layout", () => {
  it("redirects to /setup when the project config is missing", async () => {
    getConfigStatusMock.mockResolvedValue(
      configStatus({ primary_project_file_exists: false, home_project_file_exists: false }),
    );
    renderApp("/");

    expect(await screen.findByText("Setup Mock")).toBeInTheDocument();
  });

  it("redirects to /service when the project config exists", async () => {
    renderApp("/");

    expect(await screen.findByRole("heading", { name: t("service.title") })).toBeInTheDocument();
  });

  it("redirects /overview to /service", async () => {
    renderApp("/overview");

    expect(await screen.findByRole("heading", { name: t("service.title") })).toBeInTheDocument();
  });

  it("marks the active nav item based on the current route", async () => {
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const serviceLink = screen.getByRole("link", { name: new RegExp(t("app.nav.service")) });
    expect(serviceLink).toHaveClass("active");
    const setupLink = screen.getByRole("link", { name: new RegExp(t("app.nav.setup")) });
    expect(setupLink).not.toHaveClass("active");
  });

  it("calls setAppLanguage when the language select changes", async () => {
    const user = userEvent.setup();
    const changeSpy = vi.spyOn(i18n, "changeLanguage");
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(screen.getByRole("textbox", { name: t("app.language.label") }));
    await user.click(await screen.findByText(t("app.language.japanese")));

    await waitFor(() => expect(changeSpy).toHaveBeenCalledWith("ja"));
    changeSpy.mockRestore();
    await i18n.changeLanguage("en");
  });
});

function renderApp(initialPath: string) {
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

function schedulerSwitch() {
  return within(panelFor(t("overview.schedulerCard.title"))).getByRole("switch", {
    name: t("service.startTarget"),
  });
}

function eventsSwitch() {
  return within(panelFor(t("overview.eventsCard.title"))).getByRole("switch", {
    name: t("service.startTarget"),
  });
}

function panelFor(title: string): HTMLElement {
  const heading = screen.getByText(title);
  const panel = heading.closest(".service-unit-panel");
  if (!(panel instanceof HTMLElement)) {
    throw new Error(`Unable to find service unit panel for ${title}`);
  }
  return panel;
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
    github_enabled: false,
    github_project_url: "",
    github_repository_url: "",
    repo_base_url: "https://github.com",
    has_google_api_key: false,
    has_openai_api_key: true,
    has_anthropic_api_key: false,
    ...overrides,
  };
}

function routine(overrides: Partial<RoutineOption> = {}): RoutineOption {
  return {
    command: "workflows/ticket_driven_workflow",
    requires_github: false,
    ...overrides,
  };
}

function runtimeStatus(overrides: Partial<RuntimeStatus> = {}): RuntimeStatus {
  return {
    scheduler: runtimeUnit("scheduler"),
    events: runtimeUnit("events"),
    ...overrides,
  };
}

function runtimeUnit(
  target: "scheduler" | "events",
  overrides: Partial<RuntimeUnitStatus> = {},
): RuntimeUnitStatus {
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
    ...overrides,
  };
}

function promptTrace() {
  return {
    enabled: false,
    env_file: "",
    env_file_exists: false,
    trace_file: "",
    output_trace_file: "",
    default_trace_file: "",
    trace_file_exists: false,
    event_count: 0,
    events: [],
  };
}
