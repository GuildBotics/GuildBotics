import { MantineProvider, createTheme } from "@mantine/core";
import { Notifications, notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  addMemberConfig,
  ApiRequestError,
  deleteMemberConfig,
  getCliAgentDetections,
  ensureAgentField,
  getAgentFieldState,
  getCommandOptions,
  getConfigStatus,
  getIntelligenceConfig,
  getMemberConfig,
  getProjectConfig,
  getProjectStatusOptions,
  getRoleOptions,
  getTeam,
  initConfig,
  resolveMemberIdentity,
  runScenarioDiagnostics,
  updateIntelligenceConfig,
  updateMemberConfig,
  updateProjectConfig,
  type CommandOption,
  type ConfigStatus,
  type IntelligenceConfig,
  type MemberConfig,
} from "../api/client";
import { forceUpdateCliAgentSkill, getCliAgentSkillStatuses, restartBackend } from "../api/backend";
import i18n from "../i18n";
import {
  type ScheduledCommandDraft,
  SetupPage,
  buildCharacterPayload,
  buildScheduledCommandExpression,
  buildTaskSchedules,
  createProjectSchema,
  createScheduledCommandDraft,
  draftToCron,
  getMemberFieldErrors,
  getMemberResolveErrorMessage,
  initialProjectValues,
  isValidCron,
  parseCharacterFields,
  parseCommandExpression,
  parseCron,
  parseGitHub,
  quoteCommandArg,
  splitCommandLine,
  toIntelligenceUpdatePayload,
  toProjectSetupRequest,
  toProjectUpdateRequest,
} from "./SetupPage";
import "../i18n";

const t = i18n.getFixedT("en");
const dialogMock = vi.hoisted(() => ({
  open: vi.fn(async () => null as string | null),
}));

vi.mock("../api/backend", () => ({
  forceUpdateCliAgentSkill: vi.fn(async (agent: string) => ({
    agent,
    agent_home: `/home/.${agent}`,
    skill_path: `/home/.${agent}/skills/guildbotics/SKILL.md`,
    status: "up_to_date",
    can_force_update: false,
  })),
  getCliAgentSkillStatuses: vi.fn(async () => ({ agents: [] })),
  restartBackend: vi.fn(async () => undefined),
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: dialogMock.open,
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    addMemberConfig: vi.fn(async () => configWriteResponse()),
    deleteMemberConfig: vi.fn(async () => configWriteResponse()),
    getCliAgentDetections: vi.fn(async () => ({
      agents: [
        {
          name: "codex",
          executable: "codex",
          detected: true,
          path: "/usr/local/bin/codex",
        },
      ],
    })),
    getCommandOptions: vi.fn(async () => ({ options: [] })),
    getConfigStatus: vi.fn(async () => ({
      cwd: "/workspace",
      env_file: "/workspace/.env",
      env_file_exists: true,
      config_dir: "/workspace/.guildbotics/config",
      project_file: "/workspace/.guildbotics/config/project.yml",
      project_file_exists: true,
      storage_dir: "/workspace/.guildbotics",
    })),
    getIntelligenceConfig: vi.fn(async () => ({
      config_dir: "/workspace/.guildbotics/config",
      person_id: null,
      inherited: false,
      model_mapping: { default: "models/openai.yml", openai: "models/openai.yml" },
      models: [
        {
          path: "models/openai.yml",
          provider: "openai",
          model_class: "OpenAIModel",
          model_id: "gpt-5",
        },
      ],
      cli_agent_mapping: { default: "codex-cli.yml", codex: "codex-cli.yml" },
      cli_agents: [
        {
          path: "codex-cli.yml",
          name: "codex",
          env: {},
          script: "codex",
          detected: true,
          detected_path: "/usr/local/bin/codex",
        },
      ],
      brain_mapping: [],
    })),
    getMemberConfig: vi.fn(async () => memberConfig()),
    getProjectStatusOptions: vi.fn(async () => ({ available: false, statuses: [] })),
    getAgentFieldState: vi.fn(async () => ({
      available: false,
      exists: false,
      options: [],
      missing: [],
    })),
    ensureAgentField: vi.fn(async () => ({
      available: true,
      exists: true,
      options: [],
      missing: [],
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
      lane_map: { ready: "Todo", working: "In Progress", done: "Done" },
      has_google_api_key: false,
      has_openai_api_key: true,
      has_anthropic_api_key: false,
    })),
    getRoleOptions: vi.fn(async () => ({
      roles: [{ role_id: "product", summary: "Product", description: "" }],
    })),
    getTeam: vi.fn(async () => ({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [{ person_id: "alice", name: "Alice", is_active: true, roles: ["product"] }],
    })),
    initConfig: vi.fn(async () => configWriteResponse()),
    resolveMemberIdentity: vi.fn(async () => ({
      person_id: "alice",
      github_username: "alice",
      github_user_id: 1,
      git_email: "alice@example.com",
    })),
    runScenarioDiagnostics: vi.fn(async () => ({
      ok: true,
      active_members: ["alice"],
      checks: [],
      warnings: [],
      errors: [],
    })),
    updateIntelligenceConfig: vi.fn(async () => configWriteResponse()),
    updateMemberConfig: vi.fn(async () => configWriteResponse()),
    updateProjectConfig: vi.fn(async () => configWriteResponse()),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  // Restore the default existing-project responses so that first-setup tests,
  // which install persistent `mockResolvedValue` overrides, do not leak into
  // subsequent existing-project tests.
  vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: true }));
  vi.mocked(getTeam).mockResolvedValue({
    project: { name: "Demo", language_code: "en", language_name: "English" },
    members: [{ person_id: "alice", name: "Alice", is_active: true, roles: ["product"] }],
  });
  vi.mocked(getProjectConfig).mockResolvedValue(projectConfig({ description: "Demo project" }));
  vi.mocked(getCliAgentDetections).mockResolvedValue({
    agents: [{ name: "codex", executable: "codex", detected: true, path: "/usr/local/bin/codex" }],
  });
});

afterEach(() => {
  notifications.clean();
  Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
});

