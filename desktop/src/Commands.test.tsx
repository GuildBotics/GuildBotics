import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiRequestError,
  authorCommand,
  createCommandFile,
  deleteCommandFile,
  getCommandFile,
  getCommandFileExecutionStatus,
  getConfigStatus,
  getTeam,
  getTraceDetail,
  listCommandFiles,
  runCommand,
  subscribeEvents,
  updateCommandFile,
  type CommandFileDetail,
  type CommandFileExecutionStatus,
  type CommandFileSummary,
  type ConfigStatus,
  type RuntimeEvent,
  type TeamSummary,
} from "./api/client";
import i18n from "./i18n";
import "./i18n";
import { makeRuntimeEvent } from "./test/factories";

const t = i18n.getFixedT("en");

// Replace the CodeMirror editor with a plain textarea + save button so the
// page's dirty/save wiring is testable without driving CodeMirror in jsdom.
vi.mock("./commands/CommandEditor", () => ({
  CommandEditor: ({
    value,
    onChange,
    onSave,
    path,
  }: {
    value: string;
    onChange: (value: string) => void;
    onSave: () => void;
    path: string;
  }) => (
    <div>
      <div data-testid="editor-path">{path}</div>
      <textarea aria-label="editor" value={value} onChange={(e) => onChange(e.target.value)} />
      <button type="button" onClick={onSave}>
        mock-save-shortcut
      </button>
    </div>
  ),
}));

vi.mock("@tauri-apps/plugin-shell", () => ({ open: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(), save: vi.fn() }));
vi.mock("./setup/SetupPage", () => ({ SetupPage: () => <div>Setup Mock</div> }));

let eventListener: ((event: RuntimeEvent) => void) | null = null;

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    authorCommand: vi.fn(),
    getConfigStatus: vi.fn(),
    getTeam: vi.fn(),
    getTraceDetail: vi.fn(),
    listCommandFiles: vi.fn(),
    getCommandFile: vi.fn(),
    createCommandFile: vi.fn(),
    deleteCommandFile: vi.fn(),
    updateCommandFile: vi.fn(),
    getCommandFileExecutionStatus: vi.fn(),
    runCommand: vi.fn(),
    subscribeEvents: vi.fn(),
  };
});

const getConfigStatusMock = vi.mocked(getConfigStatus);
const authorCommandMock = vi.mocked(authorCommand);
const getTeamMock = vi.mocked(getTeam);
const getTraceDetailMock = vi.mocked(getTraceDetail);
const listCommandFilesMock = vi.mocked(listCommandFiles);
const getCommandFileMock = vi.mocked(getCommandFile);
const createCommandFileMock = vi.mocked(createCommandFile);
const deleteCommandFileMock = vi.mocked(deleteCommandFile);
const updateCommandFileMock = vi.mocked(updateCommandFile);
const executionStatusMock = vi.mocked(getCommandFileExecutionStatus);
const runCommandMock = vi.mocked(runCommand);
const subscribeEventsMock = vi.mocked(subscribeEvents);

function configStatus(overrides: Partial<ConfigStatus> = {}): ConfigStatus {
  return {
    cwd: "/workspace",
    env_file: "/workspace/.env",
    env_file_exists: true,
    config_dir: "/workspace/.guildbotics/config",
    project_file: "/workspace/.guildbotics/config/team/project.yml",
    project_file_exists: true,
    storage_dir: "/workspace/.guildbotics",
    ...overrides,
  };
}

function team(overrides: Partial<TeamSummary> = {}): TeamSummary {
  return {
    project: { name: "Demo", language_code: "en", language_name: "English" },
    members: [{ person_id: "bot", name: "Bot", is_active: true, roles: [] }],
    default_person_id: "bot",
    ...overrides,
  };
}

function summary(overrides: Partial<CommandFileSummary> = {}): CommandFileSummary {
  return {
    id: "greet-id",
    command: "greet",
    label: "Greet",
    description: "",
    relative_path: "greet.md",
    format: "markdown",
    ...overrides,
  };
}

