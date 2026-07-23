import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  App,
  CUSTOM_COMMAND_HISTORY_KEY,
  LAST_COMMAND_INPUTS_KEY,
  commandOutputText,
  loadCustomCommandHistory,
  loadLastCommandInputs,
  pushCustomCommand,
  saveCustomCommandHistory,
  saveLastCommandInputs,
  type CommandRunRecord,
  type LastCommandInputs,
} from "./App";
import {
  getCommandOptions,
  getConfigStatus,
  getTeam,
  getTraceDetail,
  runCommand,
  subscribeEvents,
  type CommandOption,
  type ConfigStatus,
  type RuntimeEvent,
} from "./api/client";
import i18n from "./i18n";
import "./i18n";
import { makeRuntimeEvent, makeTraceRecord } from "./test/factories";

const t = i18n.getFixedT("en");

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

vi.mock("@tauri-apps/plugin-shell", () => ({ open: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(), save: vi.fn() }));
vi.mock("./setup/SetupPage", () => ({ SetupPage: () => <div>Setup Mock</div> }));

let eventListener: ((event: RuntimeEvent) => void) | null = null;

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    getConfigStatus: vi.fn(),
    getTeam: vi.fn(),
    getCommandOptions: vi.fn(),
    getTraceDetail: vi.fn(),
    runCommand: vi.fn(),
    subscribeEvents: vi.fn(),
  };
});

const getConfigStatusMock = vi.mocked(getConfigStatus);
const getTeamMock = vi.mocked(getTeam);
const getCommandOptionsMock = vi.mocked(getCommandOptions);
const getTraceDetailMock = vi.mocked(getTraceDetail);
const runCommandMock = vi.mocked(runCommand);
const subscribeEventsMock = vi.mocked(subscribeEvents);

beforeEach(() => {
  eventListener = null;
  window.localStorage.clear();
  getConfigStatusMock.mockReset().mockResolvedValue(configStatus());
  getTeamMock.mockReset().mockResolvedValue(team());
  getCommandOptionsMock.mockReset().mockResolvedValue({ options: [catalogCommand()] });
  getTraceDetailMock.mockReset().mockResolvedValue({ trace_id: "", summary: null, records: [] });
  runCommandMock.mockReset().mockResolvedValue({ trace_id: "req-1", output: "hello output" });
  subscribeEventsMock.mockReset().mockImplementation((listener) => {
    eventListener = listener;
    return () => {
      eventListener = null;
    };
  });
});