describe("SetupPage", () => {
  it("allows sidebar navigation after opening a members deep link", async () => {
    const user = userEvent.setup();
    renderSetupPage("/setup?section=members&tab=patrol");

    expect(await screen.findByText("Alice (alice)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Project" }));

    expect(await screen.findByLabelText("Workspace")).toBeInTheDocument();
    expect(screen.queryByText("Alice (alice)")).not.toBeInTheDocument();
  });

  it("switches from settings mode to first setup mode when the selected workspace is unconfigured", async () => {
    const user = userEvent.setup();
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    renderSetupPage("/setup");

    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();

    dialogMock.open.mockResolvedValueOnce("/empty-workspace");
    vi.mocked(getConfigStatus).mockResolvedValueOnce(
      configStatus({
        cwd: "/empty-workspace",
        env_file: "/empty-workspace/.env",
        env_file_exists: false,
        config_dir: "/empty-workspace/.guildbotics/config",
        project_file: "/empty-workspace/.guildbotics/config/team/project.yml",
        project_file_exists: false,
      }),
    );
    vi.mocked(getTeam).mockRejectedValueOnce(new Error("project config missing"));

    await user.click(screen.getByRole("button", { name: "Choose" }));

    await waitFor(() => expect(restartBackend).toHaveBeenCalledWith("/empty-workspace"));
    expect(await screen.findByRole("heading", { name: "First setup" })).toBeInTheDocument();
    expect(screen.getByText("Input progress: 0 of 4 sections completed")).toBeInTheDocument();
    expect(screen.getByLabelText("Workspace")).toHaveValue("/empty-workspace");
  });

  it("switches from first setup mode to settings mode when the selected workspace is configured", async () => {
    const user = userEvent.setup();
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    vi.mocked(getConfigStatus).mockResolvedValueOnce(
      configStatus({
        cwd: "/empty-workspace",
        env_file: "/empty-workspace/.env",
        env_file_exists: false,
        config_dir: "/empty-workspace/.guildbotics/config",
        project_file: "/empty-workspace/.guildbotics/config/team/project.yml",
        project_file_exists: false,
      }),
    );
    renderSetupPage("/setup");

    expect(await screen.findByRole("heading", { name: "First setup" })).toBeInTheDocument();

    dialogMock.open.mockResolvedValueOnce("/configured-workspace");
    vi.mocked(getConfigStatus).mockResolvedValueOnce(
      configStatus({
        cwd: "/configured-workspace",
        env_file: "/configured-workspace/.env",
        env_file_exists: true,
        config_dir: "/configured-workspace/.guildbotics/config",
        project_file: "/configured-workspace/.guildbotics/config/team/project.yml",
        project_file_exists: true,
      }),
    );
    vi.mocked(getProjectConfig).mockResolvedValueOnce(
      projectConfig({
        config_dir: "/configured-workspace/.guildbotics/config",
        env_file_path: "/configured-workspace/.env",
        description: "Configured workspace",
      }),
    );
    vi.mocked(getTeam).mockResolvedValueOnce({
      project: { name: "Configured", language_code: "en", language_name: "English" },
      members: [{ person_id: "bob", name: "Bob", is_active: true, roles: ["product"] }],
    });

    await user.click(screen.getByRole("button", { name: "Choose" }));

    await waitFor(() => expect(restartBackend).toHaveBeenCalledWith("/configured-workspace"));
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByLabelText("Workspace")).toHaveValue("/configured-workspace");
  });

  it("renders the first-setup required progress and project section fields", async () => {
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: false }));
    vi.mocked(getTeam).mockRejectedValue(
      new ApiRequestError({ code: "not_found", message: "missing", context: {} }),
    );
    renderSetupPage("/setup");

    expect(await screen.findByRole("heading", { name: "First setup" })).toBeInTheDocument();
    expect(screen.getByText("Input progress: 0 of 4 sections completed")).toBeInTheDocument();
    expect(screen.getByText(t("setup.saveMode.manual"))).toBeInTheDocument();

    await waitFor(() => expect(screen.getByLabelText("Workspace")).toHaveValue("/workspace"));
    // In first-setup mode the existing project config is not loaded, so the
    // description starts empty.
    expect(screen.getByLabelText("Project description")).toHaveValue("");
    expect(screen.getByText(t("setup.project.agentLanguage"))).toBeInTheDocument();
  });

  it("shows the LLM provider and CLI agent selection with API-key availability", async () => {
    const user = userEvent.setup();
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: false }));
    vi.mocked(getTeam).mockRejectedValue(
      new ApiRequestError({ code: "not_found", message: "missing", context: {} }),
    );
    renderSetupPage("/setup");

    await screen.findByRole("heading", { name: "First setup" });
    await user.click(screen.getByRole("button", { name: "LLM / CLI agent" }));

    expect(await screen.findByText(t("setup.intelligence.defaultProvider"))).toBeInTheDocument();
    // The provider buttons expose "<label><family>" as their accessible name
    // (e.g. "OpenAIGPT"); anchor the match so the OpenAI provider is not
    // confused with the "OpenAI Codex CLI" agent button.
    expect(screen.getByRole("button", { name: /^OpenAIGPT$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Anthropic ClaudeClaude$/ })).toBeInTheDocument();

    const keyInput = screen.getByLabelText("OpenAI API key");
    await user.type(keyInput, "sk-test");
    expect(keyInput).toHaveValue("sk-test");

    expect(screen.getByRole("button", { name: /OpenAI Codex CLI/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Claude Code/ })).toBeDisabled();
  });

  it("shows CLI agent skill status and allows an explicit overwrite", async () => {
    const user = userEvent.setup();
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: false }));
    vi.mocked(getTeam).mockRejectedValue(
      new ApiRequestError({ code: "not_found", message: "missing", context: {} }),
    );
    vi.mocked(getCliAgentSkillStatuses).mockResolvedValue({
      agents: [
        {
          agent: "codex",
          agent_home: "/home/.codex",
          skill_path: "/home/.codex/skills/guildbotics/SKILL.md",
          status: "user_modified",
          can_force_update: true,
        },
      ],
    });
    renderSetupPage("/setup");

    await screen.findByRole("heading", { name: "First setup" });
    await user.click(screen.getByRole("button", { name: "LLM / CLI agent" }));

    expect(await screen.findByText(t("setup.intelligence.skillStatusTitle"))).toBeInTheDocument();
    expect(
      screen.getByText(t("setup.intelligence.skillStatusMessages.user_modified")),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("setup.intelligence.skillOverwrite") }));

    expect(vi.mocked(forceUpdateCliAgentSkill).mock.calls[0][0]).toBe("codex");
  });

  it("marks GitHub section ready for the disabled decision and incomplete when enabled without URLs", async () => {
    const user = userEvent.setup();
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: false }));
    vi.mocked(getTeam).mockRejectedValue(
      new ApiRequestError({ code: "not_found", message: "missing", context: {} }),
    );
    renderSetupPage("/setup");

    await screen.findByRole("heading", { name: "First setup" });

    // The GitHub use/don't decision now lives in the Project section (default).
    // The control is a Mantine Select; its hidden listbox shares the accessible
    // label, so target the input via its textbox role.
    const decision = await screen.findByRole("textbox", { name: "GitHub integration" });
    await user.click(decision);
    await user.click(await screen.findByRole("option", { name: "Use GitHub" }));

    // GitHub connection details (incl. the project URL) live in the GitHub
    // section, which now comes last.
    await user.click(screen.getByRole("button", { name: "GitHub" }));
    expect(screen.getByLabelText(t("setup.github.projectUrl"))).toBeEnabled();

    // Switch the decision off from the Project section: the GitHub section then
    // shows the disabled hint instead of connection fields.
    await user.click(screen.getByRole("button", { name: "Project" }));
    await user.click(screen.getByRole("textbox", { name: "GitHub integration" }));
    await user.click(await screen.findByRole("option", { name: "Do not use GitHub" }));
    await user.click(screen.getByRole("button", { name: "GitHub" }));
    expect(screen.getByText(t("setup.github.disabledHint"))).toBeInTheDocument();
  });

  it("offers fetched status options for lane mapping when GitHub is enabled", async () => {
    const user = userEvent.setup();
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: false }));
    vi.mocked(getTeam).mockRejectedValue(
      new ApiRequestError({ code: "not_found", message: "missing", context: {} }),
    );
    vi.mocked(getProjectStatusOptions).mockResolvedValue({
      available: true,
      statuses: ["Backlog", "Todo", "In Progress", "Done"],
    });
    renderSetupPage("/setup");

    await screen.findByRole("heading", { name: "First setup" });

    // Enable GitHub in the Project section, then open the GitHub section.
    const decision = await screen.findByRole("textbox", { name: "GitHub integration" });
    await user.click(decision);
    await user.click(await screen.findByRole("option", { name: "Use GitHub" }));
    await user.click(screen.getByRole("button", { name: "GitHub" }));

    // Lane options are fetched when the Project URL loses focus.
    const projectUrl = screen.getByLabelText(t("setup.github.projectUrl"));
    await user.type(projectUrl, "https://github.com/orgs/acme/projects/9");
    await user.tab();

    const readyInput = screen.getByRole("textbox", { name: t("setup.github.laneReady") });
    expect(readyInput).toHaveValue("Todo");
    // Opening the lane Select shows every fetched option, with no filtering by
    // the current value ("Todo").
    await user.click(readyInput);
    expect(await screen.findByRole("option", { name: "Backlog" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Todo" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "In Progress" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Done" })).toBeInTheDocument();

    // Selection is strict: a board lane is chosen from the list.
    await user.click(screen.getByRole("option", { name: "Backlog" }));
    expect(readyInput).toHaveValue("Backlog");
  });

  it("loads lane options on open when the Project URL is already configured", async () => {
    const user = userEvent.setup();
    // Configured project (the default getConfigStatus mock reports an existing
    // project file) with GitHub already enabled and a Project URL set.
    vi.mocked(getProjectConfig).mockResolvedValue(
      projectConfig({
        github_enabled: true,
        github_project_url: "https://github.com/orgs/acme/projects/9",
        lane_map: { ready: "Todo", working: "In Progress", done: "Done" },
      }),
    );
    vi.mocked(getProjectStatusOptions).mockResolvedValue({
      available: true,
      statuses: ["Backlog", "Todo", "In Progress", "Done"],
    });
    renderSetupPage("/setup");

    await screen.findByLabelText("Project description");
    await user.click(screen.getByRole("button", { name: "GitHub" }));

    // No blur is performed: opening the section with a pre-filled Project URL
    // must fetch the lane options on mount, so the strict Select is populated.
    const readyInput = await screen.findByRole("textbox", { name: t("setup.github.laneReady") });
    await user.click(readyInput);
    expect(await screen.findByRole("option", { name: "Backlog" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Done" })).toBeInTheDocument();
  });

  it("shows Agent field state and adds missing members", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectConfig).mockResolvedValue(
      projectConfig({
        github_enabled: true,
        github_project_url: "https://github.com/orgs/acme/projects/9",
        lane_map: { ready: "Todo", working: "In Progress", done: "Done" },
      }),
    );
    vi.mocked(getAgentFieldState).mockResolvedValue({
      available: true,
      exists: true,
      options: [{ name: "⚙bot1", description: "Bot One" }],
      missing: [{ name: "⚙bot2", description: "Bot Two" }],
    });
    vi.mocked(ensureAgentField).mockResolvedValue({
      available: true,
      exists: true,
      options: [
        { name: "⚙bot1", description: "Bot One" },
        { name: "⚙bot2", description: "Bot Two" },
      ],
      missing: [],
    });
    renderSetupPage("/setup");

    await screen.findByLabelText("Project description");
    await user.click(screen.getByRole("button", { name: "GitHub" }));

    // Registered and not-yet-registered members are both surfaced.
    expect(await screen.findByText("Bot One")).toBeInTheDocument();
    expect(screen.getByText("Bot Two")).toBeInTheDocument();

    // The state-driven button offers to add the one missing member.
    const addButton = await screen.findByRole("button", {
      name: t("setup.github.agentFieldAddMembers", { count: 1 }),
    });
    await user.click(addButton);

    await waitFor(() => expect(vi.mocked(ensureAgentField)).toHaveBeenCalledTimes(1));
    // After syncing nothing is missing, so the action reports it is up to date.
    expect(
      await screen.findByRole("button", { name: t("setup.github.agentFieldUpToDate") }),
    ).toBeInTheDocument();
  });

  it("clears fetched lane options when the Project URL becomes invalid", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectConfig).mockResolvedValue(
      projectConfig({
        github_enabled: true,
        github_project_url: "https://github.com/orgs/acme/projects/9",
        lane_map: { ready: "Todo", working: "In Progress", done: "Done" },
      }),
    );
    vi.mocked(getProjectStatusOptions).mockResolvedValue({
      available: true,
      statuses: ["Backlog", "Todo", "In Progress", "Done"],
    });
    renderSetupPage("/setup");

    await screen.findByLabelText("Project description");
    await user.click(screen.getByRole("button", { name: "GitHub" }));

    // Options load for the configured URL.
    await screen.findByRole("textbox", { name: t("setup.github.laneReady") });
    expect(screen.getByText(t("setup.github.laneMappingHint"))).toBeInTheDocument();

    // Editing the Project URL into an invalid value must drop the stale options
    // (the lanes fall back to manual entry rather than showing another
    // project's lanes).
    const projectUrl = screen.getByLabelText(t("setup.github.projectUrl"));
    await user.clear(projectUrl);
    await user.type(projectUrl, "not-a-valid-url");
    await user.tab();

    expect(await screen.findByText(t("setup.github.laneMappingManualHint"))).toBeInTheDocument();
    expect(screen.queryByText(t("setup.github.laneMappingHint"))).not.toBeInTheDocument();
  });

  it("navigates between sections with the next and back buttons", async () => {
    const user = userEvent.setup();
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: false }));
    vi.mocked(getTeam).mockRejectedValue(
      new ApiRequestError({ code: "not_found", message: "missing", context: {} }),
    );
    renderSetupPage("/setup");

    await screen.findByRole("heading", { name: "First setup" });
    await waitFor(() => expect(screen.getByLabelText("Workspace")).toHaveValue("/workspace"));

    // The Next button stays disabled until the current section is complete, so
    // fill in the required project description and make the GitHub decision
    // (which now lives in the Project section).
    await user.type(screen.getByLabelText("Project description"), "Demo project");
    await user.click(screen.getByRole("textbox", { name: "GitHub integration" }));
    await user.click(await screen.findByRole("option", { name: "Do not use GitHub" }));
    const nextButton = screen.getByRole("button", { name: t("setup.status.next") });
    await waitFor(() => expect(nextButton).toBeEnabled());

    await user.click(nextButton);
    expect(await screen.findByText(t("setup.intelligence.defaultProvider"))).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("setup.status.back") }));
    expect(await screen.findByLabelText("Workspace")).toBeInTheDocument();
  });

  it("creates the initial setup via initConfig and restartBackend", async () => {
    const user = userEvent.setup();
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    vi.mocked(getCliAgentDetections).mockResolvedValue({
      agents: [
        { name: "codex", executable: "codex", detected: true, path: "/usr/local/bin/codex" },
      ],
    });
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: false }));
    vi.mocked(getTeam).mockRejectedValue(
      new ApiRequestError({ code: "not_found", message: "missing", context: {} }),
    );
    renderSetupPage("/setup");

    await screen.findByRole("heading", { name: "First setup" });

    // Complete every required section so the Create action becomes available:
    // project description, the provider API key, and a GitHub decision.
    await waitFor(() => expect(screen.getByLabelText("Workspace")).toHaveValue("/workspace"));
    await user.type(screen.getByLabelText("Project description"), "Demo project");
    // The GitHub decision now lives in the Project section.
    await user.click(await screen.findByRole("textbox", { name: "GitHub integration" }));
    await user.click(await screen.findByRole("option", { name: "Do not use GitHub" }));
    await user.click(screen.getByRole("button", { name: "LLM / CLI agent" }));
    await user.type(await screen.findByLabelText("OpenAI API key"), "sk-test");

    // Add one active member so the members section is complete; the add form is
    // shown by default and pre-filled with character defaults.
    await user.click(screen.getByRole("button", { name: "Members" }));
    await fillRequiredMemberBasics(user, { personId: "alice", personName: "Alice" });
    await user.click(screen.getByRole("button", { name: "Add member" }));

    const createButton = await screen.findByRole("button", { name: t("setup.saveInitial") });
    await user.click(createButton);

    await waitFor(() => expect(initConfig).toHaveBeenCalledTimes(1));
    // The init payload conveys "GitHub disabled" by leaving the owner /
    // project URL fields empty (there is no github_enabled flag).
    expect(vi.mocked(initConfig).mock.calls[0][0]).toMatchObject({
      description: "Demo project",
      llm_api_type: "openai",
      cli_agent: "codex",
      owner: "",
      github_project_url: "",
      openai_api_key: "sk-test",
    });
    await waitFor(() => expect(restartBackend).toHaveBeenCalledWith("/workspace"));
    expect(localStorage.getItem("guildbotics.workspace")).toBe("/workspace");
    expect(await screen.findByText(t("setup.initialCreated.title"))).toBeInTheDocument();
    expect(screen.getByText(/\/workspace\/\.guildbotics\/config/)).toBeInTheDocument();
    expect(screen.getByText(/\/workspace\/\.env/)).toBeInTheDocument();
  });

  it("autosaves an existing project through updateProjectConfig", async () => {
    const user = userEvent.setup();
    renderSetupPage("/setup");

    const description = await screen.findByLabelText("Project description");
    await waitFor(() => expect(description).toHaveValue("Demo project"));
    await user.clear(description);
    await user.type(description, "Updated description");

    await waitFor(() => expect(updateProjectConfig).toHaveBeenCalledTimes(1), { timeout: 3000 });
    expect(vi.mocked(updateProjectConfig).mock.calls[0][0]).toMatchObject({
      description: "Updated description",
    });
    expect(initConfig).not.toHaveBeenCalled();
  });

  it("does not autosave when the form has a validation error", async () => {
    const user = userEvent.setup();
    renderSetupPage("/setup");

    const description = await screen.findByLabelText("Project description");
    await waitFor(() => expect(description).toHaveValue("Demo project"));
    await user.clear(description);

    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(updateProjectConfig).not.toHaveBeenCalled();
  });
});

