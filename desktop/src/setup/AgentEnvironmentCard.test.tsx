import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildAgentEnvironment,
  getAgentEnvironmentStatus,
  type AgentEnvironmentStatusResponse,
} from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { AgentEnvironmentCard } from "./AgentEnvironmentCard";

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  getAgentEnvironmentStatus: vi.fn(),
  buildAgentEnvironment: vi.fn(),
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
        logged_in: true,
        problem: "",
      },
      {
        name: "claude",
        label: "Claude Code",
        config_reference: "cli_agents/claude/default.yml",
        provisioned: true,
        logged_in: false,
        problem: "Claude Code is not logged in on this device.",
      },
      {
        name: "grok",
        label: "Grok Build",
        config_reference: "cli_agents/grok/default.yml",
        provisioned: false,
        logged_in: false,
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
  return render(
    <MantineProvider env="test">
      <QueryClientProvider client={client}>
        <AgentEnvironmentCard focusElement={focusElement} />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

describe("AgentEnvironmentCard", () => {
  beforeEach(() => {
    vi.mocked(getAgentEnvironmentStatus).mockReset();
    vi.mocked(buildAgentEnvironment).mockReset();
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
    expect(screen.getByText(t("setup.intelligence.environment.toolLoggedIn"))).toBeInTheDocument();
    expect(
      screen.getByText(t("setup.intelligence.environment.toolNotLoggedIn")),
    ).toBeInTheDocument();
    expect(screen.getByText("guildbotics environment login claude")).toBeInTheDocument();
    expect(
      screen.getByText(t("setup.intelligence.environment.toolNotProvisioned")),
    ).toBeInTheDocument();
    // Nothing to build when the snapshot is ready.
    expect(
      screen.queryByRole("button", { name: t("setup.intelligence.environment.build") }),
    ).not.toBeInTheDocument();
  });

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

    await screen.findByText("guildbotics environment login claude");
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    expect(document.getElementById("agent-environment-tool-claude")).not.toBeNull();
  });
});
