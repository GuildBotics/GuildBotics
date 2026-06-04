import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { SetupPage } from "./SetupPage";
import "../i18n";

vi.mock("../api/backend", () => ({
  restartBackend: vi.fn(async () => undefined),
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
    getRoleOptions: vi.fn(async () => ({
      roles: [{ role_id: "professional", summary: "Professional", description: "" }],
    })),
    getTeam: vi.fn(async () => ({
      project: { name: "Demo", language_code: "en", language_name: "English" },
      members: [{ person_id: "alice", name: "Alice", is_active: true, roles: ["professional"] }],
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

describe("SetupPage", () => {
  it("allows sidebar navigation after opening a members deep link", async () => {
    const user = userEvent.setup();
    renderSetupPage("/setup?section=members&tab=patrol");

    expect(await screen.findByText("Alice (alice)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Project" }));

    expect(await screen.findByLabelText("Working directory")).toBeInTheDocument();
    expect(screen.queryByText("Alice (alice)")).not.toBeInTheDocument();
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
    person_type: "",
    is_active: true,
    github_username: "",
    git_email: "",
    roles: ["professional"],
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
    has_slack_bot_token: false,
    has_slack_app_token: false,
    slack_channels: [],
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