function renderSetupPage(path: string) {
  const theme = createTheme({
    primaryColor: "dark",
    defaultRadius: "md",
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MantineProvider theme={theme}>
      <Notifications />
      <QueryClientProvider client={queryClient}>
        <MemoryRouter
          future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
          initialEntries={[path]}
        >
          <SetupPage />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

function memberConfig() {
  return {
    person_id: "alice",
    person_name: "Alice",
    person_type: "agent",
    github_account_type: "",
    is_active: true,
    github_username: "",
    git_email: "",
    roles: ["product"],
    speaking_style: "Professional",
    relationships: "",
    character: {},
    github_installation_id: null,
    github_app_id: null,
    github_private_key_path: "",
    has_github_installation_id: false,
    has_github_app_id: false,
    has_github_private_key_path: false,
    has_github_access_token: false,
    slack_user_id: "",
    has_slack_bot_token: false,
    has_slack_app_token: false,
    slack_channels: [],
    slack_channel_participation: {},
    routine_commands: [],
    task_schedules: [],
  };
}

function configWriteResponse() {
  return {
    project: null,
    member: null,
    intelligence: null,
  };
}

type ProjectFormValues = Parameters<typeof toProjectSetupRequest>[0];
type MemberFormValues = Parameters<typeof getMemberFieldErrors>[0];

function baseProjectValues(overrides: Partial<ProjectFormValues> = {}): ProjectFormValues {
  return {
    workspaceDir: "/workspace",
    envFileOption: "append",
    language: "en",
    description: "Demo project",
    llmApiType: "openai",
    cliAgent: "codex",
    googleApiKey: "",
    openaiApiKey: "",
    anthropicApiKey: "",
    githubDecision: "disabled",
    githubEnabled: false,
    githubProjectUrl: "",
    laneReady: "Todo",
    laneWorking: "In Progress",
    laneDone: "Done",
    ...overrides,
  };
}

function baseMemberValues(overrides: Partial<MemberFormValues> = {}): MemberFormValues {
  return {
    personType: "human",
    githubAccountType: "human",
    identity: "",
    personId: "alice",
    personName: "Alice",
    githubUsername: "alice",
    gitEmail: "alice@example.com",
    githubInstallationId: "",
    githubAppId: "",
    githubPrivateKeyPath: "",
    githubAccessToken: "",
    slackUserId: "U012345678",
    slackBotToken: "",
    slackAppToken: "",
    slackChannelsText: "",
    storedMemberSecrets: {
      githubInstallationId: false,
      githubAppId: false,
      githubPrivateKeyPath: false,
      githubAccessToken: false,
      slackBotToken: false,
      slackAppToken: false,
    },
    roles: ["product"],
    speakingStyle: "Professional",
    characterArchetype: "manager",
    characterTraits: ["organized"],
    characterInterests: ["planning"],
    characterJoinWhenText: "When planning is needed",
    characterAvoidWhenText: "When chatting",
    characterContributionText: "Clarify next actions",
    existingPersonIds: [],
    originalPersonId: null,
    ...overrides,
  };
}

function configStatus(overrides: Record<string, unknown> = {}): ConfigStatus {
  return {
    cwd: "/workspace",
    env_file: "/workspace/.env",
    env_file_exists: true,
    config_dir: "/workspace/.guildbotics/config",
    project_file: "/workspace/.guildbotics/config/project.yml",
    project_file_exists: false,
    storage_dir: "/workspace/.guildbotics",
    ...overrides,
  } as ConfigStatus;
}

type ProjectConfigValue = Parameters<typeof toProjectUpdateRequest>[2];

function projectConfig(overrides: Record<string, unknown> = {}): ProjectConfigValue {
  return {
    config_dir: "/workspace/.guildbotics/config",
    env_file_path: "/workspace/.env",
    language: "en",
    description: "Existing project",
    llm_api_type: "openai",
    cli_agent: "codex",
    github_enabled: false,
    github_project_url: "",
    lane_map: { ready: "Todo", working: "In Progress", done: "Done" },
    has_google_api_key: false,
    has_openai_api_key: true,
    has_anthropic_api_key: false,
    ...overrides,
  } as ProjectConfigValue;
}

function commandOption(overrides: Partial<CommandOption> = {}): CommandOption {
  return {
    command: "workflows/ticket_driven_workflow",
    label: "Ticket workflow",
    description: "",
    category: "workflow",
    source: "template",
    path: "workflows/ticket_driven_workflow.md",
    arguments: [],
    supports_raw_args: true,
    recommended_input: "",
    requirements: [],
    ...overrides,
  };
}

function firstError(
  result: ReturnType<ReturnType<typeof createProjectSchema>["safeParse"]>,
  path: string,
) {
  if (result.success) {
    return undefined;
  }
  return result.error.issues.find((issue) => issue.path.join(".") === path)?.message;
}

describe("createProjectSchema", () => {
  const schema = createProjectSchema(t);

  it("requires a workspace", () => {
    const result = schema.safeParse(baseProjectValues({ workspaceDir: "" }));
    expect(firstError(result, "workspaceDir")).toBe(t("setup.validation.workspaceRequired"));
  });

  it("requires a project description", () => {
    const result = schema.safeParse(baseProjectValues({ description: "   " }));
    expect(firstError(result, "description")).toBe(t("setup.validation.descriptionRequired"));
  });

  it("requires a GitHub decision", () => {
    const result = schema.safeParse(baseProjectValues({ githubDecision: "" }));
    expect(firstError(result, "githubDecision")).toBe(t("setup.validation.githubDecisionRequired"));
  });

  it("does not validate the GitHub Project URL when GitHub is disabled", () => {
    const result = schema.safeParse(
      baseProjectValues({
        githubDecision: "disabled",
        githubProjectUrl: "",
      }),
    );
    expect(result.success).toBe(true);
  });

  it("validates the GitHub Project URL when enabled", () => {
    const result = schema.safeParse(
      baseProjectValues({
        githubDecision: "enabled",
        githubProjectUrl: "not-a-url",
      }),
    );
    expect(firstError(result, "githubProjectUrl")).toBe(t("setup.validation.githubProjectInvalid"));
  });

  it("accepts a fully valid GitHub-enabled project", () => {
    const result = schema.safeParse(
      baseProjectValues({
        githubDecision: "enabled",
        githubProjectUrl: "https://github.com/orgs/acme/projects/7",
      }),
    );
    expect(result.success).toBe(true);
  });

  it("rejects identical ready and done lanes", () => {
    const result = schema.safeParse(
      baseProjectValues({
        githubDecision: "enabled",
        githubProjectUrl: "https://github.com/orgs/acme/projects/7",
        laneReady: "Done",
        laneDone: "Done",
      }),
    );
    expect(firstError(result, "laneDone")).toBe(t("setup.validation.laneReadyDoneSame"));
  });
});

describe("parseGitHub", () => {
  it("parses an organization project URL", () => {
    const parsed = parseGitHub("https://github.com/orgs/acme/projects/12");
    expect(parsed).toMatchObject({
      owner: "acme",
      projectId: "12",
      projectUrl: "https://github.com/orgs/acme/projects/12",
      projectValid: true,
    });
  });

  it("parses a user project URL", () => {
    const parsed = parseGitHub("https://github.com/users/alice/projects/3?query=1");
    expect(parsed.owner).toBe("alice");
    expect(parsed.projectId).toBe("3");
    expect(parsed.projectUrl).toBe("https://github.com/users/alice/projects/3");
    expect(parsed.projectValid).toBe(true);
  });

  it("invalidates non-github project URLs", () => {
    const parsed = parseGitHub("https://example.com/orgs/acme/projects/1");
    expect(parsed.projectValid).toBe(false);
    expect(parsed.owner).toBe("");
    expect(parsed.projectId).toBe("");
  });
});

describe("initialProjectValues", () => {
  it("returns defaults when no config exists", () => {
    localStorage.clear();
    const values = initialProjectValues(undefined, "ja", null, undefined);
    expect(values).toMatchObject({
      workspaceDir: "",
      envFileOption: "overwrite",
      language: "ja",
      description: "",
      llmApiType: "openai",
      cliAgent: "codex",
      githubDecision: "",
      githubEnabled: false,
    });
  });

  it("derives workspace and append option from config status", () => {
    const values = initialProjectValues(
      configStatus({ env_file_exists: true }),
      "en",
      null,
      undefined,
    );
    expect(values.workspaceDir).toBe("/workspace");
    expect(values.envFileOption).toBe("append");
  });

  it("maps a missing env file to overwrite option", () => {
    const values = initialProjectValues(
      configStatus({ env_file_exists: false }),
      "en",
      null,
      undefined,
    );
    expect(values.envFileOption).toBe("overwrite");
  });

  it("uses the project language when the project file exists", () => {
    const values = initialProjectValues(
      configStatus({ project_file_exists: true }),
      "en",
      "ja",
      undefined,
    );
    expect(values.language).toBe("ja");
  });

  it("hydrates from an existing project config without exposing API keys", () => {
    const values = initialProjectValues(
      configStatus(),
      "en",
      null,
      projectConfig({
        language: "ja",
        description: "Existing",
        github_enabled: true,
        github_project_url: "https://github.com/orgs/acme/projects/1",
      }),
    );
    expect(values.language).toBe("ja");
    expect(values.description).toBe("Existing");
    expect(values.githubDecision).toBe("enabled");
    expect(values.githubEnabled).toBe(true);
    expect(values.googleApiKey).toBe("");
    expect(values.openaiApiKey).toBe("");
    expect(values.anthropicApiKey).toBe("");
  });
});

describe("toProjectSetupRequest", () => {
  it("honors the env file option override and omits GitHub fields when disabled", () => {
    const request = toProjectSetupRequest(
      baseProjectValues({ githubDecision: "disabled" }),
      configStatus(),
      { envFileOption: "overwrite" },
    );
    expect(request.env_file_option).toBe("overwrite");
    expect(request.env_file_path).toBe("/workspace/.env");
    expect(request.config_dir).toBe("/workspace/.guildbotics/config");
    expect(request.owner).toBe("");
    expect(request.project_id).toBe("");
    expect(request.github_project_url).toBe("");
  });

  it("populates GitHub fields when enabled", () => {
    const request = toProjectSetupRequest(
      baseProjectValues({
        githubDecision: "enabled",
        githubProjectUrl: "https://github.com/orgs/acme/projects/9",
      }),
      configStatus(),
    );
    expect(request.owner).toBe("acme");
    expect(request.project_id).toBe("9");
    expect(request.github_project_url).toBe("https://github.com/orgs/acme/projects/9");
  });

  it("includes a trimmed lane_map when GitHub is enabled", () => {
    const request = toProjectSetupRequest(
      baseProjectValues({
        githubDecision: "enabled",
        githubProjectUrl: "https://github.com/orgs/acme/projects/9",
        laneReady: " Ready ",
        laneWorking: " Doing ",
        laneDone: " Shipped ",
      }),
      configStatus(),
    );
    expect(request.lane_map).toEqual({
      ready: "Ready",
      working: "Doing",
      done: "Shipped",
    });
  });

  it("falls back to default lane names when fields are blank", () => {
    const request = toProjectSetupRequest(
      baseProjectValues({
        githubDecision: "enabled",
        githubProjectUrl: "https://github.com/orgs/acme/projects/9",
        laneReady: "",
        laneWorking: "",
        laneDone: "",
      }),
      configStatus(),
    );
    expect(request.lane_map).toEqual({
      ready: "Todo",
      working: "In Progress",
      done: "Done",
    });
  });

  it("omits lane_map when GitHub is disabled", () => {
    const request = toProjectSetupRequest(
      baseProjectValues({ githubDecision: "disabled" }),
      configStatus(),
    );
    expect(request.lane_map).toBeUndefined();
  });
});

describe("toProjectUpdateRequest", () => {
  const snapshot = projectConfig();

  it("omits empty API keys so existing secrets are preserved", () => {
    const request = toProjectUpdateRequest(
      baseProjectValues({ openaiApiKey: "", googleApiKey: "", anthropicApiKey: "" }),
      configStatus(),
      snapshot,
    );
    expect(request.openai_api_key).toBeUndefined();
    expect(request.google_api_key).toBeUndefined();
    expect(request.anthropic_api_key).toBeUndefined();
  });

  it("forwards non-empty API keys", () => {
    const request = toProjectUpdateRequest(
      baseProjectValues({ openaiApiKey: "sk-new" }),
      configStatus(),
      snapshot,
    );
    expect(request.openai_api_key).toBe("sk-new");
  });

  it("disables GitHub and clears related fields", () => {
    const request = toProjectUpdateRequest(
      baseProjectValues({ githubDecision: "disabled" }),
      configStatus(),
      snapshot,
    );
    expect(request.github_enabled).toBe(false);
    expect(request.owner).toBe("");
  });

  it("enables GitHub from the Project URL", () => {
    const request = toProjectUpdateRequest(
      baseProjectValues({
        githubDecision: "enabled",
        githubProjectUrl: "https://github.com/orgs/acme/projects/5",
      }),
      configStatus(),
      snapshot,
    );
    expect(request.github_enabled).toBe(true);
    expect(request.owner).toBe("acme");
    expect(request.project_id).toBe("5");
  });
});

describe("getMemberFieldErrors", () => {
  it("returns no errors for a valid human member without GitHub auth", () => {
    const errors = getMemberFieldErrors(baseMemberValues({ personType: "human" }), t);
    expect(errors).toEqual({});
  });

  it("flags missing core member fields", () => {
    const errors = getMemberFieldErrors(
      baseMemberValues({
        personType: "agent",
        githubAccountType: "none",
        personId: "",
        personName: "",
        githubUsername: "",
        gitEmail: "",
        roles: [],
        speakingStyle: "",
        characterArchetype: "",
        characterTraits: [],
        characterInterests: [],
        characterJoinWhenText: "",
        characterAvoidWhenText: "",
        characterContributionText: "",
      }),
      t,
    );
    expect(errors.personId).toBe(t("setup.validation.memberIdRequired"));
    expect(errors.personName).toBe(t("setup.validation.memberNameRequired"));
    expect(errors.roles).toBe(t("setup.validation.memberRolesRequired"));
    expect(errors.speakingStyle).toBe(t("setup.validation.memberSpeakingStyleRequired"));
    expect(errors.characterArchetype).toBe(t("setup.validation.memberCharacterArchetypeRequired"));
    expect(errors.characterTraits).toBe(t("setup.validation.memberCharacterTraitsRequired"));
    expect(errors.characterInterests).toBe(t("setup.validation.memberCharacterInterestsRequired"));
    expect(errors.characterJoinWhenText).toBe(
      t("setup.validation.memberCharacterJoinWhenRequired"),
    );
    expect(errors.characterAvoidWhenText).toBe(
      t("setup.validation.memberCharacterAvoidWhenRequired"),
    );
    expect(errors.characterContributionText).toBe(
      t("setup.validation.memberCharacterContributionRequired"),
    );
  });

  it("rejects invalid and duplicate member ids", () => {
    expect(getMemberFieldErrors(baseMemberValues({ personId: "Has Space" }), t).personId).toBe(
      t("setup.validation.memberIdInvalid"),
    );
    expect(
      getMemberFieldErrors(
        baseMemberValues({
          personId: "bob",
          existingPersonIds: ["bob"],
          originalPersonId: "alice",
        }),
        t,
      ).personId,
    ).toBe(t("setup.validation.memberIdDuplicate"));
  });

  it("requires an access token for machine_user and proxy_agent members", () => {
    for (const githubAccountType of ["machine_user", "proxy_agent"] as const) {
      const errors = getMemberFieldErrors(
        baseMemberValues({ personType: "agent", githubAccountType, githubAccessToken: "" }),
        t,
      );
      expect(errors.githubAccessToken).toBe(t("setup.validation.githubAccessTokenRequired"));
    }
  });

  it("accepts a stored access token for machine_user members", () => {
    const errors = getMemberFieldErrors(
      baseMemberValues({
        personType: "agent",
        githubAccountType: "machine_user",
        githubAccessToken: "",
        storedMemberSecrets: {
          githubInstallationId: false,
          githubAppId: false,
          githubPrivateKeyPath: false,
          githubAccessToken: true,
          slackBotToken: false,
          slackAppToken: false,
        },
      }),
      t,
    );
    expect(errors.githubAccessToken).toBeUndefined();
  });

  it("validates the format of a provided access token", () => {
    const errors = getMemberFieldErrors(
      baseMemberValues({
        personType: "agent",
        githubAccountType: "machine_user",
        githubAccessToken: "bad-token",
      }),
      t,
    );
    expect(errors.githubAccessToken).toBe(t("setup.validation.githubAccessTokenInvalid"));
  });

  it("requires installation id, app id, and private key path for github_apps", () => {
    const errors = getMemberFieldErrors(
      baseMemberValues({
        personType: "agent",
        githubAccountType: "github_apps",
        identity: "https://github.com/organizations/acme/settings/apps/my-app",
        githubInstallationId: "",
        githubAppId: "",
        githubPrivateKeyPath: "",
      }),
      t,
    );
    expect(errors.githubInstallationId).toBe(t("setup.validation.githubInstallationIdRequired"));
    expect(errors.githubAppId).toBe(t("setup.validation.githubAppIdRequired"));
    expect(errors.githubPrivateKeyPath).toBe(t("setup.validation.githubPrivateKeyPathRequired"));
  });

  it("validates Slack channels, bot token, and app token", () => {
    const errors = getMemberFieldErrors(
      baseMemberValues({
        personType: "agent",
        githubAccountType: "none",
        slackChannelsText: "general, Dev",
        slackBotToken: "not-a-bot-token",
        slackAppToken: "not-an-app-token",
      }),
      t,
    );
    expect(errors.slackChannelsText).toBe(t("setup.validation.slackChannelsInvalid"));
    expect(errors.slackBotToken).toBe(t("setup.validation.slackBotTokenInvalid"));
    expect(errors.slackAppToken).toBe(t("setup.validation.slackAppTokenInvalid"));
  });

  it("accepts localized Slack channel names", () => {
    const errors = getMemberFieldErrors(
      baseMemberValues({
        personType: "agent",
        githubAccountType: "none",
        slackChannelsText: "general, 開発, かいはつ, カイハツ, C0123456789",
        slackBotToken: "xoxb-valid-token",
        slackAppToken: "xapp-valid-token",
      }),
      t,
    );
    expect(errors.slackChannelsText).toBeUndefined();
  });

  it("requires Slack tokens when channels are configured", () => {
    const errors = getMemberFieldErrors(
      baseMemberValues({
        personType: "agent",
        githubAccountType: "none",
        slackChannelsText: "general",
      }),
      t,
    );
    expect(errors.slackBotToken).toBe(t("setup.validation.slackBotTokenRequired"));
    expect(errors.slackAppToken).toBe(t("setup.validation.slackAppTokenRequired"));
  });

  it("requires a Slack User ID for human members", () => {
    const errors = getMemberFieldErrors(baseMemberValues({ slackUserId: "" }), t);
    expect(errors.slackUserId).toBe(t("setup.validation.slackUserIdRequired"));
    expect(
      getMemberFieldErrors(baseMemberValues({ slackUserId: "not-a-slack-id" }), t).slackUserId,
    ).toBe(t("setup.validation.slackUserIdInvalid"));
  });
});

describe("getMemberResolveErrorMessage", () => {
  it("maps known GitHub identity API error codes", () => {
    for (const code of ["invalid_github_username", "invalid_github_apps_url"]) {
      const error = new ApiRequestError({ code, message: "boom", context: {} });
      expect(getMemberResolveErrorMessage(error, t)).toBe(
        t("setup.validation.memberGithubIdentityNotFound"),
      );
    }
  });

  it("falls back to the generic resolve failure for unknown errors", () => {
    expect(getMemberResolveErrorMessage(new Error("network"), t)).toBe(
      t("setup.validation.memberGithubIdentityResolveFailed"),
    );
    const otherApiError = new ApiRequestError({ code: "rate_limited", message: "x", context: {} });
    expect(getMemberResolveErrorMessage(otherApiError, t)).toBe(
      t("setup.validation.memberGithubIdentityResolveFailed"),
    );
  });
});

describe("character payload round trip", () => {
  it("parses list fields and preserves extra character fields", () => {
    const parsed = parseCharacterFields({
      archetype: "manager",
      traits: [" organized ", "", "strategic"],
      interests: ["planning"],
      conversation_preferences: {
        join_when: ["When planning"],
        avoid_when: ["When chatting"],
        contribution_style: ["Clarify"],
        custom_pref: "keep me",
      },
      extra_top_level: { nested: true },
    });
    expect(parsed.archetype).toBe("manager");
    expect(parsed.traits).toEqual(["organized", "strategic"]);
    expect(parsed.joinWhen).toEqual(["When planning"]);
    expect(parsed.extras).toEqual({
      extra_top_level: { nested: true },
      conversation_preferences: { custom_pref: "keep me" },
    });
  });

  it("handles a malformed character object gracefully", () => {
    const parsed = parseCharacterFields({
      traits: "not-a-list" as unknown as string[],
      conversation_preferences: 42 as unknown as Record<string, unknown>,
    });
    expect(parsed.archetype).toBe("");
    expect(parsed.traits).toEqual([]);
    expect(parsed.joinWhen).toEqual([]);
  });

  it("rebuilds a payload that preserves extras and drops empty lists", () => {
    const parsed = parseCharacterFields({
      archetype: "manager",
      traits: ["organized"],
      interests: ["planning"],
      conversation_preferences: {
        join_when: ["When planning"],
        custom_pref: "keep me",
      },
      extra_top_level: "x",
    });
    const payload = buildCharacterPayload({ ...parsed, avoidWhen: [], contributionStyle: [] });
    expect(payload.archetype).toBe("manager");
    expect(payload.traits).toEqual(["organized"]);
    expect(payload.extra_top_level).toBe("x");
    expect(payload.conversation_preferences).toEqual({
      custom_pref: "keep me",
      join_when: ["When planning"],
    });
  });
});

describe("toIntelligenceUpdatePayload", () => {
  const team = {
    config_dir: "/workspace/.guildbotics/config",
    person_id: null,
    inherited: false,
    model_mapping: { default: "models/openai.yml" },
    models: [
      {
        path: "models/openai.yml",
        provider: "openai",
        model_class: "OpenAIModel",
        model_id: "gpt-5",
      },
    ],
    cli_agent_mapping: { default: "codex-cli.yml" },
    cli_agents: [
      {
        path: "codex-cli.yml",
        name: "codex" as const,
        env: {},
        script: "codex",
        detected: true,
        detected_path: "/usr/local/bin/codex",
      },
    ],
    brain_mapping: [],
  };

  it("emits a full team-default update", () => {
    const payload = toIntelligenceUpdatePayload(team);
    expect(payload).toMatchObject({
      person_id: null,
      inherit_team_defaults: false,
      model_mapping: team.model_mapping,
      models: team.models,
      cli_agents: team.cli_agents,
      brain_mapping: team.brain_mapping,
    });
  });

  it("emits a member override update without full definitions", () => {
    const payload = toIntelligenceUpdatePayload({ ...team, person_id: "alice", inherited: false });
    expect(payload).toEqual({
      config_dir: team.config_dir,
      person_id: "alice",
      inherit_team_defaults: false,
      model_mapping: team.model_mapping,
      cli_agent_mapping: team.cli_agent_mapping,
    });
    expect("models" in payload).toBe(false);
  });

  it("emits an inherit-team-defaults update", () => {
    const payload = toIntelligenceUpdatePayload({ ...team, person_id: "alice", inherited: true });
    expect(payload).toEqual({
      config_dir: team.config_dir,
      person_id: "alice",
      inherit_team_defaults: true,
    });
  });

  it("substitutes person_id via savePersonId", () => {
    const payload = toIntelligenceUpdatePayload(team, "bob");
    expect(payload.person_id).toBe("bob");
  });
});

describe("patrol / schedule helpers", () => {
  it("creates a catalog draft when a command is supplied", () => {
    const draft = createScheduledCommandDraft("workflows/ticket_driven_workflow");
    expect(draft.commandMode).toBe("catalog");
    expect(draft.command).toBe("workflows/ticket_driven_workflow");
    expect(draft.scheduleMode).toBe("daily");
    expect(draft.cron).toBe("0 9 * * *");
  });

  it("creates a custom draft when no command is supplied", () => {
    expect(createScheduledCommandDraft().commandMode).toBe("custom");
  });

  it("converts schedule presets to cron expressions", () => {
    const base = createScheduledCommandDraft("cmd");
    expect(draftToCron({ ...base, scheduleMode: "hourly", minute: 15 })).toBe("15 * * * *");
    expect(draftToCron({ ...base, scheduleMode: "daily", minute: 30, hour: 8 })).toBe("30 8 * * *");
    expect(draftToCron({ ...base, scheduleMode: "weekly", minute: 0, hour: 9, weekday: "3" })).toBe(
      "0 9 * * 3",
    );
    expect(draftToCron({ ...base, scheduleMode: "custom", cron: " */5 * * * * " })).toBe(
      "*/5 * * * *",
    );
  });

  it("clamps out-of-range minute and hour values", () => {
    const base = createScheduledCommandDraft("cmd");
    expect(draftToCron({ ...base, scheduleMode: "daily", minute: 99, hour: 99 })).toBe(
      "59 23 * * *",
    );
  });

  it("parses cron expressions back into preset drafts", () => {
    expect(parseCron("15 * * * *")).toEqual({
      mode: "hourly",
      minute: 15,
      hour: 9,
      weekday: "1",
    });
    expect(parseCron("30 8 * * *")).toEqual({
      mode: "daily",
      minute: 30,
      hour: 8,
      weekday: "1",
    });
    expect(parseCron("0 9 * * 3")).toEqual({
      mode: "weekly",
      minute: 0,
      hour: 9,
      weekday: "3",
    });
    expect(parseCron("0 9 1 * *").mode).toBe("custom");
    expect(parseCron("not valid").mode).toBe("custom");
  });

  it("validates cron field count", () => {
    expect(isValidCron("0 9 * * *")).toBe(true);
    expect(isValidCron("  0   9 * * *  ")).toBe(true);
    expect(isValidCron("0 9 * *")).toBe(false);
  });

  it("splits and quotes command-line arguments", () => {
    expect(splitCommandLine('foo "bar baz" qux')).toEqual(["foo", "bar baz", "qux"]);
    expect(splitCommandLine("a   b")).toEqual(["a", "b"]);
    expect(quoteCommandArg("simple")).toBe("simple");
    expect(quoteCommandArg("has space")).toBe('"has space"');
    expect(quoteCommandArg('quote"and\\slash here')).toBe('"quote\\"and\\\\slash here"');
  });

  it("builds a scheduled command expression with catalog args and raw args", () => {
    const option = commandOption({
      command: "report",
      arguments: [
        { name: "target", kind: "positional", required: true, default: "" },
        { name: "--format", kind: "keyword", required: false, default: "" },
      ],
    });
    const draft: ScheduledCommandDraft = {
      ...createScheduledCommandDraft("report"),
      argValues: { target: "weekly report", "--format": "pdf" },
      rawArgs: "--verbose",
    };
    const expression = buildScheduledCommandExpression(draft, new Map([[option.command, option]]));
    expect(expression).toBe('report "weekly report" --format=pdf --verbose');
  });

  it("uses the trimmed custom command for custom mode", () => {
    const draft: ScheduledCommandDraft = {
      ...createScheduledCommandDraft(),
      commandMode: "custom",
      customCommand: "  my/custom_command  ",
    };
    expect(buildScheduledCommandExpression(draft, new Map())).toBe("my/custom_command");
  });

  it("groups task schedules by command and skips invalid drafts", () => {
    const drafts: ScheduledCommandDraft[] = [
      { ...createScheduledCommandDraft("alpha"), scheduleMode: "daily", minute: 0, hour: 9 },
      { ...createScheduledCommandDraft("alpha"), scheduleMode: "hourly", minute: 30 },
      {
        ...createScheduledCommandDraft(),
        commandMode: "custom",
        customCommand: "",
        scheduleMode: "daily",
      },
    ];
    const schedules = buildTaskSchedules(drafts, new Map());
    expect(schedules).toEqual([{ command: "alpha", schedules: ["0 9 * * *", "30 * * * *"] }]);
  });

  it("parses catalog commands and falls back to custom commands", () => {
    const catalog = [
      commandOption({ command: "report" }),
      commandOption({ command: "report/weekly" }),
    ];
    const matched = parseCommandExpression("report/weekly arg1 arg2", catalog);
    expect(matched.option?.command).toBe("report/weekly");
    expect(matched.command).toBe("report/weekly");
    expect(matched.args).toBe("arg1 arg2");

    const custom = parseCommandExpression('my/cmd "a b" c', catalog);
    expect(custom.option).toBeNull();
    expect(custom.command).toBe("my/cmd");
    expect(custom.args).toBe("a b c");
  });

  it("round trips a catalog command through expression and parse", () => {
    const option = commandOption({ command: "report" });
    const draft: ScheduledCommandDraft = {
      ...createScheduledCommandDraft("report"),
      rawArgs: "alpha beta",
    };
    const expression = buildScheduledCommandExpression(draft, new Map([[option.command, option]]));
    const parsed = parseCommandExpression(expression, [option]);
    expect(parsed.option?.command).toBe("report");
    expect(parsed.args).toBe("alpha beta");
  });
});

function memberConfigDetail(overrides: Partial<MemberConfig> = {}): MemberConfig {
  return {
    person_id: "alice",
    person_name: "Alice",
    // No GitHub linking keeps the loaded member valid for save without extra
    // GitHub identity fields.
    person_type: "agent",
    github_account_type: "",
    is_active: true,
    github_username: "",
    git_email: "",
    roles: ["product"],
    speaking_style: "Professional and concise",
    relationships: "",
    character: {
      archetype: "manager",
      traits: ["organized"],
      interests: ["planning"],
      conversation_preferences: {
        join_when: ["When planning is needed"],
        avoid_when: ["When chatting"],
        contribution_style: ["Clarify next actions"],
      },
    },
    github_installation_id: null,
    github_app_id: null,
    github_private_key_path: "",
    has_github_installation_id: false,
    has_github_app_id: false,
    has_github_private_key_path: false,
    has_github_access_token: false,
    slack_user_id: "",
    has_slack_bot_token: false,
    has_slack_app_token: false,
    slack_channels: [],
    slack_channel_participation: {},
    routine_commands: [],
    task_schedules: [],
    ...overrides,
  };
}

// Fill the required Basic-tab fields so the member form can be submitted.
async function fillRequiredMemberBasics(
  user: ReturnType<typeof userEvent.setup>,
  { personId = "bob", personName = "Bob" }: { personId?: string; personName?: string } = {},
) {
  await user.type(await screen.findByLabelText("Member ID"), personId);
  await user.type(screen.getByLabelText("Display name"), personName);
  await user.type(screen.getByLabelText("Roles"), "product");
  await user.click(await screen.findByRole("option", { name: /^product\b/ }));
}

function lastMemberAddRequest() {
  const calls = vi.mocked(addMemberConfig).mock.calls;
  return calls[calls.length - 1][0];
}

describe("MembersSection", () => {
  it("shows the add form when there are no members", async () => {
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    renderSetupPage("/setup?section=members");

    expect(await screen.findByText(t("setup.members.requiredTitle"))).toBeInTheDocument();
    expect(await screen.findByLabelText("Member ID")).toBeInTheDocument();
    // The form header ("Add member") and the submit button share the same text;
    // assert on the submit button explicitly.
    expect(screen.getByRole("button", { name: t("setup.members.addButton") })).toBeInTheDocument();
    // With no members configured there is no "Add new member" toggle button.
    expect(
      screen.queryByRole("button", { name: t("setup.members.newButton") }),
    ).not.toBeInTheDocument();
  });

  it("adds a member through addMemberConfig with the built payload", async () => {
    const user = userEvent.setup();
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    renderSetupPage("/setup?section=members");

    await fillRequiredMemberBasics(user, { personId: "bob", personName: "Bob" });
    await user.click(screen.getByRole("button", { name: t("setup.members.addButton") }));

    await waitFor(() => expect(addMemberConfig).toHaveBeenCalledTimes(1));
    const request = lastMemberAddRequest();
    expect(request).toMatchObject({
      person_id: "bob",
      person_name: "Bob",
      // The default add form creates an agent without GitHub linking.
      person_type: "agent",
      github_account_type: "",
      is_active: true,
      roles: ["product"],
      config_dir: "/workspace/.guildbotics/config",
      env_file_path: "/workspace/.env",
    });
    // A member without GitHub linking sends no GitHub credentials.
    expect(request.github_username).toBe("");
    expect(request.github_access_token).toBeUndefined();
    expect(request.github_installation_id).toBeUndefined();
  });

  it("edits a persisted member and submits an update payload", async () => {
    const user = userEvent.setup();
    vi.mocked(getMemberConfig).mockResolvedValue(memberConfigDetail({ person_name: "Alice" }));
    renderSetupPage("/setup?section=members");

    await user.click(await screen.findByRole("button", { name: t("setup.members.editButton") }));
    expect(await screen.findByText(t("setup.members.editingBadge", { id: "alice" })));
    await waitFor(() => expect(vi.mocked(getMemberConfig).mock.calls[0]?.[0]).toBe("alice"));

    const nameInput = await screen.findByLabelText("Display name");
    await waitFor(() => expect(nameInput).toHaveValue("Alice"));
    await user.clear(nameInput);
    await user.type(nameInput, "Alice Cooper");
    await user.click(screen.getByRole("button", { name: t("setup.members.saveButton") }));

    await waitFor(() => expect(updateMemberConfig).toHaveBeenCalledTimes(1));
    const [personId, body] = vi.mocked(updateMemberConfig).mock.calls[0];
    expect(personId).toBe("alice");
    expect(body).toMatchObject({
      original_person_id: "alice",
      person_id: "alice",
      person_name: "Alice Cooper",
    });
  });

  it("edits Slack channel participation policies", async () => {
    const user = userEvent.setup();
    vi.mocked(getMemberConfig).mockResolvedValue(
      memberConfigDetail({
        has_slack_bot_token: true,
        has_slack_app_token: true,
        slack_channels: ["general", "random"],
        slack_channel_participation: { general: "strict", random: "social" },
      }),
    );
    renderSetupPage("/setup?section=members&tab=slack");

    await user.click(await screen.findByRole("button", { name: t("setup.members.editButton") }));
    await screen.findByText(t("setup.members.editingBadge", { id: "alice" }));
    await screen.findByDisplayValue("general");
    await screen.findByDisplayValue("random");

    const policyInputs = screen.getAllByLabelText(t("setup.members.slackParticipationPolicy"));
    expect(policyInputs[0]).toHaveValue("Join when needed");
    expect(policyInputs[1]).toHaveValue("Join actively");

    await user.click(policyInputs[1]);
    expect(
      (await screen.findAllByText(t("setup.members.slackParticipationDescriptions.social"))).length,
    ).toBeGreaterThan(0);
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    await user.click(screen.getByRole("button", { name: t("setup.members.saveButton") }));

    await waitFor(() => expect(updateMemberConfig).toHaveBeenCalledTimes(1));
    const body = vi.mocked(updateMemberConfig).mock.calls[0][1];
    expect(body.slack_channels).toEqual(["general", "random"]);
    expect(body.slack_channel_participation).toEqual({
      general: "strict",
      random: "muted",
    });
  });

  it("deletes a member after confirming in the modal", async () => {
    const user = userEvent.setup();
    vi.mocked(getMemberConfig).mockResolvedValue(memberConfigDetail());
    renderSetupPage("/setup?section=members");

    await user.click(await screen.findByRole("button", { name: t("setup.members.editButton") }));
    await waitFor(() => expect(vi.mocked(getMemberConfig).mock.calls[0]?.[0]).toBe("alice"));

    // The footer Delete button opens the confirmation modal; the modal's own
    // Delete button performs the deletion.
    await user.click(await screen.findByRole("button", { name: t("setup.members.deleteButton") }));
    expect(await screen.findByText(t("setup.members.deleteConfirmTitle"))).toBeInTheDocument();
    const confirmButtons = screen.getAllByRole("button", { name: t("setup.members.deleteButton") });
    await user.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => expect(deleteMemberConfig).toHaveBeenCalledTimes(1));
    expect(vi.mocked(deleteMemberConfig).mock.calls[0]).toEqual([
      "alice",
      { config_dir: "/workspace/.guildbotics/config", env_file_path: "/workspace/.env" },
    ]);
  });

  it("keeps added members as drafts before the project is persisted", async () => {
    const user = userEvent.setup();
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ project_file_exists: false }));
    vi.mocked(getTeam).mockRejectedValue(
      new ApiRequestError({ code: "not_found", message: "missing", context: {} }),
    );
    renderSetupPage("/setup?section=members");

    await fillRequiredMemberBasics(user, { personId: "carol", personName: "Carol" });
    await user.click(screen.getByRole("button", { name: t("setup.members.addButton") }));

    await waitFor(() => expect(addMemberConfig).toHaveBeenCalledTimes(1));
    // In draft (not yet persisted) mode the member appears in the list without a
    // backend reload, and getMemberConfig is never queried.
    expect(await screen.findByText("Carol (carol)")).toBeInTheDocument();
    expect(getMemberConfig).not.toHaveBeenCalled();
  });

  it("resolves a GitHub identity and fills username and email", async () => {
    const user = userEvent.setup();
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    vi.mocked(resolveMemberIdentity).mockResolvedValue({
      person_id: "octo",
      github_username: "octocat",
      github_user_id: 42,
      git_email: "octo@example.com",
    });
    renderSetupPage("/setup?section=members");

    await screen.findByLabelText("Member ID");
    await user.click(screen.getByRole("tab", { name: t("setup.members.tabs.github") }));

    await selectGitHubAccountType(user, "Machine Account (Machine User)");
    const usernameField = await screen.findByLabelText(t("setup.members.githubUsername"));
    await user.type(usernameField, "octocat");
    await user.click(screen.getByRole("button", { name: t("setup.members.resolve") }));

    await waitFor(() => expect(resolveMemberIdentity).toHaveBeenCalled());
    expect(vi.mocked(resolveMemberIdentity).mock.calls[0][0]).toEqual({
      person_type: "machine_user",
      identity: "octocat",
    });
    expect(await screen.findByLabelText(t("setup.members.gitEmail"))).toHaveValue(
      "octo@example.com",
    );
  });

  it("shows the resolve failure message for unknown GitHub usernames", async () => {
    const user = userEvent.setup();
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    vi.mocked(resolveMemberIdentity).mockRejectedValue(
      new ApiRequestError({ code: "invalid_github_username", message: "nope", context: {} }),
    );
    renderSetupPage("/setup?section=members");

    await screen.findByLabelText("Member ID");
    await user.click(screen.getByRole("tab", { name: t("setup.members.tabs.github") }));
    await selectGitHubAccountType(user, "Machine Account (Machine User)");
    await user.type(await screen.findByLabelText(t("setup.members.githubUsername")), "octocat");
    await user.click(screen.getByRole("button", { name: t("setup.members.resolve") }));

    expect(
      await screen.findByText(t("setup.validation.memberGithubIdentityNotFound")),
    ).toBeInTheDocument();
  });

  it("changes the required credential fields when switching person type", async () => {
    const user = userEvent.setup();
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    renderSetupPage("/setup?section=members");

    await screen.findByLabelText("Member ID");
    await user.click(screen.getByRole("tab", { name: t("setup.members.tabs.github") }));

    // none: no GitHub auth required.
    expect(
      await screen.findByText(t("setup.members.githubDisabledMemberHint")),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(t("setup.members.accessToken"))).not.toBeInTheDocument();

    // machine_user: an access token field appears.
    await selectGitHubAccountType(user, "Machine Account (Machine User)");
    expect(await screen.findByLabelText(t("setup.members.accessToken"))).toBeInTheDocument();
    expect(screen.queryByLabelText(t("setup.members.installationId"))).not.toBeInTheDocument();

    // github_apps: installation id / app id / private key path appear instead.
    await selectGitHubAccountType(user, "GitHub Apps");
    expect(await screen.findByLabelText(t("setup.members.installationId"))).toBeInTheDocument();
    expect(screen.getByLabelText(t("setup.members.appId"))).toBeInTheDocument();
    expect(screen.getByLabelText(t("setup.members.privateKeyPath"))).toBeInTheDocument();
    expect(screen.queryByLabelText(t("setup.members.accessToken"))).not.toBeInTheDocument();
  });

  it("locks human members inactive and uses Slack User ID settings", async () => {
    const user = userEvent.setup();
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    renderSetupPage("/setup?section=members");

    await screen.findByLabelText("Member ID");
    await selectBasicMemberType(user, "Human");

    const activeSwitch = screen.getByRole("switch", {
      name: new RegExp(t("setup.members.activeSwitch")),
    });
    expect(activeSwitch).toBeDisabled();
    await waitFor(() => expect(activeSwitch).not.toBeChecked());

    await user.click(screen.getByRole("tab", { name: t("setup.members.tabs.github") }));
    const accountType = await screen.findByRole("textbox", {
      name: t("setup.members.githubAccountType"),
    });
    expect(accountType).toBeDisabled();
    expect(accountType).toHaveValue("Human");

    await user.click(screen.getByRole("tab", { name: t("setup.members.tabs.slack") }));
    expect(await screen.findByLabelText(t("setup.members.slackUserId"))).toBeInTheDocument();
    expect(screen.queryByLabelText(t("setup.members.slackBotToken"))).not.toBeInTheDocument();
    expect(screen.queryByLabelText(t("setup.members.slackAppToken"))).not.toBeInTheDocument();
  });

  it("blocks saving a machine_user member until a valid access token is provided", async () => {
    const user = userEvent.setup();
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    renderSetupPage("/setup?section=members");

    await fillRequiredMemberBasics(user, { personId: "bot", personName: "Bot" });
    await user.click(screen.getByRole("tab", { name: t("setup.members.tabs.github") }));
    await selectGitHubAccountType(user, "Machine Account (Machine User)");
    await user.type(await screen.findByLabelText(t("setup.members.githubUsername")), "bot");
    await user.type(screen.getByLabelText(t("setup.members.gitEmail")), "bot@example.com");

    const tokenField = screen.getByLabelText(t("setup.members.accessToken"));
    await user.type(tokenField, "bad-token");
    expect(
      await screen.findByText(t("setup.validation.githubAccessTokenInvalid")),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("setup.members.addButton") })).toBeDisabled();

    await user.clear(tokenField);
    await user.type(tokenField, "ghp_0123456789abcdef0123456789abcdef0123");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: t("setup.members.addButton") })).toBeEnabled(),
    );
  });

  it("validates Slack channels and tokens", async () => {
    const user = userEvent.setup();
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    renderSetupPage("/setup?section=members");

    await screen.findByLabelText("Member ID");
    await user.click(screen.getByRole("tab", { name: t("setup.members.tabs.slack") }));

    const channelInput = await screen.findByLabelText(t("setup.members.slackChannelAdd"));
    await user.type(channelInput, "Dev");
    await user.click(
      screen.getByRole("button", { name: t("setup.members.slackChannelAddButton") }),
    );
    expect(await screen.findByText(t("setup.validation.slackChannelsInvalid"))).toBeInTheDocument();
    expect(screen.queryAllByLabelText(t("setup.members.slackParticipationPolicy"))).toHaveLength(0);

    await user.clear(channelInput);
    await user.type(channelInput, "開発");
    fireEvent.keyDown(channelInput, { key: "Enter", isComposing: true });
    expect(screen.queryAllByLabelText(t("setup.members.slackParticipationPolicy"))).toHaveLength(0);
    fireEvent.keyDown(channelInput, { key: "Enter", isComposing: false });
    await screen.findByDisplayValue("開発");

    await user.type(screen.getByLabelText(t("setup.members.slackBotToken")), "not-a-token");
    expect(await screen.findByText(t("setup.validation.slackBotTokenInvalid"))).toBeInTheDocument();
  });

  it("applies and clears the character preset on the Basic tab", async () => {
    const user = userEvent.setup();
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    renderSetupPage("/setup?section=members");

    // The archetype field's label embeds a "Set default value" action button, so
    // target the input by its preset placeholder instead of the label text.
    const archetype = (await screen.findByPlaceholderText(
      "strategic_project_manager",
    )) as HTMLInputElement;
    // The default add form applies the "professional" preset, so the archetype
    // is pre-filled.
    await waitFor(() => expect(archetype.value).toBe("strategic_project_manager"));

    await user.click(screen.getByRole("button", { name: t("setup.members.clearDefaults") }));
    await waitFor(() => expect(archetype).toHaveValue(""));
  });

  it("surfaces a roles loading error", async () => {
    vi.mocked(getRoleOptions).mockRejectedValue(new Error("roles down"));
    vi.mocked(getTeam).mockResolvedValue({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [],
    });
    renderSetupPage("/setup?section=members");

    expect(await screen.findByText(t("setup.members.rolesLoadError"))).toBeInTheDocument();
  });

  it("runs member diagnostics from the diagnostics tab", async () => {
    const user = userEvent.setup();
    vi.mocked(getMemberConfig).mockResolvedValue(memberConfigDetail());
    vi.mocked(runScenarioDiagnostics).mockResolvedValue({
      ok: false,
      active_members: ["alice"],
      checks: [
        {
          section: "github",
          code: "github_token_missing",
          status: "error",
          target: "alice",
          message: "Token missing",
          person_id: "alice",
          context: {},
        },
      ],
      warnings: [],
      errors: [],
    });
    renderSetupPage("/setup?section=members");

    await user.click(await screen.findByRole("button", { name: t("setup.members.editButton") }));
    await waitFor(() => expect(vi.mocked(getMemberConfig).mock.calls[0]?.[0]).toBe("alice"));
    await user.click(screen.getByRole("tab", { name: t("setup.members.tabs.diagnostics") }));
    await user.click(
      await screen.findByRole("button", { name: t("setup.members.diagnostics.run") }),
    );

    await waitFor(() => expect(vi.mocked(runScenarioDiagnostics).mock.calls[0]?.[0]).toBe("alice"));
    // The failing check has no localized title/description, so its raw message
    // appears in both the alert title and body.
    expect((await screen.findAllByText("Token missing")).length).toBeGreaterThan(0);
    expect(screen.queryByText(t("setup.members.diagnostics.ok"))).not.toBeInTheDocument();
    // A top-of-panel issues summary surfaces the failure without scrolling.
    expect(await screen.findByText(t("setup.members.diagnostics.issuesTitle"))).toBeInTheDocument();
  });
});