function detail(overrides: Partial<CommandFileDetail> = {}): CommandFileDetail {
  return {
    ...summary(),
    content: "---\nname: Greet\n---\nHi\n",
    revision: "rev-1",
    arguments: [],
    inputs: { defined_args: "auto", extra_args: "hidden", message: "optional" },
    ...overrides,
  };
}

function status(overrides: Partial<CommandFileExecutionStatus> = {}): CommandFileExecutionStatus {
  return {
    matches_selected_file: true,
    requirements: [],
    blocking_code: null,
    blocking_context: {},
    ...overrides,
  };
}

beforeEach(() => {
  eventListener = null;
  window.localStorage.clear();
  getConfigStatusMock.mockReset().mockResolvedValue(configStatus());
  authorCommandMock.mockReset().mockResolvedValue({
    trace_id: "author-trace-1",
    message: "Updated the draft.",
    command: "greet",
    format: "markdown",
    relative_path: "greet.md",
    content: "---\nname: Friendly greet\n---\nHello there!\n",
  });
  getTeamMock.mockReset().mockResolvedValue(team());
  getTraceDetailMock.mockReset().mockResolvedValue({ trace_id: "", summary: null, records: [] });
  listCommandFilesMock.mockReset().mockResolvedValue({ files: [summary()] });
  getCommandFileMock.mockReset().mockResolvedValue(detail());
  createCommandFileMock
    .mockReset()
    .mockResolvedValue(detail({ id: "new-id", command: "reports/weekly" }));
  deleteCommandFileMock.mockReset().mockResolvedValue({ files: [] });
  updateCommandFileMock
    .mockReset()
    .mockImplementation((_id, body) =>
      Promise.resolve(detail({ revision: "rev-2", content: body.content })),
    );
  executionStatusMock.mockReset().mockResolvedValue(status());
  runCommandMock.mockReset().mockResolvedValue({ trace_id: "run-1", output: "done" });
  subscribeEventsMock.mockReset().mockImplementation((listener) => {
    eventListener = listener;
    return () => {
      eventListener = null;
    };
  });
});