describe("Commands screen", () => {
  it("keeps diagnostics settings off the command screen", async () => {
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    expect(screen.queryByText(t("diagnostics.runtimeDebug.title"))).not.toBeInTheDocument();
    expect(screen.queryByText(t("diagnostics.transcripts.title"))).not.toBeInTheDocument();
  });

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

    expect(await screen.findByRole("textbox", { name: "topic" })).toBeRequired();
    expect(screen.getByRole("textbox", { name: "mode" })).toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: t("commands.extraArgs") }),
    ).not.toBeInTheDocument();
  });

  it("renders an optional declared argument with its default", async () => {
    getCommandOptionsMock.mockResolvedValue({
      options: [
        catalogCommand({
          arguments: [
            { name: "file", kind: "keyword", required: true, default: "" },
            { name: "language", kind: "keyword", required: false, default: "English" },
          ],
        }),
      ],
    });
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    expect(await screen.findByRole("textbox", { name: "file" })).toBeRequired();
    const language = screen.getByRole("textbox", { name: "language" });
    expect(language).not.toBeRequired();
    expect(language).toHaveAttribute("placeholder", "English");
  });

  it("requires declared arguments before running", async () => {
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    const run = screen.getByRole("button", { name: t("commands.run") });
    expect(run).toBeDisabled();

    await user.type(await screen.findByRole("textbox", { name: "topic" }), "release");
    expect(run).toBeEnabled();
  });

  it("builds the runCommand payload from positional and keyword inputs", async () => {
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    await user.type(await screen.findByRole("textbox", { name: "topic" }), " release ");
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
    await user.type(screen.getByRole("textbox", { name: "topic" }), "release");
    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock.mock.calls[0][0]).toMatchObject({ person: "bob" });
  });

  it("sends extra args in custom-command mode", async () => {
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await screen.findByRole("textbox", { name: "topic" });

    await user.click(screen.getByRole("radio", { name: t("commands.modeCustom") }));
    await user.type(
      screen.getByRole("textbox", { name: t("commands.command") }),
      "scripts/custom.sh",
    );
    await user.type(
      screen.getByRole("textbox", { name: t("commands.extraArgs") }),
      'a "b c" key=value',
    );
    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock.mock.calls[0][0]).toMatchObject({
      command: "scripts/custom.sh",
      args: ["a", "b c", "key=value"],
    });
  });

  it("hides catalog inputs and omits their stale values", async () => {
    const user = userEvent.setup();
    getCommandOptionsMock.mockResolvedValue({
      options: [
        catalogCommand({
          inputs: { defined_args: "hidden", extra_args: "hidden", message: "hidden" },
        }),
      ],
    });
    saveLastCommandInputs(
      {
        mode: "catalog",
        selectedCommand: "workflows/sample",
        customCommand: "",
        extraArgs: "--stale",
        argValues: { topic: "stale" },
        message: "stale message",
        person: "alice",
        cwd: "",
        showAdvanced: false,
        history: [],
        activeTraceId: null,
        activeTab: "events",
      },
      "/workspace/.guildbotics",
    );

    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await screen.findByRole("link", { name: "/workspace/commands/sample.py" });

    expect(screen.queryByRole("textbox", { name: "topic" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: t("commands.extraArgs") }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: t("commands.message") })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("commands.run") }));
    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock).toHaveBeenCalledWith(expect.objectContaining({ args: [], message: "" }));
  });

  it("shows and sends optional extra args", async () => {
    const user = userEvent.setup();
    getCommandOptionsMock.mockResolvedValue({
      options: [
        catalogCommand({
          arguments: [],
          inputs: { defined_args: "auto", extra_args: "optional", message: "hidden" },
        }),
      ],
    });
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await screen.findByRole("link", { name: "/workspace/commands/sample.py" });

    await user.type(
      screen.getByRole("textbox", { name: t("commands.extraArgs") }),
      '--verbose "two words"',
    );
    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock).toHaveBeenCalledWith(
      expect.objectContaining({ args: ["--verbose", "two words"], message: "" }),
    );
  });

  it("requires a message when declared by the command", async () => {
    const user = userEvent.setup();
    getCommandOptionsMock.mockResolvedValue({
      options: [
        catalogCommand({
          arguments: [],
          inputs: { defined_args: "auto", extra_args: "hidden", message: "required" },
        }),
      ],
    });
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await screen.findByRole("link", { name: "/workspace/commands/sample.py" });

    const message = screen.getByRole("textbox", { name: t("commands.message") });
    expect(message).toBeRequired();
    const run = screen.getByRole("button", { name: t("commands.run") });
    expect(run).toBeDisabled();

    await user.type(message, "translate me");
    expect(run).toBeEnabled();
    await user.click(run);

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(runCommandMock).toHaveBeenCalledWith(
      expect.objectContaining({ message: "translate me" }),
    );
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
      eventListener?.(
        makeRuntimeEvent({
          type: "command.started",
          trace_id: "evt-1",
          payload: { command: "workflows/sample", person: "alice" },
          timestamp: "2026-06-04T01:00:00Z",
        }),
      ),
    );
    expect(await screen.findByText(t("commands.status.running"))).toBeInTheDocument();

    await act(() =>
      eventListener?.(
        makeRuntimeEvent({
          type: "command.finished",
          trace_id: "evt-1",
          payload: { command: "workflows/sample", person: "alice" },
          timestamp: "2026-06-04T01:00:01Z",
        }),
      ),
    );
    expect(await screen.findByText(t("commands.status.success"))).toBeInTheDocument();

    await act(() =>
      eventListener?.(
        makeRuntimeEvent({
          type: "command.failed",
          trace_id: "evt-1",
          payload: { command: "workflows/sample", person: "alice", code: "boom" },
          timestamp: "2026-06-04T01:00:02Z",
        }),
      ),
    );
    expect(await screen.findByText(t("commands.status.failed"))).toBeInTheDocument();
  });

  it("shows the diagnostics trace timeline and record details for the active run", async () => {
    const user = userEvent.setup();
    getTraceDetailMock.mockResolvedValue({
      trace_id: "evt-9",
      summary: null,
      records: [
        makeTraceRecord({
          trace_id: "evt-9",
          type: "command.started",
          message: "workflows/sample",
          presentation: {
            label_key: "diagnostics.executions.eventTypes.command_started",
            label_fallback: "command.started",
            message_key: "",
            message: "workflows/sample",
            message_params: {},
            tone: "success",
          },
        }),
        makeTraceRecord({
          trace_id: "evt-9",
          kind: "io",
          span: "llm",
          type: "llm.request",
          message: "confidence: 0.98 label: night",
          timestamp: "2026-06-04T01:00:01Z",
          payload: { model: "gpt-5" },
          presentation: {
            label_key: "diagnostics.executions.ioTypes.llm_request",
            label_fallback: "LLM request",
            message_key: "",
            message: "confidence: 0.98 label: night",
            message_params: {},
            tone: "ai",
          },
        }),
      ],
    });
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await waitFor(() => expect(eventListener).not.toBeNull());

    await act(() =>
      eventListener?.(
        makeRuntimeEvent({
          type: "command.started",
          trace_id: "evt-9",
          payload: { command: "workflows/sample", person: "alice" },
          timestamp: "2026-06-04T01:00:00Z",
        }),
      ),
    );

    expect(await screen.findByText("confidence: 0.98 label: night")).toBeInTheDocument();
    expect(getTraceDetailMock).toHaveBeenCalledWith("evt-9");
    expect(
      screen.getByText(t("diagnostics.executions.eventTypes.command_started")),
    ).toBeInTheDocument();
    expect(screen.getByText(t("diagnostics.executions.ioTypes.llm_request"))).toBeInTheDocument();

    await user.click(screen.getByText("confidence: 0.98 label: night"));
    expect(await screen.findByText(/gpt-5/)).toBeInTheDocument();
  });

  it("switches to the output tab and shows the output after a successful run", async () => {
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await user.type(await screen.findByRole("textbox", { name: "topic" }), "release");

    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("hello output")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: t("commands.output") })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("switches to the output tab and shows the error detail when a run fails", async () => {
    runCommandMock.mockRejectedValueOnce(new Error("boom failure"));
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await user.type(await screen.findByRole("textbox", { name: "topic" }), "release");

    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("boom failure")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: t("commands.output") })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("offers every recorded free-input command, newest first and unfiltered by input", async () => {
    const user = userEvent.setup();
    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await screen.findByRole("textbox", { name: "topic" });

    await user.click(screen.getByRole("radio", { name: t("commands.modeCustom") }));
    const field = screen.getByRole("textbox", { name: t("commands.command") });

    await user.type(field, "scripts/alpha.sh");
    await user.click(screen.getByRole("button", { name: t("commands.run") }));
    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));

    await user.clear(field);
    await user.type(field, "scripts/beta.sh");
    await user.click(screen.getByRole("button", { name: t("commands.run") }));
    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(2));

    // Typing a string that matches neither entry still shows both, newest first.
    await user.clear(field);
    await user.type(field, "zzz");
    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "scripts/beta.sh",
      "scripts/alpha.sh",
    ]);
  });

  it("restores free-input mode and the last command after a restart", async () => {
    const user = userEvent.setup();
    const first = renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await screen.findByRole("textbox", { name: "topic" });

    await user.click(screen.getByRole("radio", { name: t("commands.modeCustom") }));
    await user.type(
      screen.getByRole("textbox", { name: t("commands.command") }),
      "scripts/restart.sh",
    );
    await user.click(screen.getByRole("button", { name: t("commands.run") }));
    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));

    first.unmount();

    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    expect(await screen.findByRole("radio", { name: t("commands.modeCustom") })).toBeChecked();
    expect(screen.getByRole("textbox", { name: t("commands.command") })).toHaveValue(
      "scripts/restart.sh",
    );
  });

  it("keeps the catalog selected after a restart when the last run was a catalog command", async () => {
    const user = userEvent.setup();
    const first = renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    await screen.findByRole("textbox", { name: "topic" });

    // Record a free-input command first, then run a catalog command last.
    await user.click(screen.getByRole("radio", { name: t("commands.modeCustom") }));
    await user.type(
      screen.getByRole("textbox", { name: t("commands.command") }),
      "scripts/custom.sh",
    );
    await user.click(screen.getByRole("button", { name: t("commands.run") }));
    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("radio", { name: t("commands.modeCatalog") }));
    await user.type(screen.getByRole("textbox", { name: "topic" }), "release");
    await user.click(screen.getByRole("button", { name: t("commands.run") }));
    await waitFor(() => expect(runCommandMock).toHaveBeenCalledTimes(2));

    first.unmount();

    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });
    expect(await screen.findByRole("radio", { name: t("commands.modeCatalog") })).toBeChecked();
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

  it("restores the last execution inputs and outputs from localStorage on initialization", async () => {
    const inputs: LastCommandInputs = {
      mode: "custom",
      selectedCommand: "",
      customCommand: "scripts/restore-test.sh",
      extraArgs: "arg-restored",
      argValues: {},
      message: "restored-msg",
      person: "alice",
      cwd: "/workspace/restore",
      showAdvanced: true,
      history: [
        {
          traceId: "req-restore",
          person: "alice",
          command: "scripts/restore-test.sh",
          startedAt: "2026-07-20T00:00:00Z",
          status: "success",
          output: "restored output content",
        },
      ],
      activeTraceId: "req-restore",
      activeTab: "output",
    };
    saveLastCommandInputs(inputs, "/workspace/.guildbotics");

    renderCommands();
    await screen.findByRole("heading", { name: t("commands.title") });

    await waitFor(() => {
      expect(screen.getByRole("radio", { name: t("commands.modeCustom") })).toBeChecked();
      expect(screen.getByRole("textbox", { name: t("commands.command") })).toHaveValue(
        "scripts/restore-test.sh",
      );
      expect(screen.getByRole("textbox", { name: t("commands.extraArgs") })).toHaveValue(
        "arg-restored",
      );
      expect(screen.getByRole("textbox", { name: t("commands.cwd") })).toHaveValue(
        "/workspace/restore",
      );
      expect(screen.getByRole("switch", { name: t("commands.advanced") })).toBeChecked();

      // Verify output content is restored
      expect(screen.getByText("restored output content")).toBeInTheDocument();
    });
  });
});