async function selectBasicMemberType(
  user: ReturnType<typeof userEvent.setup>,
  optionLabel: string,
) {
  await user.click(await screen.findByRole("textbox", { name: t("setup.members.type") }));
  await user.click(await screen.findByRole("option", { name: optionLabel }));
}

// Mantine Select renders a combobox whose accessible name matches the GitHub
// account type label; open it and pick the option by its visible text.
async function selectGitHubAccountType(
  user: ReturnType<typeof userEvent.setup>,
  optionLabel: string,
) {
  await user.click(
    await screen.findByRole("textbox", { name: t("setup.members.githubAccountType") }),
  );
  await user.click(await screen.findByRole("option", { name: optionLabel }));
}

const PATROL_COMMAND_OPTIONS: CommandOption[] = [
  commandOption({
    command: "workflows/ticket_driven_workflow",
    label: "Ticket workflow",
  }),
  commandOption({
    command: "report",
    label: "Report",
    arguments: [
      { name: "target", kind: "positional", required: true, default: "" },
      { name: "--format", kind: "keyword", required: false, default: "" },
    ],
  }),
];

async function openPatrolTab(user: ReturnType<typeof userEvent.setup>) {
  renderSetupPage("/setup?section=members&tab=patrol");
  await user.click(await screen.findByRole("button", { name: t("setup.members.editButton") }));
  await screen.findByText(t("setup.members.patrol.title"));
}

