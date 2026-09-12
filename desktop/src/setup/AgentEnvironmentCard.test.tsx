import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildAgentEnvironment,
  getAgentEnvironmentStatus,
  recheckCliAgentUsage,
  type CliAgentUsagesResponse,
  type AgentEnvironmentStatusResponse,
} from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { AgentEnvironmentCard } from "./AgentEnvironmentCard";

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  getAgentEnvironmentStatus: vi.fn(),
  buildAgentEnvironment: vi.fn(),
  recheckCliAgentUsage: vi.fn(),
}));

const t = i18n.getFixedT("en");

function status(
  overrides: Partial<AgentEnvironmentStatusResponse> = {},
): AgentEnvironmentStatusResponse {
  return {
    platform: "darwin",
    runtime: {
      available: true,
      reason: "",
      version: "0.6.17",
      home: "/Users/me/.guildbotics/data/msb",
    },
    snapshot: { state: "ready", name: "guildbotics-abc", detail: "", output: [] },
    dns: { declared: "host", nameservers: ["192.168.3.1"], problem: "" },
    tools: [
      {
        name: "codex",
        label: "Codex",
        config_reference: "cli_agents/codex/default.yml",
        provisioned: true,
        credentials_saved: true,
        authentication_failed: false,
        usage_supported: false,
        usage_check: null,
        login_command: "/Users/me/.guildbotics/bin/guildbotics environment login codex",
        problem: "",
      },
      {
        name: "claude",
        label: "Claude Code",
        config_reference: "cli_agents/claude/default.yml",
        provisioned: true,
        credentials_saved: false,
        authentication_failed: false,
        usage_supported: false,
        usage_check: null,
        login_command: "/Users/me/.guildbotics/bin/guildbotics environment login claude",
        problem: "No credentials are saved for Claude Code on this device.",
      },
      {
        name: "grok",
        label: "Grok Build",
        config_reference: "cli_agents/grok/default.yml",
        provisioned: false,
        credentials_saved: false,
        authentication_failed: false,
        usage_supported: false,
        usage_check: null,
        login_command: "/Users/me/.guildbotics/bin/guildbotics environment login grok",
        problem: "Grok Build is not provisioned in the agent environment yet.",
      },
    ],
    problem: "",
    problem_setting: "",
    access: { documents: [], paths: [], denied: [], problem: "" },
    members: [],
    ...overrides,
  };
}

function renderCard(focusElement?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <MantineProvider env="test">
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <AgentEnvironmentCard focusElement={focusElement} />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { ...rendered, client };
}

