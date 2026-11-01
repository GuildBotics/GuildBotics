import { createTheme } from "@mantine/core";
import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import {
  getConfigStatus,
  getSchedulerStatus,
  getServiceSettings,
  getTeam,
  resetChatReceiveState,
  startScheduler,
  stopScheduler,
  updateServiceSettings,
  type ConfigStatus,
  type RuntimeStatus,
  type RuntimeUnitStatus,
  type ServiceSettings,
} from "./api/client";
import i18n from "./i18n";
import { TestMantineProvider } from "./test/TestMantineProvider";
import "./i18n";

const t = i18n.getFixedT("en");

// jsdom lacks scrollIntoView, which Mantine's Combobox calls when opening a Select.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

vi.mock("./setup/SetupPage", () => ({ SetupPage: () => <div>Setup Mock</div> }));

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    getConfigStatus: vi.fn(),
    getTeam: vi.fn(),
    getSchedulerStatus: vi.fn(),
    getServiceSettings: vi.fn(),
    updateServiceSettings: vi.fn(),
    getCommandOptions: vi.fn(async () => ({ options: [] })),
    getRoutineCommandOptions: vi.fn(async () => ({ options: [] })),
    startScheduler: vi.fn(),
    stopScheduler: vi.fn(),
    resetChatReceiveState: vi.fn(),
    subscribeEvents: vi.fn(() => () => {}),
  };
});

const getConfigStatusMock = vi.mocked(getConfigStatus);
const getTeamMock = vi.mocked(getTeam);
const getSchedulerStatusMock = vi.mocked(getSchedulerStatus);
const startSchedulerMock = vi.mocked(startScheduler);
const stopSchedulerMock = vi.mocked(stopScheduler);
const resetChatReceiveStateMock = vi.mocked(resetChatReceiveState);
const getServiceSettingsMock = vi.mocked(getServiceSettings);
const updateServiceSettingsMock = vi.mocked(updateServiceSettings);

// The server's saved settings: every screen reads them, each change is merged
// into them, and every start runs with them.
let saved: ServiceSettings;