describe("commandOutputText", () => {
  const record = (overrides: Partial<CommandRunRecord> = {}): CommandRunRecord => ({
    traceId: "req-1",
    person: "alice",
    command: "workflows/sample",
    startedAt: "2026-06-04T01:00:00Z",
    status: "success",
    ...overrides,
  });

  it("returns the raw output on success", () => {
    expect(commandOutputText(record({ status: "success", output: "done" }))).toBe("done");
  });

  it("returns an empty string when a successful run has no output", () => {
    expect(commandOutputText(record({ status: "success" }))).toBe("");
  });

  it("returns the error detail when a run fails without output", () => {
    expect(commandOutputText(record({ status: "failed", error: "boom" }))).toBe("boom");
  });

  it("appends the output below a separator when a failed run still produced output", () => {
    expect(commandOutputText(record({ status: "failed", error: "boom", output: "partial" }))).toBe(
      "boom\n---\npartial",
    );
  });

  it("falls back to the trace id when a failed run has no error detail", () => {
    expect(commandOutputText(record({ status: "failed" }))).toBe(
      JSON.stringify({ trace_id: "req-1" }, null, 2),
    );
  });
});

describe("pushCustomCommand", () => {
  it("prepends a new command as the newest entry", () => {
    expect(pushCustomCommand(["b"], "a")).toEqual(["a", "b"]);
  });

  it("moves an existing command to the top without duplicating it", () => {
    expect(pushCustomCommand(["a", "b", "c"], "c")).toEqual(["c", "a", "b"]);
  });

  it("trims whitespace and ignores empty commands", () => {
    expect(pushCustomCommand(["a"], "  b  ")).toEqual(["b", "a"]);
    expect(pushCustomCommand(["a"], "   ")).toEqual(["a"]);
  });

  it("caps the history at the given limit", () => {
    expect(pushCustomCommand(["a", "b", "c"], "d", 3)).toEqual(["d", "a", "b"]);
  });
});