describe("AgentEnvironmentCard", () => {
  beforeEach(() => {
    vi.mocked(getAgentEnvironmentStatus).mockReset();
    vi.mocked(buildAgentEnvironment).mockReset();
    vi.mocked(recheckCliAgentUsage).mockReset();
  });

  it.each(["en", "ja"])(
    "shows a failed usage check and verifies recovery in %s",
    async (language) => {
      await i18n.changeLanguage(language);
      const tr = i18n.getFixedT(language);
      const user = userEvent.setup();
      const data = status();
      const check = {
        status: "failed" as const,
        checked_at: "2026-09-12T12:00:00Z",
        trace_id: "system:usage-test",
      };
      data.tools[0] = {
        ...data.tools[0],
        usage_supported: true,
        usage_check: check,
        problem: "Usage retrieval failed.",
      };
      vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(data);
      let finish!: (value: CliAgentUsagesResponse) => void;
      vi.mocked(recheckCliAgentUsage).mockImplementation(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      const { client } = renderCard("agent-environment-tool-codex");
      await screen.findByText(tr("setup.intelligence.environment.usageFailed"));
      const row = within(document.getElementById("agent-environment-tool-codex")!);
      expect(
        row.getByText(tr("setup.intelligence.environment.toolCredentialsSaved")),
      ).toBeVisible();
      expect(row.getByText("Usage retrieval failed.")).toBeVisible();
      expect(
        row.getByRole("link", { name: tr("setup.intelligence.environment.errorDetails") }),
      ).toHaveAttribute("href", "/diagnostics?tab=executions&trace_id=system%3Ausage-test");
      expect(
        row.getByText(
          tr("setup.intelligence.environment.lastChecked", {
            time: new Date(check.checked_at).toLocaleString(language),
          }),
        ),
      ).toBeVisible();
      // Rereading saved state must not be presented as a new provider check.
      await user.click(
        screen.getByRole("button", { name: tr("setup.intelligence.environment.refresh") }),
      );
      expect(recheckCliAgentUsage).not.toHaveBeenCalled();
      await user.click(
        row.getByRole("button", { name: tr("setup.intelligence.environment.recheck") }),
      );
      expect(recheckCliAgentUsage).toHaveBeenCalledWith("codex");
      expect(row.getByText(tr("setup.intelligence.environment.usageChecking"))).toBeVisible();
      expect(
        row.getByRole("button", { name: tr("setup.intelligence.environment.recheck") }),
      ).toBeDisabled();
      const recovered = status({
        tools: [
          {
            ...data.tools[0],
            problem: "",
            usage_check: { ...check, status: "succeeded", checked_at: "2026-09-12T12:01:00Z" },
          },
        ],
      });
      vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(recovered);
      const invalidate = vi.spyOn(client, "invalidateQueries");
      await act(async () => finish({ usages: [] }));
      await screen.findByText(tr("setup.intelligence.environment.usageSucceeded"));
      expect(screen.queryByText("Usage retrieval failed.")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("link", { name: tr("setup.intelligence.environment.errorDetails") }),
      ).not.toBeInTheDocument();
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["system-alerts"] });
      await i18n.changeLanguage("en");
    },
  );

  it("keeps the previous result visible when a recheck request fails", async () => {
    const data = status();
    data.tools[0] = {
      ...data.tools[0],
      usage_supported: true,
      usage_check: {
        status: "failed",
        checked_at: "2026-09-12T12:00:00Z",
        trace_id: "system:failed",
      },
      problem: "Previous failure",
    };
    vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(data);
    vi.mocked(recheckCliAgentUsage).mockRejectedValue(new Error("offline"));
    renderCard();
    await userEvent.click(
      await screen.findByRole("button", { name: t("setup.intelligence.environment.recheck") }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      t("setup.intelligence.environment.recheckError"),
    );
    expect(screen.getByText("Previous failure")).toBeVisible();
  });

  it("shows the runtime, the snapshot, the resolvers, and each tool's login on this device", async () => {
    vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(status());
    renderCard();

    expect(
      await screen.findByText(
        t("setup.intelligence.environment.runtimeAvailable", { version: "0.6.17" }),
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("setup.intelligence.environment.snapshotStates.ready")),
    ).toBeInTheDocument();
    expect(screen.getByText("guildbotics-abc")).toBeInTheDocument();
    expect(screen.getByText(t("setup.intelligence.environment.dnsHost"))).toBeInTheDocument();
    expect(screen.getByText(/192\.168\.3\.1/)).toBeInTheDocument();
    // The catalog's three answers, on the device's layer: logged in here, not
    // logged in here (with the command that does it), not provisioned at all.
    expect(
      screen.getByText(t("setup.intelligence.environment.toolCredentialsSaved")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("setup.intelligence.environment.toolCredentialsMissing")),
    ).toBeInTheDocument();
    expect(
      screen.getByText("/Users/me/.guildbotics/bin/guildbotics environment login claude"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("setup.intelligence.environment.toolNotProvisioned")),
    ).toBeInTheDocument();
    // Nothing to build when the snapshot is ready.
    expect(
      screen.queryByRole("button", { name: t("setup.intelligence.environment.build") }),
    ).not.toBeInTheDocument();
  });

  it.each(["en", "ja"])(
    "keeps login and copy available for every credential state in %s",
    async (language) => {
      await i18n.changeLanguage(language);
      const tr = i18n.getFixedT(language);
      const user = userEvent.setup();
      const data = status();
      data.tools = [
        data.tools[0],
        data.tools[1],
        {
          ...data.tools[0],
          name: "gemini",
          label: "Gemini",
          authentication_failed: true,
          usage_supported: false,
          usage_check: null,
          login_command: "/Users/me/.guildbotics/bin/guildbotics environment login gemini",
        },
      ];
      vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(data);
      renderCard();
      await screen.findByText(tr("setup.intelligence.environment.toolAuthenticationFailed"));
      for (const tool of data.tools) {
        const row = within(document.getElementById(`agent-environment-tool-${tool.name}`)!);
        expect(row.getByText(tool.login_command)).toBeInTheDocument();
        await user.click(
          row.getByRole("button", { name: tr("setup.intelligence.environment.copy") }),
        );
        expect(await navigator.clipboard.readText()).toBe(tool.login_command);
      }
      const recovered = {
        ...data,
        tools: data.tools.map((tool) => ({ ...tool, authentication_failed: false })),
      };
      vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(recovered);
      await user.click(
        screen.getByRole("button", { name: tr("setup.intelligence.environment.refresh") }),
      );
      await waitFor(() =>
        expect(
          screen.queryByText(tr("setup.intelligence.environment.toolAuthenticationFailed")),
        ).not.toBeInTheDocument(),
      );
      await i18n.changeLanguage("en");
    },
  );

  it("offers a build for a missing snapshot and shows the build's output while it runs", async () => {
    vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(
      status({ snapshot: { state: "missing", name: "guildbotics-abc", detail: "", output: [] } }),
    );
    vi.mocked(buildAgentEnvironment).mockResolvedValue(
      status({
        snapshot: {
          state: "building",
          name: "guildbotics-abc",
          detail: "",
          output: ["[home]", "[npm]"],
        },
      }),
    );
    renderCard();

    await userEvent.click(
      await screen.findByRole("button", { name: t("setup.intelligence.environment.build") }),
    );

    expect(buildAgentEnvironment).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByText(t("setup.intelligence.environment.snapshotStates.building")),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText(t("setup.intelligence.environment.buildOutput")),
    ).toHaveTextContent("[home] [npm]");
    // While building there is nothing to press.
    expect(
      screen.queryByRole("button", { name: t("setup.intelligence.environment.build") }),
    ).not.toBeInTheDocument();
  });

  it("shows why a build failed, with its output, and offers to build again", async () => {
    vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(
      status({
        snapshot: {
          state: "failed",
          name: "guildbotics-abc",
          detail: "Build step 'apt' failed with exit code 100.",
          output: ["[apt]", "E: Unable to locate package nope"],
        },
      }),
    );
    renderCard();

    expect(
      await screen.findByText(t("setup.intelligence.environment.snapshotStates.failed")),
    ).toBeInTheDocument();
    expect(screen.getByText("Build step 'apt' failed with exit code 100.")).toBeInTheDocument();
    expect(
      screen.getByLabelText(t("setup.intelligence.environment.buildOutput")),
    ).toHaveTextContent("E: Unable to locate package nope");
    expect(
      screen.getByRole("button", { name: t("setup.intelligence.environment.build") }),
    ).toBeEnabled();
  });

  it("explains an unavailable runtime instead of offering a build", async () => {
    vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(
      status({
        runtime: {
          available: false,
          reason: "The microsandbox runtime is not installed.",
          version: "",
          home: "",
        },
        snapshot: { state: "missing", name: "guildbotics-abc", detail: "", output: [] },
        problem: "The microsandbox runtime is not installed.",
        problem_setting: "runtime",
      }),
    );
    renderCard();

    expect(
      await screen.findByText(t("setup.intelligence.environment.runtimeUnavailable")),
    ).toBeInTheDocument();
    expect(screen.getByText("The microsandbox runtime is not installed.")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("setup.intelligence.environment.build") }),
    ).not.toBeInTheDocument();
  });

  it("brings the row an alert asked for into view once the status is loaded", async () => {
    vi.mocked(getAgentEnvironmentStatus).mockResolvedValue(status());
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    renderCard("agent-environment-tool-claude");

    await screen.findByText("/Users/me/.guildbotics/bin/guildbotics environment login claude");
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    expect(document.getElementById("agent-environment-tool-claude")).not.toBeNull();
  });
});
