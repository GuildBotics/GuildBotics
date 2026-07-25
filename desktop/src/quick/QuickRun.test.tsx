import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getCommandOptions,
  getTeam,
  getTraceDetail,
  runCommand,
  subscribeEvents,
  type CommandOption,
  type CommandRunResponse,
  type RuntimeEvent,
  type TraceDetailResponse,
  type TraceRecord,
} from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { QuickRun } from "./QuickRun";
import { IDLE_RUN_MS, type QuickRunTrigger } from "./quickRunState";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getCommandOptions: vi.fn(),
    runCommand: vi.fn(),
    getTeam: vi.fn(),
    getTraceDetail: vi.fn(),
    subscribeEvents: vi.fn(),
  };
});
const hideQuickWindowMock = vi.fn();
const pollClipboardMock = vi.fn();
const watchSupportedMock = vi.fn(async () => true);
// Shorten the idle auto-run pause; three real seconds per case is not worth it.
vi.mock("./quickRunState", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./quickRunState")>();
  return { ...actual, IDLE_RUN_MS: 200 };
});
vi.mock("../hotkeys/hotkeyRuntime", () => ({
  hideQuickWindow: () => hideQuickWindowMock(),
  clipboardWatchSupported: () => watchSupportedMock(),
  pollClipboard: (since: number) => pollClipboardMock(since),
}));

const t = i18n.getFixedT("en");

function option(overrides: Partial<CommandOption> = {}): CommandOption {
  return {
    command: "greet",
    label: "Greet",
    description: "",
    category: "custom",
    source: "workspace",
    path: "greet.md",
    arguments: [],
    inputs: { defined_args: "auto", extra_args: "hidden", message: "optional" },
    requirements: [],
    routine_eligible: true,
    ...overrides,
  } as CommandOption;
}

/** One trace record, reduced to what the status line reads. */
function traceRecord(label: string, message: string): TraceRecord {
  return {
    presentation: {
      label_key: "",
      label_fallback: label,
      message_key: "",
      message,
      message_params: {},
      tone: "info",
    },
  } as TraceRecord;
}

function traceResponse(records: TraceRecord[]): TraceDetailResponse {
  return { trace_id: "trace-9", summary: null, records, transcript_available: true };
}

function commandStarted(traceId: string, command: string, person: string): RuntimeEvent {
  return {
    kind: "event",
    type: "command.started",
    trace_id: traceId,
    span_id: null,
    parent_id: null,
    source: "app",
    person_id: person,
    command,
    workflow: "",
    attributes: {},
    payload: { command, person },
    timestamp: "2026-07-26T00:00:00Z",
  };
}

let activate: ((trigger: QuickRunTrigger) => void) | undefined;
let publish: ((event: RuntimeEvent) => void) | undefined;

function renderWindow() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider>
      <QueryClientProvider client={queryClient}>
        <QuickRun
          subscribe={(handler) => {
            activate = handler;
            return () => {
              activate = undefined;
            };
          }}
        />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

beforeEach(() => {
  activate = undefined;
  publish = undefined;
  window.localStorage.clear();
  vi.mocked(subscribeEvents)
    .mockReset()
    .mockImplementation((onEvent) => {
      publish = onEvent;
      return () => {
        publish = undefined;
      };
    });
  vi.mocked(getTraceDetail).mockReset().mockResolvedValue(traceResponse([]));
  hideQuickWindowMock.mockReset();
  watchSupportedMock.mockReset().mockResolvedValue(true);
  pollClipboardMock.mockReset();
  setClipboard(1, "initial");
  vi.mocked(runCommand).mockReset().mockResolvedValue({ trace_id: "t1", output: "done" });
  vi.mocked(getTeam)
    .mockReset()
    .mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [
        { person_id: "bot", name: "Bot", is_active: true, roles: [] },
        { person_id: "aiko", name: "Aiko", is_active: true, roles: [] },
        { person_id: "retired", name: "Retired", is_active: false, roles: [] },
      ],
      default_person_id: "bot",
    });
  vi.mocked(getCommandOptions)
    .mockReset()
    .mockResolvedValue({ options: [option(), option({ command: "review", label: "Review" })] });
});