describe("custom command history persistence", () => {
  beforeEach(() => window.localStorage.clear());

  it("round-trips the history and last-run mode through localStorage", () => {
    saveCustomCommandHistory({ commands: ["a", "b"], lastRunWasCustom: true });
    expect(loadCustomCommandHistory()).toEqual({ commands: ["a", "b"], lastRunWasCustom: true });
  });

  it("returns an empty history when nothing is stored", () => {
    expect(loadCustomCommandHistory()).toEqual({ commands: [], lastRunWasCustom: false });
  });

  it("falls back to an empty history when the stored value is corrupt", () => {
    window.localStorage.setItem(CUSTOM_COMMAND_HISTORY_KEY, "{not json");
    expect(loadCustomCommandHistory()).toEqual({ commands: [], lastRunWasCustom: false });
  });

  it("sanitizes a stored history with blanks, duplicates, whitespace and non-strings", () => {
    window.localStorage.setItem(
      CUSTOM_COMMAND_HISTORY_KEY,
      JSON.stringify({ commands: ["  a ", "a", "", "b", 5, "a"], lastRunWasCustom: true }),
    );
    expect(loadCustomCommandHistory()).toEqual({ commands: ["a", "b"], lastRunWasCustom: true });
  });

  it("caps an oversized stored history at the limit, keeping the newest entries", () => {
    const stored = Array.from({ length: 50 }, (_, index) => `cmd-${index}`);
    window.localStorage.setItem(
      CUSTOM_COMMAND_HISTORY_KEY,
      JSON.stringify({ commands: stored, lastRunWasCustom: false }),
    );
    const loaded = loadCustomCommandHistory().commands;
    expect(loaded).toHaveLength(30);
    expect(loaded[0]).toBe("cmd-0");
    expect(loaded[29]).toBe("cmd-29");
  });
});

