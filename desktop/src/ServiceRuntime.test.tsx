import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getCurrentWindow } from "@tauri-apps/api/window";

import { App } from "./App";
import {
  getConfigStatus,
  getRuntimeDebug,
  getSchedulerStatus,
  getTeam,
  resetChatReceiveState,
  startScheduler,
  stopScheduler,
  updateRuntimeDebug,
  type ConfigStatus,
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
vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: vi.fn() }));
vi.mock("./setup/SetupPage", () => ({ SetupPage: () => <div>Setup Mock</div> }));

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    getConfigStatus: vi.fn(),
    getTeam: vi.fn(),
    getSchedulerStatus: vi.fn(),
    getCommandOptions: vi.fn(async () => ({ options: [] })),
    getRoutineCommandOptions: vi.fn(async () => ({ options: [] })),
    getPromptTrace: vi.fn(async () => promptTrace()),
    getRuntimeDebug: vi.fn(async () => runtimeDebug()),
    startScheduler: vi.fn(),
    stopScheduler: vi.fn(),
    resetChatReceiveState: vi.fn(),
    updateRuntimeDebug: vi.fn(async (body: { enabled: boolean }) => runtimeDebug(body)),
    subscribeEvents: vi.fn(() => () => {}),
    subscribeLogs: vi.fn(() => () => {}),
  };
});

const getConfigStatusMock = vi.mocked(getConfigStatus);
const getTeamMock = vi.mocked(getTeam);
const getSchedulerStatusMock = vi.mocked(getSchedulerStatus);
const startSchedulerMock = vi.mocked(startScheduler);
const stopSchedulerMock = vi.mocked(stopScheduler);
const resetChatReceiveStateMock = vi.mocked(resetChatReceiveState);
const getRuntimeDebugMock = vi.mocked(getRuntimeDebug);
const updateRuntimeDebugMock = vi.mocked(updateRuntimeDebug);

beforeEach(() => {
  // The Service screen now persists run-target preferences, so clear storage
  // between tests to keep each case starting from the built-in defaults.
  window.localStorage.clear();
  getConfigStatusMock.mockReset().mockResolvedValue(configStatus());
  getTeamMock.mockReset().mockResolvedValue({
    project: { name: "Demo", language_code: "en", language_name: "English" },
    members: [{ person_id: "alice", name: "Alice", is_active: true, roles: ["developer"] }],
  });
  getSchedulerStatusMock.mockReset().mockResolvedValue(runtimeStatus());
  startSchedulerMock.mockReset().mockResolvedValue(runtimeStatus());
  stopSchedulerMock.mockReset().mockResolvedValue(runtimeStatus());
  resetChatReceiveStateMock.mockReset().mockResolvedValue({ members_reset: 1, channels_reset: 3 });
  getRuntimeDebugMock.mockReset().mockResolvedValue(runtimeDebug());
  updateRuntimeDebugMock.mockReset().mockImplementation(async (body) => runtimeDebug(body));
});