describe("PatrolSettingsEditor", () => {
  beforeEach(() => {
    // The loaded member has no GitHub linking, so the rest of the form stays
    // valid and save reflects only the patrol/schedule changes under test.
    vi.mocked(getMemberConfig).mockResolvedValue(memberConfigDetail());
    vi.mocked(getCommandOptions).mockResolvedValue({ options: PATROL_COMMAND_OPTIONS });
  });

  it("explains the shared default when the routine override is off", async () => {
    const user = userEvent.setup();
    await openPatrolTab(user);

    expect(
      await screen.findByText(t("setup.members.patrol.usesServiceDefault")),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText(t("setup.members.patrol.routineCommands")),
    ).not.toBeInTheDocument();
  });

  it("requires a routine when the override is enabled with none selected", async () => {
    const user = userEvent.setup();
    await openPatrolTab(user);

    // Clicking the Switch's visible label toggles the member-specific override.
    await user.click(await screen.findByText(t("setup.members.patrol.overrideRoutine")));
    expect(await screen.findByText(t("setup.members.patrol.routineRequired"))).toBeInTheDocument();
    // The error blocks saving even though the rest of the member is valid.
    expect(screen.getByRole("button", { name: t("setup.members.saveButton") })).toBeDisabled();
  });

  it("adds and removes a scheduled command", async () => {
    const user = userEvent.setup();
    await openPatrolTab(user);

    expect(await screen.findByText(t("setup.members.patrol.noSchedules"))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("setup.members.patrol.addSchedule") }));

    // A freshly added schedule defaults to the first catalog command.
    expect(
      await screen.findByDisplayValue("Ticket workflow (workflows/ticket_driven_workflow)"),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: t("setup.members.patrol.removeSchedule") }),
    );
    expect(await screen.findByText(t("setup.members.patrol.noSchedules"))).toBeInTheDocument();
  });

  it("converts catalog command args into the task_schedules payload", async () => {
    const user = userEvent.setup();
    await openPatrolTab(user);

    await user.click(screen.getByRole("button", { name: t("setup.members.patrol.addSchedule") }));
    // Switch the schedule's command to the catalog "report" command, which
    // exposes positional and keyword argument inputs.
    await user.click(await screen.findByRole("textbox", { name: t("commands.command") }));
    await user.click(await screen.findByRole("option", { name: /Report \(report\)/ }));

    await user.type(await screen.findByLabelText("target *"), "weekly report");
    await user.type(screen.getByLabelText("--format"), "pdf");

    await user.click(screen.getByRole("button", { name: t("setup.members.saveButton") }));

    await waitFor(() => expect(updateMemberConfig).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(updateMemberConfig).mock.calls[0];
    expect(body.task_schedules).toEqual([
      { command: 'report "weekly report" --format=pdf', schedules: ["0 9 * * *"] },
    ]);
  });

  it("edits custom command raw args and converts them into task_schedules", async () => {
    const user = userEvent.setup();
    await openPatrolTab(user);

    await user.click(screen.getByRole("button", { name: t("setup.members.patrol.addSchedule") }));
    await user.click(await screen.findByRole("radio", { name: t("commands.modeCustom") }));

    await user.type(await screen.findByLabelText(t("commands.command")), "my/custom_command");
    await user.type(screen.getByLabelText(t("commands.rawArgs")), "--verbose");
    await user.click(screen.getByRole("button", { name: t("setup.members.saveButton") }));

    await waitFor(() => expect(updateMemberConfig).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(updateMemberConfig).mock.calls[0];
    expect(body.task_schedules).toEqual([
      { command: "my/custom_command --verbose", schedules: ["0 9 * * *"] },
    ]);
  });

  it("switches schedule presets and converts each to a cron expression", async () => {
    const user = userEvent.setup();
    await openPatrolTab(user);

    await user.click(screen.getByRole("button", { name: t("setup.members.patrol.addSchedule") }));

    // Default is daily at 09:00.
    expect(await screen.findByDisplayValue("0 9 * * *")).toBeInTheDocument();

    await user.click(
      screen.getByRole("radio", { name: t("setup.members.patrol.cronPresets.hourly") }),
    );
    await waitFor(() => expect(screen.getByDisplayValue("0 * * * *")).toBeInTheDocument());

    await user.click(
      screen.getByRole("radio", { name: t("setup.members.patrol.cronPresets.weekly") }),
    );
    await waitFor(() => expect(screen.getByDisplayValue("0 9 * * 1")).toBeInTheDocument());

    await user.click(
      screen.getByRole("radio", { name: t("setup.members.patrol.cronPresets.custom") }),
    );
    // The custom-cron input is seeded from the draft's stored cron string (the
    // original daily expression), independent of the preset-driven generated cron.
    const cronField = await screen.findByLabelText(t("setup.members.patrol.cron"));
    expect(cronField).toHaveValue("0 9 * * *");
  });

  it("blocks saving when a custom cron is invalid", async () => {
    const user = userEvent.setup();
    await openPatrolTab(user);

    await user.click(screen.getByRole("button", { name: t("setup.members.patrol.addSchedule") }));
    await user.click(
      await screen.findByRole("radio", { name: t("setup.members.patrol.cronPresets.custom") }),
    );

    const cronField = await screen.findByLabelText(t("setup.members.patrol.cron"));
    await user.clear(cronField);
    await user.type(cronField, "0 9 * *");

    expect(await screen.findByText(t("setup.members.patrol.cronInvalid"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("setup.members.saveButton") })).toBeDisabled();
  });
});

