import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import {
  getCommandOptions,
  getConfigStatus,
  getTeam,
  runCommand,
  subscribeEvents,
  subscribeLogs,
  type CommandOption,
  type ConfigStatus,
  type RuntimeEvent,
  type RuntimeLog,
} from "./api/client";
import i18n from "./i18n";
import "./i18n";

const t = i18n.getFixedT("en");

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

vi.mock("@tauri-apps/plugin-shell", () => ({ open: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(), save: vi.fn() }));
vi.mock("./setup/SetupPage", () => ({ SetupPage: () => <div>Setup Mock</div> }));

let eventListener: ((event: RuntimeEvent) => void) | null = null;
let logListener: ((log: RuntimeLog) => void) | null = null;

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    getConfigStatus: vi.fn(),
    getTeam: vi.fn(),
    getCommandOptions: vi.fn(),
    getPromptTrace: vi.fn(async () => promptTrace()),
    runCommand: vi.fn(),
    subscribeEvents: vi.fn(),
    subscribeLogs: vi.fn(),
  };
});

const getConfigStatusMock = vi.mocked(getConfigStatus);
const getTeamMock = vi.mocked(getTeam);
const getCommandOptionsMock = vi.mocked(getCommandOptions);
const runCommandMock = vi.mocked(runCommand);
const subscribeEventsMock = vi.mocked(subscribeEvents);
const subscribeLogsMock = vi.mocked(subscribeLogs);

beforeEach(() => {
  eventListener = null;
  logListener = null;
  getConfigStatusMock.mockReset().mockResolvedValue(configStatus());
  getTeamMock.mockReset().mockResolvedValue(team());
  getCommandOptionsMock.mockReset().mockResolvedValue({ options: [catalogCommand()] });
  runCommandMock.mockReset().mockResolvedValue({ request_id: "req-1", output: "hello output" });
  subscribeEventsMock.mockReset().mockImplementation((listener) => {
    eventListener = listener;
    return () => {
      eventListener = null;
    };
  });
  subscribeLogsMock.mockReset().mockImplementation((listener) => {
    logListener = listener;
    return () => {
      logListener = null;
    };
  });
});