describe("Service Runtime screen", () => {
  it("keeps sidebar runtime indicators hidden while stopped", async () => {
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    expect(
      screen.queryByRole("status", { name: t("app.navStatus.service.running") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("status", { name: t("app.navStatus.commands.running") }),
    ).not.toBeInTheDocument();
  });

  it("shows solid sidebar indicators for running service and manual command", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "running", running: true }),
        active_works: [
          {
            id: "manual-1",
            source: "manual",
            person_id: "alice",
            command: "demo",
            started_at: "2026-07-05T00:00:00Z",
          },
        ],
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    expect(
      await screen.findByRole("status", { name: t("app.navStatus.service.running") }),
    ).toHaveClass("running");
    expect(
      await screen.findByRole("status", { name: t("app.navStatus.commands.running") }),
    ).toHaveClass("running");
  });

  it("shows blinking sidebar indicators while stopping", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "stopping", running: true }),
        active_works: [
          {
            id: "manual-1",
            source: "manual",
            person_id: "alice",
            command: "demo",
            started_at: "2026-07-05T00:00:00Z",
          },
        ],
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    expect(
      await screen.findByRole("status", { name: t("app.navStatus.service.stopping") }),
    ).toHaveClass("stopping");
    expect(
      await screen.findByRole("status", { name: t("app.navStatus.commands.stopping") }),
    ).toHaveClass("stopping");
  });

  it("sends source selection when only scheduled and routine sources are enabled", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(eventsSwitch());
    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(startSchedulerMock).toHaveBeenCalledTimes(1));
    expect(startSchedulerMock.mock.calls[0][0]).toMatchObject({
      sources: { scheduled: true, routine: true, event_queue: false },
    });
  });

  it("omits routine_commands because patrol commands are configured per member", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(startSchedulerMock).toHaveBeenCalledTimes(1));
    expect(startSchedulerMock.mock.calls[0][0]).not.toHaveProperty("routine_commands");
  });

  it("toggles runtime debug from the service screen", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(
      await screen.findByRole("switch", { name: t("overview.runtimeDebug.disabled") }),
    );

    await waitFor(() => expect(updateRuntimeDebugMock).toHaveBeenCalledTimes(1));
    expect(updateRuntimeDebugMock.mock.calls[0][0]).toEqual({ enabled: true });
  });

  it("omits routine_commands and sends only the event queue source when event-only is selected", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(routineSourceSwitch());
    await user.click(scheduledSourceSwitch());
    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(startSchedulerMock).toHaveBeenCalledTimes(1));
    const body = startSchedulerMock.mock.calls[0][0];
    expect(body).toMatchObject({
      sources: { scheduled: false, routine: false, event_queue: true },
    });
    expect(body).not.toHaveProperty("routine_commands");
  });

  it("disables start when both targets are disabled", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(routineSourceSwitch());
    await user.click(scheduledSourceSwitch());
    await user.click(eventsSwitch());

    expect(screen.getByRole("button", { name: t("overview.start") })).toBeDisabled();
    expect(screen.getByText(t("service.noTargetTitle"))).toBeInTheDocument();
  });

  it("disables start and shows a setup link when project config is missing", async () => {
    getConfigStatusMock.mockResolvedValue(configStatus({ project_file_exists: false }));
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

  it("remembers the source toggles across remounts", async () => {
    const user = userEvent.setup();
    const first = renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(eventsSwitch());
    await user.click(scheduledSourceSwitch());
    await waitFor(() => expect(eventsSwitch()).not.toBeChecked());
    await waitFor(() => expect(scheduledSourceSwitch()).not.toBeChecked());

    first.unmount();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await waitFor(() => expect(eventsSwitch()).not.toBeChecked());
    await waitFor(() => expect(scheduledSourceSwitch()).not.toBeChecked());
    expect(routineSourceSwitch()).toBeChecked();
  });

  it("renders the running state and offers a stop action", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", {
          state: "running",
          running: true,
          scheduled_source_enabled: true,
          routine_source_enabled: true,
        }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await screen.findByRole("button", { name: t("overview.stop") });
    expect(screen.getAllByText(t("overview.runtimeStates.running")).length).toBeGreaterThan(0);
  });

  it("shows active manual work as a stop target", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        active_works: [
          {
            id: "work-1",
            source: "manual",
            person_id: "aiko",
            command: "demo",
            started_at: "2026-07-05T00:00:00Z",
          },
        ],
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    expect(await screen.findByRole("button", { name: t("overview.stop") })).toBeEnabled();
    expect(screen.getByText(t("overview.activeWork.title"))).toBeInTheDocument();
    expect(
      screen.getByText(
        t("overview.activeWork.item", {
          person: "aiko",
          source: t("overview.activeWork.sources.manual"),
          command: "demo",
        }),
      ),
    ).toBeInTheDocument();
  });

  it("shows scheduled/routine sources as stopped when only event queue workers are active", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", {
          state: "running",
          running: true,
          worker_count: 1,
          active_member_count: 1,
          scheduled_source_enabled: false,
          routine_source_enabled: false,
          event_queue_source_enabled: true,
        }),
        events: runtimeUnit("events", { state: "running", running: true }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const sourcePanel = panelFor(t("overview.routineSourceCard.title"));
    expect(
      within(sourcePanel).getAllByText(t("overview.runtimeStates.stopped")).length,
    ).toBeGreaterThan(0);
    await waitFor(() =>
      expect(within(sourcePanel).getByText(t("overview.disabled"))).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        within(panelFor(t("overview.eventsCard.title"))).getByText(
          t("overview.eventsCard.workerValue", { workers: 1 }),
        ),
      ).toBeInTheDocument(),
    );
  });

  it("renders the failed state with the runtime error message", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "failed", error: "worker crashed" }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await waitFor(() =>
      expect(screen.getAllByText(t("overview.runtimeStates.failed")).length).toBeGreaterThan(0),
    );
    expect(screen.getAllByText("worker crashed").length).toBeGreaterThan(0);
  });

  it("surfaces a Slack auth failure with the affected member id in the events card", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        events: runtimeUnit("events", {
          state: "running",
          running: true,
          events_auth_failed_count: 1,
          events_auth_failed_persons: ["yuki"],
        }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    expect(await screen.findByText(t("overview.eventsCard.authFailedTitle"))).toBeInTheDocument();
    expect(
      screen.getByText(t("overview.eventsCard.authFailedBody", { persons: "yuki" })),
    ).toBeInTheDocument();
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

    await waitFor(() =>
      expect(screen.getAllByText(t("overview.stopDelayHint")).length).toBeGreaterThan(0),
    );
    expect(screen.getAllByText(t("overview.runtimeStates.stopping")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: t("overview.forceStop") })).toBeInTheDocument();
  });

  it("sends a force stop request from the stopping state", async () => {
    const user = userEvent.setup();
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "stopping", running: true }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(await screen.findByRole("button", { name: t("overview.forceStop") }));

    await waitFor(() => expect(stopSchedulerMock).toHaveBeenCalledWith({ force: true }));
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

  it("resets chat receive state after confirming while stopped", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const resetButton = await screen.findByRole("button", {
      name: t("overview.eventsCard.chatReset.action"),
    });
    expect(resetButton).toBeEnabled();
    await user.click(resetButton);

    await user.click(
      await screen.findByRole("button", { name: t("overview.eventsCard.chatReset.confirm") }),
    );

    await waitFor(() => expect(resetChatReceiveStateMock).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText(
        t("overview.eventsCard.chatReset.successBody", { members: 1, channels: 3 }),
      ),
    ).toBeInTheDocument();
  });

  it("disables the chat receive reset while the runtime is running", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "running", running: true }),
        events: runtimeUnit("events", { state: "running", running: true }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const resetButton = await screen.findByRole("button", {
      name: t("overview.eventsCard.chatReset.action"),
    });
    expect(resetButton).toBeDisabled();
    expect(
      screen.getByText(t("overview.eventsCard.chatReset.stoppedOnlyHint")),
    ).toBeInTheDocument();
    expect(resetChatReceiveStateMock).not.toHaveBeenCalled();
  });
});