beforeEach(() => {
  saved = {
    keep_awake: false,
    scheduled: true,
    routine: true,
    event_queue: true,
    routine_interval_minutes: 10,
    max_consecutive_errors: 3,
  };
  getConfigStatusMock.mockReset().mockResolvedValue(configStatus());
  getTeamMock.mockReset().mockResolvedValue({
    project: { name: "Demo", language_code: "en", language_name: "English" },
    members: [{ person_id: "alice", name: "Alice", is_active: true, roles: ["developer"] }],
    default_person_id: "alice",
  });
  getSchedulerStatusMock.mockReset().mockResolvedValue(runtimeStatus());
  startSchedulerMock.mockReset().mockResolvedValue(runtimeStatus());
  stopSchedulerMock.mockReset().mockResolvedValue(runtimeStatus());
  resetChatReceiveStateMock.mockReset().mockResolvedValue({ members_reset: 1, channels_reset: 3 });
  getServiceSettingsMock.mockReset().mockImplementation(async () => structuredClone(saved));
  updateServiceSettingsMock.mockReset().mockImplementation(async (change) => {
    saved = { ...saved, ...change };
    return structuredClone(saved);
  });
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

  it("saves each change on the server and starts with what is saved", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await waitFor(() => expect(eventsSwitch()).toBeEnabled());

    await user.click(eventsSwitch());
    await waitFor(() =>
      expect(saved).toMatchObject({ scheduled: true, routine: true, event_queue: false }),
    );
    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(startSchedulerMock).toHaveBeenCalledTimes(1));
    // Only the change is sent, so what another screen saved stays.
    expect(updateServiceSettingsMock).toHaveBeenCalledWith({ event_queue: false });
    // The start sends nothing of its own: the backend reads the saved settings.
    expect(startSchedulerMock).toHaveBeenCalledWith();
  });

  it("waits to start until the last change is saved", async () => {
    let finishSave: () => void = () => undefined;
    updateServiceSettingsMock.mockImplementation(
      (change) =>
        new Promise((resolve) => {
          finishSave = () => {
            saved = { ...saved, ...change };
            resolve(structuredClone(saved));
          };
        }),
    );
    const user = userEvent.setup();
    renderApp("/service");
    await waitFor(() => expect(eventsSwitch()).toBeEnabled());
    const start = screen.getByRole("button", { name: t("overview.start") });

    await user.click(eventsSwitch());

    await waitFor(() => expect(start).toBeDisabled());
    act(() => finishSave());
    await waitFor(() => expect(start).toBeEnabled());
  });

  it("waits to start for a change made before the screen was opened again", async () => {
    let finishSave: () => void = () => undefined;
    updateServiceSettingsMock.mockImplementation(
      (change) =>
        new Promise((resolve) => {
          finishSave = () => {
            saved = { ...saved, ...change };
            resolve(structuredClone(saved));
          };
        }),
    );
    const user = userEvent.setup();
    const { rerender } = renderApp("/service");
    await waitFor(() => expect(eventsSwitch()).toBeEnabled());
    await user.click(eventsSwitch());

    // Away to another screen and back, on the same client, while it saves.
    rerender(appTree("/commands"));
    rerender(appTree("/service"));

    const start = await screen.findByRole("button", { name: t("overview.start") });
    // The server still has what the change replaces, so it is not read back.
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(getServiceSettingsMock).toHaveBeenCalledTimes(1);
    expect(eventsSwitch()).not.toBeChecked();
    expect(start).toBeDisabled();
    act(() => finishSave());
    await waitFor(() => expect(start).toBeEnabled());
    await waitFor(() => expect(getServiceSettingsMock).toHaveBeenCalledTimes(2));
    expect(eventsSwitch()).not.toBeChecked();
  });

  it("says why a change failed, also after the screen was opened again", async () => {
    let failSave: () => void = () => undefined;
    updateServiceSettingsMock.mockImplementation(
      () => new Promise((_resolve, reject) => (failSave = () => reject(new Error("disk full")))),
    );
    const user = userEvent.setup();
    const { rerender } = renderApp("/service");
    await waitFor(() => expect(eventsSwitch()).toBeEnabled());
    await user.click(eventsSwitch());

    rerender(appTree("/commands"));
    act(() => failSave());
    rerender(appTree("/service"));

    expect(await screen.findByText(t("service.saveError"))).toBeInTheDocument();
    expect(screen.getByText("disk full")).toBeInTheDocument();
    await waitFor(() => expect(eventsSwitch()).toBeChecked());
  });

  it("does not let a read already under way undo a change", async () => {
    let answerRead: () => void = () => undefined;
    let finishSave: () => void = () => undefined;
    updateServiceSettingsMock.mockImplementation(
      (change) =>
        new Promise((resolve) => {
          finishSave = () => {
            saved = { ...saved, ...change };
            resolve(structuredClone(saved));
          };
        }),
    );
    const user = userEvent.setup();
    renderApp("/service");
    await waitFor(() => expect(eventsSwitch()).toBeEnabled());
    // A read that started before the change answers with what was saved then.
    const before = structuredClone(saved);
    getServiceSettingsMock.mockImplementationOnce(
      () => new Promise((resolve) => (answerRead = () => resolve(before))),
    );
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(getServiceSettingsMock).toHaveBeenCalledTimes(2));

    await user.click(eventsSwitch());
    await act(async () => {
      answerRead();
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(eventsSwitch()).not.toBeChecked();
    act(() => finishSave());
    await waitFor(() => expect(getServiceSettingsMock).toHaveBeenCalledTimes(3));
    expect(eventsSwitch()).not.toBeChecked();
    focusManager.setFocused(undefined);
  });

  it("keeps this machine awake during the service once switched on, even while running", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({ scheduler: runtimeUnit("scheduler", { state: "running", running: true }) }),
    );
    const user = userEvent.setup();
    renderApp("/service");
    const keepAwake = await screen.findByRole("switch", { name: t("service.keepAwake.title") });
    await waitFor(() => expect(keepAwake).toBeEnabled());
    expect(keepAwake).not.toBeChecked();

    await user.click(keepAwake);

    await waitFor(() => expect(keepAwake).toBeChecked());
    expect(updateServiceSettingsMock).toHaveBeenCalledWith({ keep_awake: true });
  });

  it("shows the saved setting again and why saving a change failed", async () => {
    saved.keep_awake = true;
    updateServiceSettingsMock.mockRejectedValue(new Error("disk full"));
    const user = userEvent.setup();
    renderApp("/service");
    const keepAwake = await screen.findByRole("switch", { name: t("service.keepAwake.title") });
    await waitFor(() => expect(keepAwake).toBeChecked());

    await user.click(keepAwake);

    expect(await screen.findByText(t("service.saveError"))).toBeInTheDocument();
    expect(screen.getByText("disk full")).toBeInTheDocument();
    await waitFor(() => expect(keepAwake).toBeChecked());
  });

  it("says why it cannot read the settings and does not start", async () => {
    getServiceSettingsMock.mockRejectedValue(new Error("service.yml: broken"));
    renderApp("/service");

    expect(await screen.findByText(t("service.loadError"))).toBeInTheDocument();
    expect(screen.getByText("service.yml: broken")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("overview.start") })).toBeDisabled();
  });

  it("keeps diagnostics settings off the service screen", async () => {
    renderApp("/service");
    await screen.findByRole("heading", { name: t("service.title") });

    expect(screen.queryByText(t("diagnostics.runtimeDebug.title"))).not.toBeInTheDocument();
    expect(screen.queryByText(t("diagnostics.transcripts.title"))).not.toBeInTheDocument();
  });

  it("keeps the last source on, so there is always something to start", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    await waitFor(() => expect(eventsSwitch()).toBeEnabled());

    await user.click(routineSourceSwitch());
    await user.click(scheduledSourceSwitch());

    await waitFor(() =>
      expect(saved).toMatchObject({ scheduled: false, routine: false, event_queue: true }),
    );
    expect(eventsSwitch()).toBeChecked();
    expect(eventsSwitch()).toBeDisabled();
    expect(routineSourceSwitch()).toBeEnabled();
    expect(screen.getByRole("button", { name: t("overview.start") })).toBeEnabled();
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

  it("saves the patrol interval and the failure limit", async () => {
    const user = userEvent.setup();
    renderApp("/service");
    const interval = await screen.findByRole("textbox", {
      name: t("overview.routineIntervalMinutes"),
    });
    await waitFor(() => expect(interval).toHaveValue("10"));

    await user.clear(interval);
    await user.type(interval, "30");
    const maxErrors = screen.getByRole("textbox", { name: t("overview.maxConsecutiveErrors") });
    await user.clear(maxErrors);
    await user.type(maxErrors, "7");

    await waitFor(() =>
      expect(saved).toMatchObject({ routine_interval_minutes: 30, max_consecutive_errors: 7 }),
    );
  });

  it("shows another screen's saved settings", async () => {
    Object.assign(saved, { scheduled: false, routine: true, event_queue: false });
    saved.routine_interval_minutes = 3;
    renderApp("/service");

    await waitFor(() => expect(routineSourceSwitch()).toBeChecked());
    expect(eventsSwitch()).not.toBeChecked();
    expect(scheduledSourceSwitch()).not.toBeChecked();
    expect(screen.getByRole("textbox", { name: t("overview.routineIntervalMinutes") })).toHaveValue(
      "3",
    );
  });

  it("keeps the settings editable while the service runs, for its next start", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({ scheduler: runtimeUnit("scheduler", { state: "running", running: true }) }),
    );
    const user = userEvent.setup();
    renderApp("/service");
    await screen.findByRole("button", { name: t("overview.stop") });
    expect(screen.getByText(t("service.settingsApplyAtNextStart"))).toBeInTheDocument();
    await waitFor(() => expect(eventsSwitch()).toBeEnabled());

    await user.click(eventsSwitch());

    await waitFor(() => expect(saved.event_queue).toBe(false));
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

  it("surfaces a chat auth failure with the affected member id in the events card", async () => {
    getSchedulerStatusMock.mockResolvedValue(
      runtimeStatus({
        events: runtimeUnit("events", {
          state: "running",
          running: true,
          events_auth_failed_count: 1,
          events_auth_failed_persons: ["yuki"],
          events_chat_provider: "slack",
          member_routines: [],
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
    const user = userEvent.setup();
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
    expect(resetChatReceiveStateMock).not.toHaveBeenCalled();

    // Hover to trigger tooltip containing the stoppedOnlyHint text
    await user.hover(resetButton);
    expect(
      await screen.findByText(new RegExp(t("overview.eventsCard.chatReset.stoppedOnlyHint"))),
    ).toBeInTheDocument();
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

    await user.click(screen.getByRole("combobox", { name: t("app.language.label") }));
    await user.click(await screen.findByText(t("app.language.japanese")));

    await waitFor(() => expect(changeSpy).toHaveBeenCalledWith("ja"));
    changeSpy.mockRestore();
    await i18n.changeLanguage("en");
  });
});

let queryClient: QueryClient;

function appTree(path: string) {
  const theme = createTheme({ primaryColor: "dark", defaultRadius: "md" });
  return (
    <TestMantineProvider theme={theme}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter key={path} initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>
    </TestMantineProvider>
  );
}

function renderApp(initialPath: string) {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(appTree(initialPath));
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
    workspace: "/workspace",
    config_dir: "/workspace/.guildbotics/config",
    project_file: "/workspace/.guildbotics/config/project.yml",
    project_file_exists: true,
    storage_dir: "/workspace/.guildbotics",
    ...overrides,
  };
}

function runtimeStatus(overrides: Partial<RuntimeStatus> = {}): RuntimeStatus {
  const status = {
    scheduler: runtimeUnit("scheduler"),
    events: runtimeUnit("events"),
    ...overrides,
  };
  // What the backend reports for this state; the app reads it, never derives it.
  return {
    has_active_work:
      status.scheduler.running || status.events.running || (status.active_works ?? []).length > 0,
    ...status,
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
    events_chat_provider: "",
    member_routines: [],
    ...overrides,
  };
}