function teamIntelligenceConfig(overrides: Partial<IntelligenceConfig> = {}): IntelligenceConfig {
  return {
    config_dir: "/workspace/.guildbotics/config",
    person_id: null,
    inherited: false,
    model_mapping: { default: "models/openai.yml", openai: "models/openai.yml" },
    models: [
      {
        path: "models/openai.yml",
        provider: "openai",
        model_class: "OpenAIModel",
        model_id: "gpt-5",
      },
    ],
    cli_agent_mapping: { default: "codex-cli.yml", codex: "codex-cli.yml" },
    cli_agents: [
      {
        path: "codex-cli.yml",
        name: "codex",
        env: {},
        script: "codex",
        detected: true,
        detected_path: "/usr/local/bin/codex",
      },
    ],
    brain_mapping: [
      { name: "writer", brain_class: "WriterBrain", engine: "llm", target: "default" },
    ],
    ...overrides,
  };
}

function memberIntelligenceConfig(overrides: Partial<IntelligenceConfig> = {}): IntelligenceConfig {
  return teamIntelligenceConfig({
    person_id: "alice",
    inherited: false,
    model_mapping: {
      default: "models/openai.yml",
      openai: "models/openai.yml",
      gemini: "models/gemini.yml",
    },
    models: [
      {
        path: "models/openai.yml",
        provider: "openai",
        model_class: "OpenAIModel",
        model_id: "gpt-5",
      },
      {
        path: "models/gemini.yml",
        provider: "gemini",
        model_class: "GeminiModel",
        model_id: "gemini-2.5",
      },
    ],
    cli_agent_mapping: {
      default: "codex-cli.yml",
      codex: "codex-cli.yml",
      claude: "claude-cli.yml",
    },
    cli_agents: [
      {
        path: "codex-cli.yml",
        name: "codex",
        env: {},
        script: "codex",
        detected: true,
        detected_path: "/usr/local/bin/codex",
      },
      {
        path: "claude-cli.yml",
        name: "claude",
        env: {},
        script: "claude",
        detected: true,
        detected_path: "/usr/local/bin/claude",
      },
    ],
    brain_mapping: [],
    ...overrides,
  });
}