describe("App routing and layout", () => {
  it("redirects to /setup when the project config is missing", async () => {
    getConfigStatusMock.mockResolvedValue(configStatus({ project_file_exists: false }));
    renderApp("/");

    expect(await screen.findByText("Setup Mock")).toBeInTheDocument();
  });

  it("redirects to /activity when the project config exists", async () => {
    renderApp("/");

    expect(await screen.findByRole("heading", { name: t("activity.title") })).toBeInTheDocument();
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

describe("App close guard", () => {
  type CloseHandler = (event: { preventDefault: () => void }) => Promise<void>;
  let closeHandler: CloseHandler | undefined;
  const destroyMock = vi.fn();

  beforeEach(() => {
    closeHandler = undefined;
    destroyMock.mockReset();
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    vi.mocked(getCurrentWindow).mockReturnValue({
      onCloseRequested: vi.fn(async (handler: CloseHandler) => {
        closeHandler = handler;
        return () => {};
      }),
      destroy: destroyMock,
    } as unknown as ReturnType<typeof getCurrentWindow>);
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  async function requestClose() {
    await waitFor(() => expect(closeHandler).toBeDefined());
    const preventDefault = vi.fn();
    await act(async () => {
      await closeHandler!({ preventDefault });
    });
    return preventDefault;
  }

  it("allows the window to close while nothing is running", async () => {
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const preventDefault = await requestClose();

    expect(preventDefault).not.toHaveBeenCalled();
    expect(screen.queryByText(t("app.closeBlocked.body"))).not.toBeInTheDocument();
  });

  it("blocks the close with a modal while work is active and force stops on request", async () => {
    const user = userEvent.setup();
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "running", running: true }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const preventDefault = await requestClose();

    expect(preventDefault).toHaveBeenCalled();
    expect(await screen.findByText(t("app.closeBlocked.body"))).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("app.closeBlocked.force") }));

    await waitFor(() => expect(stopSchedulerMock).toHaveBeenCalledWith({ force: true }));
    await waitFor(() => expect(destroyMock).toHaveBeenCalled());
  });

  it("shows the destroy error instead of spinning forever when quitting fails", async () => {
    const user = userEvent.setup();
    destroyMock.mockRejectedValue(new Error("window.destroy not allowed"));
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "running", running: true }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await requestClose();
    await user.click(await screen.findByRole("button", { name: t("app.closeBlocked.force") }));

    expect(await screen.findByText(t("app.closeBlocked.error"))).toBeInTheDocument();
    expect(screen.getByText("window.destroy not allowed")).toBeInTheDocument();
    // The modal stays open and the force button is usable again.
    expect(screen.getByRole("button", { name: t("app.closeBlocked.force") })).toBeEnabled();
  });

  it("keeps the window open when the blocked close is cancelled", async () => {
    const user = userEvent.setup();
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        scheduler: runtimeUnit("scheduler", { state: "running", running: true }),
      }),
    );
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await requestClose();
    expect(await screen.findByText(t("app.closeBlocked.body"))).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("app.closeBlocked.cancel") }));

    await waitFor(() =>
      expect(screen.queryByText(t("app.closeBlocked.body"))).not.toBeInTheDocument(),
    );
    expect(destroyMock).not.toHaveBeenCalled();
    expect(stopSchedulerMock).not.toHaveBeenCalled();
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

function routineSourceSwitch() {
  return within(panelFor(t("overview.routineSourceCard.title"))).getByRole("switch", {
    name: t("service.sourceTarget"),
  });
}

function scheduledSourceSwitch() {
  return within(panelFor(t("overview.scheduledSourceCard.title"))).getByRole("switch", {
    name: t("service.sourceTarget"),
  });
}

function eventsSwitch() {
  return within(panelFor(t("overview.eventsCard.title"))).getByRole("switch", {
    name: t("service.sourceTarget"),
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
    config_dir: "/workspace/.guildbotics/config",
    project_file: "/workspace/.guildbotics/config/project.yml",
    project_file_exists: true,
    storage_dir: "/workspace/.guildbotics",
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

function runtimeDebug(overrides: { enabled?: boolean } = {}) {
  const enabled = overrides.enabled ?? false;
  return {
    enabled,
    log_level: enabled ? "DEBUG" : "INFO",
    agno_debug: enabled,
    env_file: "/workspace/.env",
    env_file_exists: true,
  };
}