/**
 * Put text on the clipboard, mirroring the host command: the contents come back
 * only when the counter moved since the caller's last poll.
 */
function setClipboard(changeCount: number, text: string) {
  pollClipboardMock.mockImplementation(async (since: number) =>
    since === changeCount
      ? { change_count: changeCount, text: null }
      : { change_count: changeCount, text },
  );
}

/** Simulate a native copy of the given text from inside the window. */
function copySelection(text: string) {
  vi.spyOn(window, "getSelection").mockReturnValue({
    toString: () => text,
  } as unknown as Selection);
  document.dispatchEvent(new Event("copy"));
}

async function fire(trigger: QuickRunTrigger) {
  await waitFor(() => expect(activate).toBeDefined());
  activate!(trigger);
}

describe("QuickRun", () => {
  it("carries the clipboard text into the input and runs it", async () => {
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "selected words" });

    const input = await screen.findByRole("textbox", { name: t("quickRun.message") });
    expect(input).toHaveValue("selected words");

    await user.click(screen.getByRole("button", { name: t("quickRun.run") }));

    await waitFor(() =>
      expect(runCommand).toHaveBeenCalledWith(
        expect.objectContaining({ command: "greet", message: "selected words" }),
      ),
    );
    expect(await screen.findByText("done")).toBeInTheDocument();
  });

  it("offers a command picker only for the generic hotkey", async () => {
    renderWindow();

    await fire({ command: null, text: "" });
    expect(await screen.findByRole("textbox", { name: t("quickRun.command") })).toBeInTheDocument();

    await fire({ command: "review", text: "" });
    await waitFor(() =>
      expect(
        screen.queryByRole("textbox", { name: t("quickRun.command") }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Review")).toBeInTheDocument();
  });

  it("runs a dedicated hotkey without waiting for the user", async () => {
    renderWindow();

    await fire({ command: "review", text: "selected words" });

    await waitFor(() =>
      expect(runCommand).toHaveBeenCalledWith(
        expect.objectContaining({ command: "review", message: "selected words" }),
      ),
    );
  });

  it("waits for input instead of running when a required message is missing", async () => {
    vi.mocked(getCommandOptions).mockResolvedValue({
      options: [
        option({
          command: "review",
          label: "Review",
          inputs: { defined_args: "auto", extra_args: "hidden", message: "required" },
        }),
      ],
    });
    renderWindow();

    await fire({ command: "review", text: "   " });

    expect(await screen.findByRole("textbox", { name: t("quickRun.message") })).toBeInTheDocument();
    expect(runCommand).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: t("quickRun.run") })).toBeDisabled();
  });

  it("waits for input when a required argument has no value", async () => {
    vi.mocked(getCommandOptions).mockResolvedValue({
      options: [
        option({
          command: "review",
          label: "Review",
          arguments: [{ name: "target", kind: "positional", required: true, default: "" }],
        }),
      ],
    });
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: "review", text: "hello" });

    expect(runCommand).not.toHaveBeenCalled();
    await user.type(await screen.findByRole("textbox", { name: "target" }), "main");

    await user.click(screen.getByRole("button", { name: t("quickRun.run") }));
    await waitFor(() =>
      expect(runCommand).toHaveBeenCalledWith(expect.objectContaining({ args: ["main"] })),
    );
  });

  it("preselects the command chosen last time", async () => {
    window.localStorage.setItem("guildbotics.quickRun.lastCommand", "review");
    renderWindow();

    await fire({ command: null, text: "" });

    expect(await screen.findByRole("textbox", { name: t("quickRun.command") })).toHaveValue(
      "Review",
    );
  });

  it("remembers a command picked in the window even when it is not run", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "" });

    await user.click(await screen.findByRole("textbox", { name: t("quickRun.command") }));
    await user.click(await screen.findByRole("option", { name: "Review" }));

    expect(runCommand).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("guildbotics.quickRun.lastCommand")).toBe("review");
  });

  it("keeps the remembered choice when a dedicated hotkey runs another command", async () => {
    window.localStorage.setItem("guildbotics.quickRun.lastCommand", "greet");
    renderWindow();

    await fire({ command: "review", text: "hello" });
    await waitFor(() => expect(runCommand).toHaveBeenCalled());

    expect(window.localStorage.getItem("guildbotics.quickRun.lastCommand")).toBe("greet");
  });

  it("hides the window on escape", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "" });
    await screen.findByRole("textbox", { name: t("quickRun.message") });

    await user.keyboard("{Escape}");

    expect(hideQuickWindowMock).toHaveBeenCalled();
  });

  it("reports a failed run instead of showing an empty result", async () => {
    vi.mocked(runCommand).mockRejectedValue(new Error("backend is down"));
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "hi" });
    await user.click(await screen.findByRole("button", { name: t("quickRun.run") }));

    expect(await screen.findByText(t("quickRun.failed"))).toBeInTheDocument();
  });

  it("offers clipboard watching only alongside the input field", async () => {
    vi.mocked(getCommandOptions).mockResolvedValue({
      options: [
        option({
          command: "silent",
          label: "Silent",
          inputs: { defined_args: "auto", extra_args: "hidden", message: "hidden" },
        }),
      ],
    });
    renderWindow();

    await fire({ command: "silent", text: "" });

    await waitFor(() => expect(runCommand).toHaveBeenCalled());
    expect(
      screen.queryByRole("checkbox", { name: t("quickRun.watchClipboard") }),
    ).not.toBeInTheDocument();
  });

  it("shows clipboard watching unchecked for the generic window", async () => {
    renderWindow();

    await fire({ command: null, text: "" });

    const watch = await screen.findByRole("checkbox", { name: t("quickRun.watchClipboard") });
    expect(watch).toBeEnabled();
    expect(watch).not.toBeChecked();
  });

  it("hides the option where the platform cannot report clipboard changes", async () => {
    watchSupportedMock.mockResolvedValue(false);
    renderWindow();

    await fire({ command: null, text: "" });
    await screen.findByRole("textbox", { name: t("quickRun.message") });

    expect(
      screen.queryByRole("checkbox", { name: t("quickRun.watchClipboard") }),
    ).not.toBeInTheDocument();
  });

  it("leaves the input alone while watching is off", async () => {
    renderWindow();

    await fire({ command: null, text: "first" });
    await screen.findByRole("textbox", { name: t("quickRun.message") });

    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(pollClipboardMock).not.toHaveBeenCalled();
  });

  it("replaces the input when the clipboard changes while watching", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "first" });

    await user.click(await screen.findByRole("checkbox", { name: t("quickRun.watchClipboard") }));

    // The first tick only takes a baseline, so the carried-in text survives it.
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalled());
    expect(screen.getByRole("textbox", { name: t("quickRun.message") })).toHaveValue("first");

    setClipboard(2, "copied later");
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: t("quickRun.message") })).toHaveValue(
        "copied later",
      ),
    );
  });

  it("keeps the watching choice after the window is dismissed", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "" });

    await user.click(await screen.findByRole("checkbox", { name: t("quickRun.watchClipboard") }));

    expect(window.localStorage.getItem("guildbotics.quickRun.watchClipboard")).toBe("true");
  });

  it("restores the watching choice from a previous window", async () => {
    window.localStorage.setItem("guildbotics.quickRun.watchClipboard", "true");
    renderWindow();

    await fire({ command: null, text: "" });

    expect(
      await screen.findByRole("checkbox", { name: t("quickRun.watchClipboard") }),
    ).toBeChecked();
  });

  it("stops watching once the window is dismissed", async () => {
    window.localStorage.setItem("guildbotics.quickRun.watchClipboard", "true");
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "" });
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalled());

    await user.keyboard("{Escape}");
    pollClipboardMock.mockClear();

    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(pollClipboardMock).not.toHaveBeenCalled();
  });

  it("runs again when watching replaces the input while auto-run is on", async () => {
    window.localStorage.setItem("guildbotics.quickRun.watchClipboard", "true");
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "first" });
    await user.click(await screen.findByRole("checkbox", { name: t("quickRun.autoRun") }));
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalled());
    expect(runCommand).not.toHaveBeenCalled();

    setClipboard(2, "copied later");

    await waitFor(() =>
      expect(runCommand).toHaveBeenCalledWith(
        expect.objectContaining({ command: "greet", message: "copied later" }),
      ),
    );
  });

  it("only replaces the input on a fresh copy while auto-run is off", async () => {
    window.localStorage.setItem("guildbotics.quickRun.watchClipboard", "true");
    renderWindow();

    await fire({ command: null, text: "first" });
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalled());

    setClipboard(2, "copied later");

    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: t("quickRun.message") })).toHaveValue(
        "copied later",
      ),
    );
    expect(runCommand).not.toHaveBeenCalled();
  });

  it("turns auto-run on for a dedicated hotkey and off for the generic window", async () => {
    renderWindow();

    await fire({ command: "review", text: "hello" });
    expect(await screen.findByRole("checkbox", { name: t("quickRun.autoRun") })).toBeChecked();

    await fire({ command: null, text: "hello" });
    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: t("quickRun.autoRun") })).not.toBeChecked(),
    );
  });

  it("runs on activation when auto-run is turned on for the generic window", async () => {
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "hello" });
    await user.click(await screen.findByRole("checkbox", { name: t("quickRun.autoRun") }));
    expect(runCommand).not.toHaveBeenCalled();

    // The choice applies to the next activation, not retroactively.
    await fire({ command: "review", text: "hello" });

    await waitFor(() =>
      expect(runCommand).toHaveBeenCalledWith(expect.objectContaining({ command: "review" })),
    );
  });

  it("does not run on a fresh copy that still leaves a required input empty", async () => {
    vi.mocked(getCommandOptions).mockResolvedValue({
      options: [
        option({
          arguments: [{ name: "target", kind: "positional", required: true, default: "" }],
        }),
      ],
    });
    window.localStorage.setItem("guildbotics.quickRun.watchClipboard", "true");
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "first" });
    // Auto-run on, so the run is blocked by the missing argument rather than by
    // the toggle.
    await user.click(await screen.findByRole("checkbox", { name: t("quickRun.autoRun") }));
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalled());

    setClipboard(2, "copied later");

    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: t("quickRun.message") })).toHaveValue(
        "copied later",
      ),
    );
    expect(runCommand).not.toHaveBeenCalled();
  });

  it("explains a hotkey bound to a command that is no longer available", async () => {
    renderWindow();

    await fire({ command: "deleted", text: "hello" });

    expect(await screen.findByText(t("quickRun.unknownCommandTitle"))).toBeInTheDocument();
    expect(runCommand).not.toHaveBeenCalled();
  });

  it("ignores a copy made inside the window", async () => {
    window.localStorage.setItem("guildbotics.quickRun.watchClipboard", "true");
    renderWindow();

    await fire({ command: null, text: "original" });
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalled());

    copySelection("the translated result");
    setClipboard(2, "the translated result");

    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalledWith(2));
    expect(screen.getByRole("textbox", { name: t("quickRun.message") })).toHaveValue("original");
    expect(runCommand).not.toHaveBeenCalled();
  });

  it("still reacts to the next copy after skipping its own", async () => {
    window.localStorage.setItem("guildbotics.quickRun.watchClipboard", "true");
    renderWindow();

    await fire({ command: null, text: "original" });
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalled());

    copySelection("the translated result");
    setClipboard(2, "the translated result");
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalledWith(2));

    setClipboard(3, "copied elsewhere");

    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: t("quickRun.message") })).toHaveValue(
        "copied elsewhere",
      ),
    );
  });

  it("runs an edited input once the field is left", async () => {
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "" });
    const field = await screen.findByRole("textbox", { name: t("quickRun.message") });
    await user.click(await screen.findByRole("checkbox", { name: t("quickRun.autoRun") }));

    await user.click(field);
    await user.type(field, "typed by hand");
    expect(runCommand).not.toHaveBeenCalled();

    await user.tab();

    await waitFor(() =>
      expect(runCommand).toHaveBeenCalledWith(
        expect.objectContaining({ message: "typed by hand" }),
      ),
    );
  });

  it("does not run the same request twice on a second blur", async () => {
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "" });
    const field = await screen.findByRole("textbox", { name: t("quickRun.message") });
    await user.click(await screen.findByRole("checkbox", { name: t("quickRun.autoRun") }));

    await user.click(field);
    await user.type(field, "typed by hand");
    await user.tab();
    await waitFor(() => expect(runCommand).toHaveBeenCalledTimes(1));

    await user.click(field);
    await user.tab();

    expect(runCommand).toHaveBeenCalledTimes(1);
  });

  it("leaves an edited input alone while auto-run is off", async () => {
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "" });
    const field = await screen.findByRole("textbox", { name: t("quickRun.message") });

    await user.click(field);
    await user.type(field, "typed by hand");
    await user.tab();

    expect(runCommand).not.toHaveBeenCalled();
  });

  it("does not run the text left behind when the window is dismissed", async () => {
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: "review", text: "" });
    const field = await screen.findByRole("textbox", { name: t("quickRun.message") });
    await user.click(field);
    await user.type(field, "abandoned");

    await user.keyboard("{Escape}");
    field.blur();

    expect(hideQuickWindowMock).toHaveBeenCalled();
    // The activation itself may have run the empty request; what must not
    // happen is the abandoned edit being sent.
    expect(runCommand).not.toHaveBeenCalledWith(expect.objectContaining({ message: "abandoned" }));
  });

  describe("idle auto-run", () => {
    // Typing carries no "I am done" signal, so the run hangs off a pause. The
    // module mock above shortens that pause so the wait stays real (fake timers
    // stall the promises react-query and user-event await).
    function pause(ms: number) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    async function openWith(command: string | null) {
      // A required message keeps the activation itself from running, so every
      // run in these cases comes from the typing pause.
      vi.mocked(getCommandOptions).mockResolvedValue({
        options: [
          option({
            command: "review",
            label: "Review",
            inputs: { defined_args: "auto", extra_args: "hidden", message: "required" },
          }),
        ],
      });
      renderWindow();
      await fire({ command, text: "" });
      return screen.findByRole("textbox", { name: t("quickRun.message") });
    }

    it("runs once typing has been quiet long enough", async () => {
      const user = userEvent.setup();
      const field = await openWith("review");

      await user.type(field, "typed by hand");

      await waitFor(() =>
        expect(runCommand).toHaveBeenCalledWith(
          expect.objectContaining({ message: "typed by hand" }),
        ),
      );
    });

    it("keeps waiting while the typing continues", async () => {
      const user = userEvent.setup({ delay: null });
      const field = await openWith("review");

      await user.type(field, "first");
      await pause(IDLE_RUN_MS / 2);
      expect(runCommand).not.toHaveBeenCalled();

      // Typing again pushes the pause back rather than adding a second run.
      await user.type(field, " second");
      await pause(IDLE_RUN_MS / 2);
      expect(runCommand).not.toHaveBeenCalled();

      await waitFor(() =>
        expect(runCommand).toHaveBeenCalledWith(
          expect.objectContaining({ message: "first second" }),
        ),
      );
      expect(runCommand).toHaveBeenCalledTimes(1);
    });

    it("leaves settled typing alone while auto-run is off", async () => {
      const user = userEvent.setup();
      const field = await openWith(null);

      await user.type(field, "typed by hand");
      await pause(IDLE_RUN_MS * 3);

      expect(runCommand).not.toHaveBeenCalled();
    });

    it("drops the pending run when the window is dismissed", async () => {
      const user = userEvent.setup();
      const field = await openWith("review");

      await user.type(field, "abandoned");
      await user.keyboard("{Escape}");
      await pause(IDLE_RUN_MS * 3);

      expect(runCommand).not.toHaveBeenCalledWith(
        expect.objectContaining({ message: "abandoned" }),
      );
    });
  });

  it("shows who the run goes to", async () => {
    renderWindow();

    await fire({ command: null, text: "" });

    expect(await screen.findByAltText("Bot")).toBeInTheDocument();
  });

  it("switches the member the run is attributed to", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "hello" });

    await user.click(await screen.findByLabelText(t("quickRun.runner", { member: "Bot" })));
    await user.click(await screen.findByRole("menuitem", { name: "Aiko" }));

    expect(await screen.findByAltText("Aiko")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("quickRun.run") }));

    await waitFor(() =>
      expect(runCommand).toHaveBeenCalledWith(expect.objectContaining({ person: "aiko" })),
    );
  });

  it("offers only active members", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "" });

    await user.click(await screen.findByLabelText(t("quickRun.runner", { member: "Bot" })));

    expect(await screen.findByRole("menuitem", { name: "Aiko" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Retired" })).not.toBeInTheDocument();
  });

  it("remembers the member across windows", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "" });

    await user.click(await screen.findByLabelText(t("quickRun.runner", { member: "Bot" })));
    await user.click(await screen.findByRole("menuitem", { name: "Aiko" }));

    expect(window.localStorage.getItem("guildbotics.quickRun.lastPerson")).toBe("aiko");
  });

  it("restores the remembered member", async () => {
    window.localStorage.setItem("guildbotics.quickRun.lastPerson", "aiko");
    renderWindow();

    await fire({ command: null, text: "" });

    expect(await screen.findByAltText("Aiko")).toBeInTheDocument();
  });

  it("falls back to the default when the remembered member is gone", async () => {
    window.localStorage.setItem("guildbotics.quickRun.lastPerson", "deleted");
    renderWindow();

    await fire({ command: null, text: "" });

    expect(await screen.findByAltText("Bot")).toBeInTheDocument();
  });

  it("copies the output without feeding it back into the input", async () => {
    window.localStorage.setItem("guildbotics.quickRun.watchClipboard", "true");
    const user = userEvent.setup();
    // Installed after setup(), which stubs navigator.clipboard itself.
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    renderWindow();

    await fire({ command: null, text: "original" });
    await user.click(await screen.findByRole("button", { name: t("quickRun.run") }));
    await screen.findByText("done");

    await user.click(screen.getByRole("button", { name: t("quickRun.copy") }));
    expect(writeText).toHaveBeenCalledWith("done");

    // The watcher sees the change but must recognise it as our own.
    setClipboard(2, "done");
    await waitFor(() => expect(pollClipboardMock).toHaveBeenCalledWith(2));
    expect(screen.getByRole("textbox", { name: t("quickRun.message") })).toHaveValue("original");
  });

  it("closes from the window's own close button", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "" });

    await user.click(await screen.findByRole("button", { name: t("quickRun.close") }));

    expect(hideQuickWindowMock).toHaveBeenCalled();
  });

  it("does not run the text left behind when closed from the button", async () => {
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: "review", text: "" });
    const field = await screen.findByRole("textbox", { name: t("quickRun.message") });
    await user.click(field);
    await user.type(field, "abandoned");

    await user.click(screen.getByRole("button", { name: t("quickRun.close") }));
    field.blur();

    expect(runCommand).not.toHaveBeenCalledWith(expect.objectContaining({ message: "abandoned" }));
  });

  it("labels the copy control instead of showing a translation key", async () => {
    const user = userEvent.setup();
    renderWindow();

    await fire({ command: null, text: "hi" });
    await user.click(await screen.findByRole("button", { name: t("quickRun.run") }));
    await screen.findByText("done");

    const copy = screen.getByRole("button", { name: t("quickRun.copy") });
    expect(copy).toBeInTheDocument();
    expect(t("quickRun.copy")).not.toBe("quickRun.copy");
  });

  it("fetches the catalogue for the member the run is attributed to", async () => {
    const user = userEvent.setup();
    renderWindow();
    await fire({ command: null, text: "" });
    await waitFor(() => expect(getCommandOptions).toHaveBeenCalledWith("bot"));

    await user.click(await screen.findByLabelText(t("quickRun.runner", { member: "Bot" })));
    await user.click(await screen.findByRole("menuitem", { name: "Aiko" }));

    await waitFor(() => expect(getCommandOptions).toHaveBeenCalledWith("aiko"));
  });

  it("refuses to run a command whose integrations are not configured", async () => {
    vi.mocked(getCommandOptions).mockResolvedValue({
      options: [
        option({
          command: "review",
          label: "Review",
          requirements: [{ kind: "github", satisfied: false, message: "" }],
        }),
      ],
    });
    renderWindow();

    await fire({ command: "review", text: "hello" });

    expect(
      await screen.findByText(
        t("quickRun.requirementsMissing", { requirements: t("commands.requirements.github") }),
      ),
    ).toBeInTheDocument();
    expect(runCommand).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: t("quickRun.run") })).toBeDisabled();
  });
  it("follows the run with the newest trace record in the status line", async () => {
    let finish: ((response: CommandRunResponse) => void) | undefined;
    vi.mocked(runCommand).mockReturnValue(
      new Promise<CommandRunResponse>((resolve) => {
        finish = resolve;
      }),
    );
    vi.mocked(getTraceDetail).mockResolvedValue(
      traceResponse([traceRecord("Prompt", "asking the model")]),
    );
    renderWindow();

    await fire({ command: "review", text: "hello" });
    await waitFor(() => expect(runCommand).toHaveBeenCalled());

    // The trace is named by the service, not by the still-pending request.
    await waitFor(() => expect(publish).toBeDefined());
    publish!(commandStarted("trace-9", "review", "bot"));

    await waitFor(() => expect(getTraceDetail).toHaveBeenCalledWith("trace-9"));
    const status = await screen.findByRole("status", { name: t("quickRun.status") });
    await waitFor(() => expect(status).toHaveTextContent("asking the model"));
    expect(status).toHaveTextContent("Prompt");

    // The closing record is written as the run answers, so it is read once more.
    vi.mocked(getTraceDetail).mockResolvedValue(
      traceResponse([
        traceRecord("Prompt", "asking the model"),
        traceRecord("Done", "wrote 3 files"),
      ]),
    );
    finish!({ trace_id: "trace-9", output: "done" });

    await waitFor(() => expect(status).toHaveTextContent("wrote 3 files"));
  });

  it("keeps the status line out of runs the window did not start", async () => {
    renderWindow();
    await fire({ command: null, text: "" });
    await waitFor(() => expect(publish).toBeDefined());

    publish!(commandStarted("scheduler-1", "digest", "bot"));

    await waitFor(() => expect(screen.getByRole("status")).toBeEmptyDOMElement());
    expect(getTraceDetail).not.toHaveBeenCalled();
  });
});