async function openTeamIntelligenceAdvanced(user: ReturnType<typeof userEvent.setup>) {
  renderSetupPage("/setup");
  // The intelligence section is reached through the sidebar nav (only the
  // members section is deep-linkable via the URL).
  await user.click(await screen.findByRole("button", { name: t("setup.nav.intelligence") }));
  await user.click(await screen.findByRole("button", { name: t("setup.intelligence.advanced") }));
  // The advanced editor loads its config lazily; wait for a unique label.
  await screen.findByText(t("setup.intelligence.modelMapping"));
}

async function openMemberIntelligenceTab(user: ReturnType<typeof userEvent.setup>) {
  renderSetupPage("/setup?section=members");
  await user.click(await screen.findByRole("button", { name: t("setup.members.editButton") }));
  await user.click(await screen.findByRole("tab", { name: t("setup.members.tabs.intelligence") }));
}

describe("IntelligenceEditor (team default)", () => {
  beforeEach(() => {
    vi.mocked(getIntelligenceConfig).mockResolvedValue(teamIntelligenceConfig());
    vi.mocked(getCliAgentDetections).mockResolvedValue({
      agents: [
        { name: "codex", executable: "codex", detected: true, path: "/usr/local/bin/codex" },
      ],
    });
  });

  it("autosaves a model definition edit through updateIntelligenceConfig after debounce", async () => {
    const user = userEvent.setup();
    await openTeamIntelligenceAdvanced(user);

    const modelClass = await screen.findByLabelText(t("setup.intelligence.modelClass"));
    await user.clear(modelClass);
    await user.type(modelClass, "CustomModel");

    await waitFor(() => expect(updateIntelligenceConfig).toHaveBeenCalledTimes(1), {
      timeout: 3000,
    });
    const body = vi.mocked(updateIntelligenceConfig).mock.calls[0][0];
    expect(body).toMatchObject({ person_id: null, inherit_team_defaults: false });
    expect(body.models?.[0]).toMatchObject({
      path: "models/openai.yml",
      model_class: "CustomModel",
    });
  });

  it("does not autosave repeatedly before the debounce elapses", async () => {
    const user = userEvent.setup();
    await openTeamIntelligenceAdvanced(user);

    const modelId = await screen.findByLabelText(t("setup.intelligence.modelId"));
    await user.type(modelId, "X");
    // The debounce is 800ms; right after typing nothing has been persisted yet.
    expect(updateIntelligenceConfig).not.toHaveBeenCalled();

    await waitFor(() => expect(updateIntelligenceConfig).toHaveBeenCalledTimes(1), {
      timeout: 3000,
    });
  });

  it("edits the brain feature-to-engine mapping and sends the new assignment", async () => {
    const user = userEvent.setup();
    await openTeamIntelligenceAdvanced(user);

    const engineSelect = await screen.findByRole("textbox", {
      name: t("setup.intelligence.engine"),
    });
    await user.click(engineSelect);
    await user.click(await screen.findByRole("option", { name: "CLI" }));

    await waitFor(() => expect(updateIntelligenceConfig).toHaveBeenCalledTimes(1), {
      timeout: 3000,
    });
    const body = vi.mocked(updateIntelligenceConfig).mock.calls[0][0];
    expect(body.brain_mapping?.[0]).toMatchObject({
      name: "writer",
      engine: "cli",
      target: "default",
    });
  });

  it("shows a JSON validation error and blocks autosave for malformed env", async () => {
    const user = userEvent.setup();
    await openTeamIntelligenceAdvanced(user);

    const envInput = await screen.findByLabelText(t("setup.intelligence.envJson"));
    await user.click(envInput);
    await user.paste("{not json");

    expect(await screen.findByText(t("setup.intelligence.envJsonError"))).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 1100));
    expect(updateIntelligenceConfig).not.toHaveBeenCalled();
  });

  it("renders the CLI agent detection badge from detections", async () => {
    const user = userEvent.setup();
    await openTeamIntelligenceAdvanced(user);

    await screen.findByText(t("setup.intelligence.cliDefinitions"));
    expect(screen.getAllByText(t("setup.intelligence.detected")).length).toBeGreaterThan(0);
  });

  it("surfaces a save error returned by updateIntelligenceConfig", async () => {
    const user = userEvent.setup();
    vi.mocked(updateIntelligenceConfig).mockRejectedValueOnce(new Error("write blew up"));
    await openTeamIntelligenceAdvanced(user);

    const modelClass = await screen.findByLabelText(t("setup.intelligence.modelClass"));
    await user.clear(modelClass);
    await user.type(modelClass, "BrokenModel");

    expect(await screen.findByText(t("setup.intelligence.saveAdvancedError"))).toBeInTheDocument();
    expect(screen.getByText("write blew up")).toBeInTheDocument();
  });
});

