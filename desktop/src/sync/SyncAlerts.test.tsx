import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getWorkspaceSecrets,
  getWorkspaceSyncStatus,
  type WorkspaceSecrets,
  type WorkspaceSyncStatus,
} from "../api/client";
import i18n from "../i18n";
import { SyncAlerts } from "./SyncAlerts";

const t = i18n.getFixedT("en");

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    getWorkspaceSecrets: vi.fn(),
    getWorkspaceSyncStatus: vi.fn(),
    retryWorkspaceSync: vi.fn(),
  };
});

function secrets(overrides: Partial<WorkspaceSecrets> = {}): WorkspaceSecrets {
  return {
    enabled: true,
    hub_reachable: true,
    hub_error_code: "",
    secret_store: { available: true, locked: false },
    hub_secret_store: { available: true, locked: false },
    keys: [],
    sendable_keys: [],
    fetchable_keys: [],
    missing_count: 0,
    outdated_count: 0,
    pending_count: 0,
    attention_count: 0,
    ...overrides,
  };
}

function status(overrides: Partial<WorkspaceSyncStatus> = {}): WorkspaceSyncStatus {
  return {
    enabled: true,
    workspace_id: "1f0a0000-0000-7000-8000-00000000000a",
    device_id: "1f0a0000-0000-7000-8000-0000000000d1",
    hub_url: "user@hub:.guildbotics/hub/w.git",
    state: "idle",
    local_head: null,
    remote_head: null,
    ahead_count: 0,
    behind_count: 0,
    unsendable_changes: [],
    rejected_changes: [],
    last_success_at: null,
    last_error_code: null,
    live_error_code: null,
    ...overrides,
  };
}

const held = {
  rejection_id: "01a01500-0000-7000-8000-00000000000a",
  occurred_at: "2026-08-18T12:07:02Z",
  paths: ["config/team/project.yml"],
};

function renderAlerts() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MantineProvider>
        <MemoryRouter>
          <SyncAlerts />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getWorkspaceSecrets).mockResolvedValue(secrets());
});

describe("the synchronization warning band", () => {
  it("says nothing while there is nothing to say", async () => {
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status());
    renderAlerts();

    await waitFor(() => expect(getWorkspaceSyncStatus).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBe(null);
  });

  it("keeps saying a change of the user's was set aside, even while in sync", async () => {
    // The rejection is rare and costs the user an edit, so it is a state that
    // ends when they say they are done with it -- not an event that scrolls
    // past on a timeline while everything else reads as healthy.
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status({ rejected_changes: [held] }));
    renderAlerts();

    expect(await screen.findByText(t("sync.rejected.alertTitle"))).toBeInTheDocument();
    expect(screen.getByText(t("sync.rejected.alert", { count: 1 }))).toBeInTheDocument();
  });

  it("names credentials this machine has to act on, and where to act", async () => {
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status());
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({ missing_count: 2, attention_count: 2 }),
    );
    renderAlerts();

    expect(await screen.findByText(t("sync.secrets.title"))).toBeInTheDocument();
    expect(screen.getByText(t("sync.secrets.alert.attention", { count: 2 }))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: t("sync.secrets.hint.link") })).toBeInTheDocument();
  });

  it("says nothing about credentials while every machine is in step", async () => {
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status());
    renderAlerts();

    await waitFor(() => expect(getWorkspaceSecrets).toHaveBeenCalled());
    expect(screen.queryByText(t("sync.secrets.title"))).toBe(null);
  });

  it("shows an unreachable hub and a set aside change at the same time", async () => {
    // Neither says anything about the other: a hub that cannot be reached now
    // has no bearing on what it already refused.
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(
      status({ state: "unreachable", rejected_changes: [held] }),
    );
    renderAlerts();

    expect(await screen.findByText(t("sync.state.unreachable.label"))).toBeInTheDocument();
    expect(screen.getByText(t("sync.rejected.alertTitle"))).toBeInTheDocument();
  });
});
