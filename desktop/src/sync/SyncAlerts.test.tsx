import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getWorkspaceSecrets,
  getWorkspaceSyncStatus,
  type WorkspaceSecrets,
  type WorkspaceSyncStatus,
} from "../api/client";
import i18n from "../i18n";
import { SyncAlerts } from "./SyncAlerts";
import { TestMantineProvider } from "../test/TestMantineProvider";

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
    secret_store: { available: true, locked: false, error_code: "" },
    hub_secret_store: { available: true, locked: false, error_code: "" },
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
    last_error_detail: null,
    live_error_code: null,
    ...overrides,
  };
}

const held = {
  rejection_id: "01a01500-0000-7000-8000-00000000000a",
  occurred_at: "2026-08-18T12:07:02Z",
  paths: ["config/team/project.yml"],
};

function renderAlerts(initial?: WorkspaceSyncStatus) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (initial) client.setQueryData(["workspace-sync"], initial);
  const view = render(
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <MemoryRouter>
          <SyncAlerts />
        </MemoryRouter>
      </TestMantineProvider>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getWorkspaceSecrets).mockResolvedValue(secrets());
});

afterEach(() => vi.useRealTimers());

describe("member reads after synchronization", () => {
  it.each([
    ["an adopted head", { local_head: "adopted" }, true],
    ["a newly set aside change", { rejected_changes: [held] }, true],
    ["the same head with new progress", { last_success_at: "2026-10-03T00:00:00Z" }, false],
    ["a different workspace", { workspace_id: "another-workspace", local_head: "other" }, false],
  ] as const)("refreshes only when needed: %s", async (_label, next, changed) => {
    const initial = status({ local_head: "original" });
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(initial);
    const { client } = renderAlerts(initial);
    await waitFor(() => expect(client.getQueryData(["workspace-sync"])).toEqual(initial));
    const invalidate = vi.spyOn(client, "invalidateQueries");

    await act(async () => client.setQueryData(["workspace-sync"], { ...initial, ...next }));

    await waitFor(() =>
      expect(invalidate.mock.calls.map(([filter]) => filter?.queryKey)).toEqual(
        changed ? [["team"], ["member-config"]] : [],
      ),
    );
    invalidate.mockClear();
    await act(async () => client.setQueryData(["workspace-sync"], { ...initial, ...next }));
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("notices an adopted head on the next five-second poll", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(getWorkspaceSyncStatus)
      .mockResolvedValueOnce(status({ local_head: "original" }))
      .mockResolvedValue(status({ local_head: "adopted" }));
    const { client } = renderAlerts();
    await waitFor(() =>
      expect(client.getQueryData(["workspace-sync"])).toMatchObject({ local_head: "original" }),
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");

    await act(async () => vi.advanceTimersByTimeAsync(5000));

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["team"] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["member-config"] });
  });
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

  it("shows what the hub printed when synchronization fails", async () => {
    // The hub answers ssh but its own git cannot run: only its words tell the
    // user that the fix is on the hub machine.
    const printed =
      "You have not agreed to the Xcode license agreements.\nfatal: Could not read from remote repository.";
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(
      status({ state: "unreachable", last_error_detail: printed }),
    );
    renderAlerts();

    expect(await screen.findByText(t("sync.failureDetail"))).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) => element?.tagName === "PRE" && element.textContent === printed,
      ),
    ).toBeInTheDocument();
  });

  it("shows no error details when the failure left none", async () => {
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status({ state: "unreachable" }));
    renderAlerts();

    expect(await screen.findByText(t("sync.state.unreachable.label"))).toBeInTheDocument();
    expect(screen.queryByText(t("sync.failureDetail"))).toBe(null);
  });
});