describe("IntelligenceEditor (member override)", () => {
  beforeEach(() => {
    // Two providers and two CLI agents are available so the override buttons are
    // enabled and selecting a non-default option is possible.
    vi.mocked(getProjectConfig).mockResolvedValue(
      projectConfig({
        description: "Demo project",
        has_openai_api_key: true,
        has_google_api_key: true,
      }),
    );
    vi.mocked(getCliAgentDetections).mockResolvedValue({
      agents: [
        { name: "codex", executable: "codex", detected: true, path: "/usr/local/bin/codex" },
        { name: "claude", executable: "claude", detected: true, path: "/usr/local/bin/claude" },
      ],
    });
    vi.mocked(getMemberConfig).mockResolvedValue(memberConfigDetail());
    vi.mocked(getIntelligenceConfig).mockResolvedValue(memberIntelligenceConfig());
  });

  it("registers an external save callback and persists the override on member save", async () => {
    const user = userEvent.setup();
    await openMemberIntelligenceTab(user);

    // Switch the member's default LLM provider to Gemini.
    await user.click(await screen.findByText(t("setup.intelligence.memberDefaultProvider")));
    const geminiButton = screen.getByText("Google Gemini").closest("button");
    if (!geminiButton) {
      throw new Error("Gemini override button not found");
    }
    await user.click(geminiButton);

    await user.click(screen.getByRole("button", { name: t("setup.members.saveButton") }));

    await waitFor(() => expect(updateIntelligenceConfig).toHaveBeenCalledTimes(1));
    const body = vi.mocked(updateIntelligenceConfig).mock.calls[0][0];
    expect(body).toMatchObject({ person_id: "alice", inherit_team_defaults: false });
    expect(body.model_mapping?.default).toBe("models/gemini.yml");
    // Member overrides do not resend full model/cli/brain definitions.
    expect("models" in body).toBe(false);
    expect("brain_mapping" in body).toBe(false);
  });

  it("edits the member default CLI agent override", async () => {
    const user = userEvent.setup();
    await openMemberIntelligenceTab(user);

    await screen.findByText(t("setup.intelligence.memberDefaultCliAgent"));
    const claudeButton = screen.getByText("Claude Code").closest("button");
    if (!claudeButton) {
      throw new Error("Claude override button not found");
    }
    await user.click(claudeButton);

    await user.click(screen.getByRole("button", { name: t("setup.members.saveButton") }));

    await waitFor(() => expect(updateIntelligenceConfig).toHaveBeenCalledTimes(1));
    const body = vi.mocked(updateIntelligenceConfig).mock.calls[0][0];
    expect(body.cli_agent_mapping?.default).toBe("claude-cli.yml");
  });

  it("sends inherit_team_defaults when the inherit switch is enabled", async () => {
    const user = userEvent.setup();
    await openMemberIntelligenceTab(user);

    await user.click(
      await screen.findByRole("switch", {
        name: t("setup.intelligence.inheritTeamDefaults"),
      }),
    );
    expect(await screen.findByText(t("setup.intelligence.inheritingTitle"))).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("setup.members.saveButton") }));

    await waitFor(() => expect(updateIntelligenceConfig).toHaveBeenCalledTimes(1));
    expect(vi.mocked(updateIntelligenceConfig).mock.calls[0][0]).toEqual({
      config_dir: "/workspace/.guildbotics/config",
      person_id: "alice",
      inherit_team_defaults: true,
    });
  });
});