describe("last command inputs persistence", () => {
  beforeEach(() => window.localStorage.clear());

  it("round-trips the last inputs through localStorage", () => {
    const inputs: LastCommandInputs = {
      mode: "custom",
      selectedCommand: "workflows/custom",
      customCommand: "echo hello",
      extraArgs: "--verbose",
      argValues: { foo: "bar" },
      message: "test commit",
      person: "bob",
      cwd: "/src",
      showAdvanced: true,
      history: [
        {
          traceId: "req-123",
          person: "alice",
          command: "workflows/sample",
          startedAt: "2026-07-20T00:00:00Z",
          status: "success",
          output: "success output",
        },
      ],
      activeTraceId: "req-123",
      activeTab: "output",
    };
    saveLastCommandInputs(inputs, "/workspace/default");
    expect(loadLastCommandInputs("/workspace/default")).toEqual(inputs);
  });

  it("returns null when nothing is stored", () => {
    expect(loadLastCommandInputs()).toBeNull();
  });

  it("falls back to default/null values when stored value is corrupt", () => {
    window.localStorage.setItem(`${LAST_COMMAND_INPUTS_KEY}:/workspace/default`, "{not json");
    expect(loadLastCommandInputs("/workspace/default")).toBeNull();
  });

  it("partially restores and sanitizes inputs from incomplete localStorage object", () => {
    window.localStorage.setItem(
      `${LAST_COMMAND_INPUTS_KEY}:/workspace/default`,
      JSON.stringify({
        mode: "custom",
        customCommand: "echo hi",
      }),
    );
    expect(loadLastCommandInputs("/workspace/default")).toEqual({
      mode: "custom",
      selectedCommand: "",
      customCommand: "echo hi",
      extraArgs: "",
      argValues: {},
      message: "",
      person: null,
      cwd: "",
      showAdvanced: false,
      history: [],
      activeTraceId: null,
      activeTab: "events",
    });
  });

  it("isolates inputs between different workspaces", () => {
    const inputs1 = {
      mode: "custom" as const,
      selectedCommand: "",
      customCommand: "echo workspace 1",
      extraArgs: "",
      argValues: {},
      message: "",
      person: null,
      cwd: "",
      showAdvanced: false,
      history: [],
      activeTraceId: null,
      activeTab: "events",
    };
    const inputs2 = {
      mode: "catalog" as const,
      selectedCommand: "test",
      customCommand: "",
      extraArgs: "arg",
      argValues: { key: "val" },
      message: "hello",
      person: "bob",
      cwd: "/ws2",
      showAdvanced: true,
      history: [],
      activeTraceId: "trace-2",
      activeTab: "output",
    };

    saveLastCommandInputs(inputs1, "/workspace/1");
    saveLastCommandInputs(inputs2, "/workspace/2");

    expect(loadLastCommandInputs("/workspace/1")).toEqual(inputs1);
    expect(loadLastCommandInputs("/workspace/2")).toEqual(inputs2);
  });

  it("filters out corrupt/null objects from arrays during load", () => {
    window.localStorage.setItem(
      `${LAST_COMMAND_INPUTS_KEY}:/workspace/default`,
      JSON.stringify({
        mode: "custom",
        argValues: { valid: "text", invalid: 123 },
        showAdvanced: "truthy string",
        history: [
          null,
          { invalidRecord: true },
          {
            traceId: "req-1",
            person: "alice",
            command: "test",
            startedAt: "2026-07-20T00:00:00Z",
            status: "success",
          },
        ],
      }),
    );

    const loaded = loadLastCommandInputs("/workspace/default");
    expect(loaded?.showAdvanced).toBe(false);
    expect(loaded?.argValues).toEqual({ valid: "text" });
    expect(loaded?.history).toHaveLength(1);
    expect(loaded?.history[0].traceId).toBe("req-1");
  });

  it("returns null when only global key exists but workspace B key is missing", () => {
    window.localStorage.setItem(
      LAST_COMMAND_INPUTS_KEY,
      JSON.stringify({
        mode: "custom",
        customCommand: "echo global",
      }),
    );
    expect(loadLastCommandInputs("/workspace/B")).toBeNull();
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
    config_dir: "/workspace/.guildbotics/config",
    project_file: "/workspace/.guildbotics/config/project.yml",
    project_file_exists: true,
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
    inputs: { defined_args: "auto", extra_args: "hidden", message: "optional" },
    requirements: [],
    ...overrides,
  };
}
