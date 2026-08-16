import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getWorkspaceSyncStatus,
  retryWorkspaceSync,
  type WorkspaceSyncStatus,
} from "../api/client";
import i18n from "../i18n";
import { SyncAlerts } from "./SyncAlerts";
import { SyncIndicator } from "./SyncIndicator";

const t = i18n.getFixedT("en");

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    getWorkspaceSyncStatus: vi.fn(),
    retryWorkspaceSync: vi.fn(),
  };
});

function status(overrides: Partial<WorkspaceSyncStatus> = {}): WorkspaceSyncStatus {
  return {
    enabled: true,
    workspace_id: "1f0a0000-0000-7000-8000-000000000001",
    device_id: "1f0a0000-0000-7000-8000-0000000000d1",
    hub_url: "user@hub:.guildbotics/hub/workspaces/w/repository.git",
    state: "idle",
    local_head: "abc",
    remote_head: "abc",
    ahead_count: 0,
    behind_count: 0,
    unsendable_changes: [],
    last_success_at: null,
    last_error_code: null,
    ...overrides,
  };
}

function renderWith(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MantineProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status());
});

describe("SyncIndicator", () => {
  it("shows nothing for a workspace that was never connected to a hub", async () => {
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status({ enabled: false }));
    renderWith(<SyncIndicator />);

    await waitFor(() => expect(getWorkspaceSyncStatus).toHaveBeenCalled());
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows the state as a short line", async () => {
    renderWith(<SyncIndicator />);

    expect(await screen.findByText(t("sync.state.synced.label"))).toBeInTheDocument();
  });

  it("opens the detail with the counts behind it", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(
      status({ ahead_count: 3, last_success_at: "2026-07-01T10:00:00+00:00" }),
    );
    renderWith(<SyncIndicator />);

    await user.click(await screen.findByRole("button", { name: /Waiting to send/ }));

    expect(await screen.findByText(t("sync.state.sending.detail"))).toBeInTheDocument();
    expect(screen.getByText(t("sync.counts.ahead", { count: 3 }))).toBeInTheDocument();
  });

  it("offers a retry only when the hub could not be reached", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status({ state: "unreachable" }));
    vi.mocked(retryWorkspaceSync).mockResolvedValue(status());
    renderWith(<SyncIndicator />);

    await user.click(await screen.findByRole("button", { name: /Hub unreachable/ }));
    await user.click(await screen.findByRole("button", { name: t("sync.actions.retry") }));

    expect(retryWorkspaceSync).toHaveBeenCalledTimes(1);
  });

  it("does not offer a retry for changes that would only be rejected again", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(
      status({ unsendable_changes: [{ path: "config/team/project.yml", reason: "too large" }] }),
    );
    renderWith(<SyncIndicator />);

    await user.click(await screen.findByRole("button", { name: /cannot be sent/ }));

    expect(await screen.findByText(t("sync.state.unsendable.detail"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("sync.actions.retry") })).not.toBeInTheDocument();
  });
});

describe("SyncAlerts", () => {
  it("stays out of the way while synchronization is working", async () => {
    renderWith(<SyncAlerts />);

    await waitFor(() => expect(getWorkspaceSyncStatus).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says nothing about changes merely queued", async () => {
    // Progress belongs to the sidebar; the band is for what needs a person.
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status({ ahead_count: 9 }));
    renderWith(<SyncAlerts />);

    await waitFor(() => expect(getWorkspaceSyncStatus).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("warns that the hub cannot be reached, with a way to try again", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status({ state: "unreachable" }));
    vi.mocked(retryWorkspaceSync).mockResolvedValue(status());
    renderWith(<SyncAlerts />);

    expect(await screen.findByText(t("sync.alerts.unreachable"))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("sync.actions.retry") }));

    expect(retryWorkspaceSync).toHaveBeenCalledTimes(1);
  });

  it("counts the changes that cannot be sent and links to the list", async () => {
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(
      status({
        unsendable_changes: [
          { path: "config/team/project.yml", reason: "too large" },
          { path: "state/devices/d1.json", reason: "not valid JSON" },
        ],
      }),
    );
    renderWith(<SyncAlerts />);

    expect(await screen.findByText(t("sync.alerts.unsendable", { count: 2 }))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: t("sync.actions.settings") })).toBeInTheDocument();
  });

  it("asks for an update when another machine wrote something newer", async () => {
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status({ state: "update_required" }));
    renderWith(<SyncAlerts />);

    expect(await screen.findByText(t("sync.alerts.update_required"))).toBeInTheDocument();
    // Retrying cannot make an old build read a newer record.
    expect(screen.queryByRole("button", { name: t("sync.actions.retry") })).not.toBeInTheDocument();
  });
});