async function renderPage() {
  const { CommandsPage } = await import("./commands/CommandsPage");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/commands"]}>
          <CommandsPage />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

describe("Command editor screen", () => {
  it("shows the empty state when there are no commands", async () => {
    listCommandFilesMock.mockResolvedValue({ files: [] });
    await renderPage();

    expect(await screen.findByText(t("commands.emptyTitle"))).toBeInTheDocument();
  });

  it("loads the selected command source and path into the editor", async () => {
    await renderPage();

    const editor = await screen.findByLabelText<HTMLTextAreaElement>("editor");
    expect(editor).toHaveValue("---\nname: Greet\n---\nHi\n");
    expect(screen.getByTestId("editor-path")).toHaveTextContent(
      "/workspace/.guildbotics/config/commands/greet.md",
    );
  });

  it("hydrates the persisted per-workspace state after the config resolves", async () => {
    // storageDir arrives asynchronously; a value stored under that workspace key
    // must still be restored even though the first render had no storageDir.
    listCommandFilesMock.mockResolvedValue({
      files: [summary(), summary({ id: "other-id", command: "other", label: "Other" })],
    });
    getCommandFileMock.mockImplementation((id) =>
      Promise.resolve(id === "other-id" ? detail({ id: "other-id", command: "other" }) : detail()),
    );
    window.localStorage.setItem(
      "guildbotics.commands.editorState:/workspace/.guildbotics",
      JSON.stringify({ selectedFileId: "other-id" }),
    );
    await renderPage();

    // The restored selection wins over the default first file.
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: t("commands.editSelectLabel") })).toHaveValue(
        "Other (other)",
      ),
    );
  });

  it("marks the buffer dirty on edit and enables save", async () => {
    const user = userEvent.setup();
    await renderPage();
    const editor = await screen.findByLabelText<HTMLTextAreaElement>("editor");

    expect(screen.getByText(t("commands.saveState.clean"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("commands.save") })).toBeDisabled();

    await user.type(editor, "!");

    expect(await screen.findByText(t("commands.saveState.dirty"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("commands.save") })).toBeEnabled();
  });

  it("applies an AI authoring response to the unsaved editor draft", async () => {
    const user = userEvent.setup();
    await renderPage();
    const editor = await screen.findByLabelText<HTMLTextAreaElement>("editor");

    await user.type(
      screen.getByRole("textbox", { name: t("commands.authoring.inputLabel") }),
      "Make it friendlier",
    );
    await user.click(screen.getByRole("button", { name: t("commands.authoring.send") }));

    await waitFor(() =>
      expect(authorCommandMock).toHaveBeenCalledWith({
        mode: "edit",
        conversation_id: expect.any(String),
        command: "greet",
        format: "markdown",
        content: "---\nname: Greet\n---\nHi\n",
        message: "Make it friendlier",
        person: "bot",
      }),
    );
    expect(editor).toHaveValue("---\nname: Friendly greet\n---\nHello there!\n");
    expect(screen.getByText("Updated the draft.")).toBeInTheDocument();
    expect(screen.getByText(t("commands.saveState.dirty"))).toBeInTheDocument();
  });

  it("shows AI authoring errors in the assistant panel", async () => {
    authorCommandMock.mockRejectedValue(new Error("Agent unavailable"));
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.type(
      screen.getByRole("textbox", { name: t("commands.authoring.inputLabel") }),
      "Update it",
    );
    await user.click(screen.getByRole("button", { name: t("commands.authoring.send") }));

    expect(await screen.findByText(t("commands.authoring.errorTitle"))).toBeInTheDocument();
    expect(screen.getByText("Agent unavailable")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: t("commands.authoring.inputLabel") })).toHaveValue(
      "Update it",
    );
  });

  it("leaves an AI authoring failure behind when another command is opened", async () => {
    listCommandFilesMock.mockResolvedValue({
      files: [summary(), summary({ id: "other-id", command: "other", label: "Other" })],
    });
    getCommandFileMock.mockImplementation((id) =>
      Promise.resolve(id === "other-id" ? detail({ id: "other-id", command: "other" }) : detail()),
    );
    authorCommandMock.mockRejectedValue(new Error("Agent unavailable"));
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.type(
      screen.getByRole("textbox", { name: t("commands.authoring.inputLabel") }),
      "Update it",
    );
    await user.click(screen.getByRole("button", { name: t("commands.authoring.send") }));
    expect(await screen.findByText("Agent unavailable")).toBeInTheDocument();

    await user.click(screen.getByRole("textbox", { name: t("commands.editSelectLabel") }));
    await user.click(await screen.findByText("Other (other)"));

    // The failure belonged to the previous command's conversation.
    await waitFor(() => expect(screen.queryByText("Agent unavailable")).not.toBeInTheDocument());
    expect(screen.queryByText(t("commands.authoring.errorTitle"))).not.toBeInTheDocument();
  });

  it("saves via the editor shortcut and returns to a clean state", async () => {
    const user = userEvent.setup();
    await renderPage();
    const editor = await screen.findByLabelText<HTMLTextAreaElement>("editor");
    await user.type(editor, "X");

    await user.click(screen.getByRole("button", { name: "mock-save-shortcut" }));

    await waitFor(() =>
      expect(updateCommandFileMock).toHaveBeenCalledWith("greet-id", {
        content: "---\nname: Greet\n---\nHi\nX",
        expected_revision: "rev-1",
      }),
    );
    expect(await screen.findByText(t("commands.saveState.clean"))).toBeInTheDocument();
  });

  it("saves invalid source but blocks execution until it is corrected", async () => {
    executionStatusMock.mockImplementation((_fileId, params) =>
      Promise.resolve(
        params.expected_revision === "rev-2"
          ? status({
              matches_selected_file: false,
              blocking_code: "command_file_invalid_source",
              blocking_context: { message: "Command 'args' must be a mapping." },
            })
          : status(),
      ),
    );
    const user = userEvent.setup();
    await renderPage();
    const editor = await screen.findByLabelText<HTMLTextAreaElement>("editor");
    await user.type(editor, "X");
    await user.click(screen.getByRole("button", { name: t("commands.save") }));

    expect(await screen.findByText(t("commands.saveState.clean"))).toBeInTheDocument();
    expect(screen.queryByText(t("commands.saveErrorTitle"))).not.toBeInTheDocument();
    expect(await screen.findByText(t("commands.runBlockedTitle"))).toBeInTheDocument();
    expect(screen.getByText(t("commands.errors.command_file_invalid_source"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("commands.saveAndRun") })).toBeDisabled();
    expect(editor).toHaveValue("---\nname: Greet\n---\nHi\nX");
  });

  it("shows a conflict alert on a stale revision", async () => {
    updateCommandFileMock.mockRejectedValue(
      new ApiRequestError({
        code: "command_file_changed",
        message: "changed",
        context: {},
      }),
    );
    const user = userEvent.setup();
    await renderPage();
    const editor = await screen.findByLabelText<HTMLTextAreaElement>("editor");
    await user.type(editor, "X");
    await user.click(screen.getByRole("button", { name: t("commands.save") }));

    expect(await screen.findByText(t("commands.conflictTitle"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("commands.conflictReload") })).toBeInTheDocument();
  });

  it("guards a command switch when there are unsaved changes", async () => {
    listCommandFilesMock.mockResolvedValue({
      files: [summary(), summary({ id: "other-id", command: "other", label: "Other" })],
    });
    const user = userEvent.setup();
    await renderPage();
    const editor = await screen.findByLabelText<HTMLTextAreaElement>("editor");
    await user.type(editor, "X");

    await user.click(screen.getByRole("textbox", { name: t("commands.editSelectLabel") }));
    await user.click(await screen.findByText("Other (other)"));

    expect(
      await screen.findByRole("heading", { name: t("commands.unsavedTitle"), level: 2 }),
    ).toBeInTheDocument();
  });

  it("creates a new command through the dialog and selects it in the editor", async () => {
    const created = detail({
      id: "new-id",
      command: "reports/weekly",
      label: "Weekly",
      content: "NEW COMMAND SOURCE",
    });
    createCommandFileMock.mockResolvedValue(created);
    getCommandFileMock.mockImplementation((id) =>
      Promise.resolve(id === "new-id" ? created : detail()),
    );
    // After creation the backend list includes the new file; the selection must
    // survive the refetch rather than snapping back to the first file.
    listCommandFilesMock.mockResolvedValue({
      files: [summary(), summary({ id: "new-id", command: "reports/weekly", label: "Weekly" })],
    });
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.click(screen.getByRole("button", { name: t("commands.newFile") }));
    await user.click(await screen.findByText(t("commands.createMethodManual")));
    await user.type(
      await screen.findByRole("textbox", { name: t("commands.createNameLabel") }),
      "reports/weekly",
    );
    await user.click(screen.getByRole("button", { name: t("commands.createSubmit") }));

    await waitFor(() =>
      expect(createCommandFileMock).toHaveBeenCalledWith({
        command: "reports/weekly",
        format: "markdown",
      }),
    );
    // The created file is now the loaded, selected file.
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLTextAreaElement>("editor")).toHaveValue(
        "NEW COMMAND SOURCE",
      ),
    );
  });

  it("lets AI choose the name and format and creates the file only on save", async () => {
    const source = "def main(context) -> str:\n    return 'weekly'\n";
    const revisedSource = "commands: []\n";
    const created = detail({
      id: "ai-created-id",
      command: "reports/weekly-workflow",
      format: "yaml",
      relative_path: "reports/weekly-workflow.yaml",
      content: revisedSource,
      revision: "ai-rev-1",
    });
    authorCommandMock.mockResolvedValue({
      trace_id: "author-create-trace",
      message: "Python fits the required data processing.",
      command: "reports/weekly",
      format: "python",
      relative_path: "reports/weekly.py",
      content: source,
    });
    createCommandFileMock.mockResolvedValue(created);
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.click(screen.getByRole("button", { name: t("commands.newFile") }));
    await user.type(
      await screen.findByRole("textbox", { name: t("commands.createAiRequestLabel") }),
      "Create a weekly report command",
    );
    await user.click(screen.getByRole("button", { name: t("commands.createAiSubmit") }));

    await waitFor(() =>
      expect(authorCommandMock).toHaveBeenCalledWith({
        mode: "create",
        conversation_id: expect.any(String),
        message: "Create a weekly report command",
        person: "bot",
      }),
    );
    expect(createCommandFileMock).not.toHaveBeenCalled();
    expect(await screen.findByLabelText<HTMLTextAreaElement>("editor")).toHaveValue(source);
    expect(screen.getByTestId("editor-path")).toHaveTextContent(
      "/workspace/.guildbotics/config/commands/reports/weekly.py",
    );
    const authoringId = authorCommandMock.mock.calls[0][0].conversation_id;

    authorCommandMock.mockResolvedValueOnce({
      trace_id: "author-revise-trace",
      message: "Changed it to a declarative workflow.",
      command: "reports/weekly-workflow",
      format: "yaml",
      relative_path: "reports/weekly-workflow.yaml",
      content: revisedSource,
    });
    await user.type(
      screen.getByRole("textbox", { name: t("commands.authoring.inputLabel") }),
      "Use a declarative workflow instead",
    );
    await user.click(screen.getByRole("button", { name: t("commands.authoring.send") }));

    await waitFor(() =>
      expect(authorCommandMock).toHaveBeenLastCalledWith({
        mode: "create",
        conversation_id: authoringId,
        command: "reports/weekly",
        format: "python",
        content: source,
        message: "Use a declarative workflow instead",
        person: "bot",
      }),
    );
    expect(screen.getByLabelText<HTMLTextAreaElement>("editor")).toHaveValue(revisedSource);
    expect(screen.getByTestId("editor-path")).toHaveTextContent(
      "/workspace/.guildbotics/config/commands/reports/weekly-workflow.yaml",
    );
    expect(screen.getByText("Changed it to a declarative workflow.")).toBeInTheDocument();

    listCommandFilesMock.mockResolvedValue({
      files: [
        summary(),
        summary({
          id: created.id,
          command: created.command,
          label: created.label,
          relative_path: created.relative_path,
          format: created.format,
        }),
      ],
    });
    getCommandFileMock.mockImplementation((id) =>
      Promise.resolve(id === created.id ? created : detail()),
    );
    await user.click(screen.getByRole("button", { name: t("commands.save") }));

    await waitFor(() =>
      expect(createCommandFileMock).toHaveBeenCalledWith({
        command: "reports/weekly-workflow",
        format: "yaml",
        content: revisedSource,
      }),
    );

    authorCommandMock.mockResolvedValueOnce({
      trace_id: "author-after-save-trace",
      message: "Added another workflow step.",
      command: "reports/weekly-workflow",
      format: "yaml",
      relative_path: "reports/weekly-workflow.yaml",
      content: "commands:\n  - print: done\n",
    });
    expect(screen.getByText("Changed it to a declarative workflow.")).toBeInTheDocument();
    await user.type(
      screen.getByRole("textbox", { name: t("commands.authoring.inputLabel") }),
      "Add a final status",
    );
    await user.click(screen.getByRole("button", { name: t("commands.authoring.send") }));

    await waitFor(() =>
      expect(authorCommandMock).toHaveBeenLastCalledWith({
        mode: "edit",
        conversation_id: authoringId,
        command: "reports/weekly-workflow",
        format: "yaml",
        content: revisedSource,
        message: "Add a final status",
        person: "bot",
      }),
    );
  });

  it("explains how to resolve an AI-proposed name collision", async () => {
    authorCommandMock.mockResolvedValue({
      trace_id: "author-create-trace",
      message: "Draft created.",
      command: "greet",
      format: "markdown",
      relative_path: "greet.md",
      content: "---\nbrain: none\n---\nHello.\n",
    });
    createCommandFileMock.mockRejectedValue(
      new ApiRequestError({
        code: "command_file_exists",
        message: "Command 'greet' already exists.",
        context: {},
      }),
    );
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.click(screen.getByRole("button", { name: t("commands.newFile") }));
    await user.type(
      await screen.findByRole("textbox", { name: t("commands.createAiRequestLabel") }),
      "Create a greeting command",
    );
    await user.click(screen.getByRole("button", { name: t("commands.createAiSubmit") }));
    await screen.findByText("Draft created.");
    await user.click(screen.getByRole("button", { name: t("commands.save") }));

    expect(
      await screen.findByText(t("commands.aiNameCollisionHelp"), { exact: false }),
    ).toBeInTheDocument();
  });

  it("blocks the run with a member-shadow message and disables save-and-run", async () => {
    executionStatusMock.mockResolvedValue(
      status({
        matches_selected_file: false,
        blocking_code: "command_file_shadowed",
        blocking_context: { shadow_source: "member" },
      }),
    );
    await renderPage();
    await screen.findByLabelText("editor");

    expect(await screen.findByText(t("commands.shadow.member"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("commands.saveAndRun") })).toBeDisabled();
  });

  it("saves before running and builds the payload from the saved response", async () => {
    // The pre-save file accepts a message; the saved response changes inputs to
    // hidden. The run payload must follow the saved definition, not the stale
    // one, so the typed message is dropped.
    updateCommandFileMock.mockImplementation((_id, body) =>
      Promise.resolve(
        detail({
          revision: "rev-2",
          content: body.content,
          inputs: { defined_args: "auto", extra_args: "hidden", message: "hidden" },
        }),
      ),
    );
    const user = userEvent.setup();
    await renderPage();
    const editor = await screen.findByLabelText<HTMLTextAreaElement>("editor");
    await user.type(editor, "X");
    await user.type(screen.getByRole("textbox", { name: t("commands.message") }), "hello");

    await user.click(screen.getByRole("button", { name: t("commands.saveAndRun") }));

    await waitFor(() => expect(updateCommandFileMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(runCommandMock).toHaveBeenCalledWith(
        expect.objectContaining({
          command: "greet",
          message: "",
          expected_command_file_id: "greet-id",
          expected_command_file_revision: "rev-2",
        }),
      ),
    );
  });

  it("runs without a person when no member is picked and shows the default in the field", async () => {
    getTeamMock.mockResolvedValue(
      team({
        members: [
          { person_id: "bot", name: "Bot", is_active: true, roles: [] },
          { person_id: "aiko", name: "Aiko", is_active: true, roles: [] },
        ],
        default_person_id: "aiko",
      }),
    );
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.click(screen.getByText(t("commands.advanced")));
    expect(
      await screen.findByPlaceholderText(
        t("commands.memberDefaultPlaceholder", { member: "Aiko (aiko)" }),
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("commands.saveAndRun") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalled());
    expect(runCommandMock.mock.calls[0][0].person).toBeUndefined();
  });

  it("sends the picked member and keeps it out of the request once cleared", async () => {
    getTeamMock.mockResolvedValue(
      team({
        members: [
          { person_id: "bot", name: "Bot", is_active: true, roles: [] },
          { person_id: "aiko", name: "Aiko", is_active: true, roles: [] },
        ],
        default_person_id: "aiko",
      }),
    );
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.click(screen.getByText(t("commands.advanced")));
    await user.click(
      await screen.findByPlaceholderText(
        t("commands.memberDefaultPlaceholder", { member: "Aiko (aiko)" }),
      ),
    );
    await user.click(await screen.findByRole("option", { name: "Bot (bot)" }));
    await user.click(screen.getByRole("button", { name: t("commands.saveAndRun") }));

    await waitFor(() => expect(runCommandMock).toHaveBeenCalled());
    expect(runCommandMock.mock.calls[0][0].person).toBe("bot");
  });

  it("disables save-and-run when no member can execute commands", async () => {
    // A human member cannot run commands, so the backend reports no default.
    getTeamMock.mockResolvedValue(
      team({
        members: [
          { person_id: "hana", name: "Hana", person_type: "human", is_active: true, roles: [] },
        ],
        default_person_id: "",
      }),
    );
    await renderPage();
    await screen.findByLabelText("editor");

    expect(screen.getByRole("button", { name: t("commands.saveAndRun") })).toBeDisabled();
  });

  it("keeps the member and cwd inputs inside the collapsed advanced section", async () => {
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    expect(screen.queryByRole("textbox", { name: t("commands.member") })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: t("commands.cwd") })).not.toBeInTheDocument();
    await user.click(screen.getByText(t("commands.advanced")));
    expect(screen.getByRole("textbox", { name: t("commands.member") })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: t("commands.cwd") })).toBeInTheDocument();
  });

  it("updates run history from command lifecycle events", async () => {
    await renderPage();
    await screen.findByLabelText("editor");

    eventListener?.(
      makeRuntimeEvent({
        type: "command.started",
        trace_id: "evt-1",
        payload: { command: "greet", person: "bot" },
      }),
    );

    const running = await screen.findAllByText(t("commands.status.running"));
    expect(running.length).toBeGreaterThan(0);
  });

  it("shows the run timeline without the diagnostics record filter", async () => {
    await renderPage();
    await screen.findByLabelText("editor");

    eventListener?.(
      makeRuntimeEvent({
        type: "command.started",
        trace_id: "evt-1",
        payload: { command: "greet", person: "bot" },
      }),
    );

    await screen.findAllByText(t("commands.status.running"));
    // A single run is already scoped to one command, so the kind filter that
    // the diagnostics timeline offers is not rendered here.
    expect(
      screen.queryByText(t("diagnostics.executions.recordFilters.ai")),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(t("diagnostics.executions.recordFilters.memory")),
    ).not.toBeInTheDocument();
  });

  it("deletes the selected command after confirmation and selects what remains", async () => {
    listCommandFilesMock.mockResolvedValue({
      files: [summary(), summary({ id: "other-id", command: "other", label: "Other" })],
    });
    deleteCommandFileMock.mockResolvedValue({
      files: [summary({ id: "other-id", command: "other", label: "Other" })],
    });
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.click(screen.getByRole("button", { name: t("commands.deleteFile") }));
    const dialog = await screen.findByRole("dialog");
    // The confirm dialog names the command that is about to be removed.
    expect(within(dialog).getByText(/greet/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: t("commands.deleteFile") }));

    // The loaded revision travels with the request so a file changed elsewhere
    // is refused by the backend instead of being discarded.
    await waitFor(() => expect(deleteCommandFileMock).toHaveBeenCalledWith("greet-id", "rev-1"));
    await waitFor(() => expect(getCommandFileMock).toHaveBeenCalledWith("other-id"));
  });

  it("explains a delete refused because the file changed elsewhere", async () => {
    deleteCommandFileMock.mockRejectedValue(
      new ApiRequestError({
        code: "command_file_changed",
        message: "The command file changed since it was loaded.",
        context: {},
      }),
    );
    const user = userEvent.setup();
    await renderPage();
    await screen.findByLabelText("editor");

    await user.click(screen.getByRole("button", { name: t("commands.deleteFile") }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: t("commands.deleteFile") }));

    expect(await within(dialog).findByText(t("commands.deleteConflictBody"))).toBeInTheDocument();
    // The command stays selected so the latest version can be reviewed.
    expect(screen.getByLabelText("editor")).toBeInTheDocument();
  });

  it("keeps the delete button disabled while no command is selected", async () => {
    listCommandFilesMock.mockResolvedValue({ files: [] });
    await renderPage();

    await screen.findByText(t("commands.emptyTitle"));
    expect(screen.getByRole("button", { name: t("commands.deleteFile") })).toBeDisabled();
  });

  it("lets the input text box be resized vertically", async () => {
    await renderPage();
    await screen.findByLabelText("editor");

    const messageBox = screen.getByRole("textbox", { name: t("commands.message") });
    expect(messageBox.style.getPropertyValue("--input-resize")).toBe("vertical");
  });

  it("defines both English and Japanese labels for the editor", () => {
    expect(i18n.getFixedT("en")("commands.title")).toBe("Edit Command");
    expect(i18n.getFixedT("ja")("commands.title")).toBe("コマンド編集");
    expect(i18n.getFixedT("ja")("commands.shadow.member")).not.toBe("commands.shadow.member");
  });
});