describe("Commands screen", () => {
  it("shows the no-active-member blocked alert and disables run", async () => {
    getTeamMock.mockResolvedValue(team({ members: [member({ is_active: false })] }));
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    expect(await screen.findByText(t("commands.noMembersTitle"))).toBeInTheDocument();
    expect(screen.getByText(t("commands.noMembersBody"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("commands.run") })).toBeDisabled();
  });

  it("renders an argument form for the selected catalog command", async () => {
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    expect(await screen.findByRole("textbox", { name: "topic *" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "mode" })).toBeInTheDocument();
  });

  it("builds the runCommand payload from positional and keyword inputs", async () => {
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    await user.type(await screen.findByRole("textbox", { name: "topic *" }), " release ");
    await user.type(screen.getByRole("textbox", { name: "mode" }), "fast");
    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock.mock.calls[0][0]).toMatchObject({
      command: "workflows/sample",
      args: ["release", "mode=fast"],
      person: "alice",
    });
  });

  it("puts the selected member into the person field", async () => {
    const user = userEvent.setup();
    getTeamMock.mockResolvedValue(
      team({
        members: [member(), member({ person_id: "bob", name: "Bob" })],
      }),
    );
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    await user.click(await screen.findByRole("textbox", { name: t("commands.member") }));
    await user.click(await screen.findByText("Bob (bob)"));
    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock.mock.calls[0][0]).toMatchObject({ person: "bob" });
  });

  it("sends raw args in custom-command mode", async () => {
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await screen.findByRole("textbox", { name: "topic *" });

    await user.click(screen.getByRole("radio", { name: t("commands.modeCustom") }));
    await user.type(
      screen.getByRole("textbox", { name: t("commands.command") }),
      "scripts/custom.sh",
    );
    await user.type(
      screen.getByRole("textbox", { name: t("commands.rawArgs") }),
      'a "b c" key=value',
    );
    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock.mock.calls[0][0]).toMatchObject({
      command: "scripts/custom.sh",
      args: ["a", "b c", "key=value"],
    });
  });

  it("includes the cwd advanced input in the payload", async () => {
    const user = userEvent.setup();
    getCommandOptionsMock.mockResolvedValue({
      options: [catalogCommand({ arguments: [] })],
    });
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    await user.click(await screen.findByRole("switch", { name: t("commands.advanced") }));
    await user.type(screen.getByRole("textbox", { name: t("commands.cwd") }), "/tmp/run-here");
    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock.mock.calls[0][0]).toMatchObject({ cwd: "/tmp/run-here" });
  });

  it("disables run and shows the blocked alert when requirements are unsatisfied", async () => {
    getCommandOptionsMock.mockResolvedValue({
      options: [
        catalogCommand({
          requirements: [{ kind: "github", satisfied: false, message: "missing" }],
        }),
      ],
    });
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    expect(await screen.findByText(t("commands.requirementsBlockedTitle"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("commands.run") })).toBeDisabled();
    expect(runCommandMock).not.toHaveBeenCalled();
  });

  it("updates history on command.started, command.finished, and command.failed events", async () => {
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await waitFor(() => expect(eventListener).not.toBeNull());

    await act(() =>
      eventListener?.({
        type: "command.started",
        request_id: "evt-1",
        payload: { command: "workflows/sample", person: "alice" },
        timestamp: "2026-06-04T01:00:00Z",
      }),
    );
    expect(await screen.findByText(t("commands.status.running"))).toBeInTheDocument();

    await act(() =>
      eventListener?.({
        type: "command.finished",
        request_id: "evt-1",
        payload: { command: "workflows/sample", person: "alice" },
        timestamp: "2026-06-04T01:00:01Z",
      }),
    );
    expect(await screen.findByText(t("commands.status.success"))).toBeInTheDocument();

    await act(() =>
      eventListener?.({
        type: "command.failed",
        request_id: "evt-1",
        payload: { command: "workflows/sample", person: "alice", code: "boom" },
        timestamp: "2026-06-04T01:00:02Z",
      }),
    );
    expect(await screen.findByText(t("commands.status.failed"))).toBeInTheDocument();
  });

  it("ties command logs to the active request id", async () => {
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await waitFor(() => expect(eventListener).not.toBeNull());

    await act(() =>
      eventListener?.({
        type: "command.started",
        request_id: "evt-9",
        payload: { command: "workflows/sample", person: "alice" },
        timestamp: "2026-06-04T01:00:00Z",
      }),
    );
    await act(() =>
      logListener?.({
        level: "INFO",
        message: "log for this request",
        request_id: "evt-9",
        timestamp: "2026-06-04T01:00:01Z",
      }),
    );
    await act(() =>
      logListener?.({
        level: "INFO",
        message: "log for another request",
        request_id: "other",
        timestamp: "2026-06-04T01:00:02Z",
      }),
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: t("commands.logs") }));

    expect(await screen.findByText("log for this request")).toBeInTheDocument();
    expect(screen.queryByText("log for another request")).not.toBeInTheDocument();
  });

  it("copies the script path via the clipboard outside Tauri", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    const scriptLink = await screen.findByRole("link", {
      name: "/workspace/commands/sample.py",
    });
    expect(scriptLink).toHaveAttribute("href", "file:///workspace/commands/sample.py");

    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    fireEvent.click(screen.getByRole("button", { name: t("commands.copyScriptPath") }));

    expect(writeText).toHaveBeenCalledWith("/workspace/commands/sample.py");
  });

  it("opens the script through the Tauri shell instead of following the link", async () => {
    const shell = await import("@tauri-apps/plugin-shell");
    const openMock = vi.mocked(shell.open);
    openMock.mockClear();
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    await user.click(await screen.findByRole("link", { name: "/workspace/commands/sample.py" }));

    await waitFor(() => expect(openMock).toHaveBeenCalledWith("/workspace/commands/sample.py"));
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });
});

function act(callback: () => void): Promise<void> {
  return waitFor(() => {
    callback();
  });
}

function renderCommands() {
  const theme = createTheme({ primaryColor: "dark", defaultRadius: "md" });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/commands"]}>
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

type TeamResponse = Awaited<ReturnType<typeof getTeam>>;
type Member = TeamResponse["members"][number];

function member(overrides: Partial<Member> = {}): Member {
  return {
    person_id: "alice",
    name: "Alice",
    is_active: true,
    roles: ["developer"],
    ...overrides,
  };
}

function team(overrides: Partial<TeamResponse> = {}): TeamResponse {
  return {
    project: { name: "Demo", language_code: "en", language_name: "English" },
    members: [member()],
    ...overrides,
  };
}

function catalogCommand(overrides: Partial<CommandOption> = {}): CommandOption {
  return {
    command: "workflows/sample",
    label: "Sample workflow",
    description: "A sample command",
    category: "workflow",
    source: "workspace",
    path: "/workspace/commands/sample.py",
    arguments: [
      { name: "topic", kind: "positional", required: true, default: "" },
      { name: "mode", kind: "keyword", required: false, default: "" },
    ],
    supports_raw_args: true,
    recommended_input: "",
    requirements: [],
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
