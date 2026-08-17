import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiRequestError,
  changeWorkspaceSyncHub,
  createDeviceSshKey,
  createHub,
  enableWorkspaceSync,
  getConfigStatus,
  getDeviceSshKey,
  getHubStatus,
  getWorkspaceDevices,
  getWorkspaceSyncStatus,
  inspectHub,
  previewWorkspaceSync,
  renameThisDevice,
  trustHub,
  type ConfigStatus,
  type HubConnection,
  type WorkspaceSyncPreview,
  type WorkspaceSyncStatus,
} from "../api/client";
import i18n from "../i18n";
import { SyncSettings } from "./SyncSettings";

const t = i18n.getFixedT("en");

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    changeWorkspaceSyncHub: vi.fn(),
    createDeviceSshKey: vi.fn(),
    createHub: vi.fn(),
    enableWorkspaceSync: vi.fn(),
    getConfigStatus: vi.fn(),
    getDeviceSshKey: vi.fn(),
    getHubStatus: vi.fn(),
    getWorkspaceDevices: vi.fn(),
    getWorkspaceSyncStatus: vi.fn(),
    inspectHub: vi.fn(),
    previewWorkspaceSync: vi.fn(),
    renameThisDevice: vi.fn(),
    trustHub: vi.fn(),
  };
});

function status(overrides: Partial<WorkspaceSyncStatus> = {}): WorkspaceSyncStatus {
  return {
    enabled: false,
    workspace_id: null,
    device_id: "1f0a0000-0000-7000-8000-0000000000d1",
    hub_url: null,
    state: "disabled",
    local_head: null,
    remote_head: null,
    ahead_count: 0,
    behind_count: 0,
    unsendable_changes: [],
    last_success_at: null,
    last_error_code: null,
    ...overrides,
  };
}

function configStatus(overrides: Partial<ConfigStatus> = {}): ConfigStatus {
  return {
    cwd: "/home/u",
    workspace: "/home/u/workspace",
    config_dir: "/home/u/workspace/.guildbotics/config",
    project_file: "/home/u/workspace/.guildbotics/config/team/project.yml",
    project_file_exists: true,
    storage_dir: "/home/u/workspace",
    ...overrides,
  };
}

function connection(overrides: Partial<HubConnection> = {}): HubConnection {
  return {
    endpoint: "user@hub.local",
    is_local: false,
    host_key_fingerprints: [],
    host_key_trusted: true,
    host_key_changed: false,
    workspace_ids: [],
    ...overrides,
  };
}

function preview(overrides: Partial<WorkspaceSyncPreview> = {}): WorkspaceSyncPreview {
  return {
    hub_workspace_id: "1f0a0000-0000-7000-8000-00000000000a",
    workspace_id: "1f0a0000-0000-7000-8000-00000000000b",
    mode: "join",
    hub_only: [],
    device_only: [],
    differing: [],
    unsendable_changes: [],
    ...overrides,
  };
}

function renderSettings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MantineProvider>
        <MemoryRouter>
          <SyncSettings />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

async function lookUpHub(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText(t("sync.connect.endpoint")), "user@hub.local");
  await user.click(screen.getByRole("button", { name: t("sync.connect.inspect") }));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getConfigStatus).mockResolvedValue(configStatus());
  vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(status());
  vi.mocked(getWorkspaceDevices).mockResolvedValue({ devices: [], device_id: "d1" });
  vi.mocked(getDeviceSshKey).mockResolvedValue({
    exists: false,
    path: null,
    public_key: "",
    fingerprint: "",
  });
  vi.mocked(getHubStatus).mockResolvedValue({
    hosted: false,
    hub_root: "/home/u/.guildbotics/hub",
    hub_id: null,
    created_at: null,
    ssh_endpoint: null,
    workspace_ids: [],
  });
});

describe("confirming the hub's host key", () => {
  it("asks the user to match a fingerprint before anything is sent", async () => {
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(
      connection({ host_key_trusted: false, host_key_fingerprints: ["SHA256:aaa", "SHA256:bbb"] }),
    );
    renderSettings();

    await lookUpHub(user);

    expect(await screen.findByText(t("sync.hostKey.title"))).toBeInTheDocument();
    expect(screen.getByText("SHA256:aaa")).toBeInTheDocument();
    // The workspace list is withheld until the key is confirmed.
    expect(
      screen.queryByRole("button", { name: t("sync.connect.register") }),
    ).not.toBeInTheDocument();
  });

  it("sends back the fingerprint the user actually selected", async () => {
    // Not a bare "confirmed" flag: the machine must not be able to answer with
    // a different key than the one that was read.
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(
      connection({ host_key_trusted: false, host_key_fingerprints: ["SHA256:aaa", "SHA256:bbb"] }),
    );
    vi.mocked(trustHub).mockResolvedValue(connection());
    renderSettings();
    await lookUpHub(user);

    const buttons = await screen.findAllByRole("button", { name: t("sync.hostKey.confirm") });
    await user.click(buttons[1]);

    expect(trustHub).toHaveBeenCalledWith({
      endpoint: "user@hub.local",
      fingerprint: "SHA256:bbb",
    });
  });

  it("says a stored key was replaced rather than presenting a first contact", async () => {
    // A rebuilt hub machine and an impostor look identical from here, so the
    // screen says which situation it is in and leaves the judgement to the user.
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(
      connection({
        host_key_trusted: false,
        host_key_changed: true,
        host_key_fingerprints: ["SHA256:aaa"],
      }),
    );
    renderSettings();

    await lookUpHub(user);

    expect(await screen.findByText(t("sync.hostKey.changedTitle"))).toBeInTheDocument();
    expect(screen.queryByText(t("sync.hostKey.title"))).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("sync.hostKey.confirm") })).toBeInTheDocument();
  });

  it("asks the user to look again when the hub offers a different key", async () => {
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(
      connection({ host_key_trusted: false, host_key_fingerprints: ["SHA256:aaa"] }),
    );
    vi.mocked(trustHub).mockRejectedValue(
      new ApiRequestError({
        code: "host_key_changed",
        message: "hub.local offered a different host key than the one confirmed.",
        context: {},
      }),
    );
    renderSettings();
    await lookUpHub(user);
    await user.click(await screen.findByRole("button", { name: t("sync.hostKey.confirm") }));

    expect(await screen.findByText(/different host key/)).toBeInTheDocument();
  });
});

describe("saying where the hub is", () => {
  it("cannot look up a hub before an address is given", async () => {
    // An empty field means the user has not said where the hub is. Reading it
    // as "this machine" carried them to a register button that a machine
    // hosting no hub could only fail at -- after minting a workspace id.
    renderSettings();

    expect(await screen.findByLabelText(t("sync.connect.endpoint"))).toHaveValue("");
    expect(screen.getByRole("button", { name: t("sync.connect.inspect") })).toBeDisabled();
    expect(inspectHub).not.toHaveBeenCalled();
  });

  it("offers the hub on this machine as its own choice, only when there is one", async () => {
    const user = userEvent.setup();
    vi.mocked(getHubStatus).mockResolvedValue({
      hosted: true,
      hub_root: "/home/u/.guildbotics/hub",
      hub_id: "hub-1",
      created_at: "2026-07-01T00:00:00+00:00",
      ssh_endpoint: "user@hub.local",
      workspace_ids: [],
    });
    vi.mocked(inspectHub).mockResolvedValue(connection({ endpoint: "", is_local: true }));
    renderSettings();

    await user.click(await screen.findByRole("button", { name: t("sync.connect.useLocal") }));

    expect(inspectHub).toHaveBeenCalledWith({ endpoint: "" });
    expect(
      await screen.findByRole("button", { name: t("sync.connect.register") }),
    ).toBeInTheDocument();
  });

  it("does not offer this machine when it hosts no hub", async () => {
    renderSettings();

    expect(await screen.findByLabelText(t("sync.connect.endpoint"))).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("sync.connect.useLocal") }),
    ).not.toBeInTheDocument();
  });
});

describe("choosing what to do with this workspace", () => {
  it("registers without previewing, because there is nothing to compare against", async () => {
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(connection());
    vi.mocked(enableWorkspaceSync).mockResolvedValue(status({ enabled: true, state: "idle" }));
    renderSettings();
    await lookUpHub(user);

    await user.click(await screen.findByRole("button", { name: t("sync.connect.register") }));

    expect(previewWorkspaceSync).not.toHaveBeenCalled();
    expect(enableWorkspaceSync).toHaveBeenCalledWith({
      hub: { endpoint: "user@hub.local" },
      workspace_id: "",
    });
  });

  it("shows what joining would change before joining", async () => {
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(
      connection({ workspace_ids: ["1f0a0000-0000-7000-8000-00000000000a"] }),
    );
    vi.mocked(previewWorkspaceSync).mockResolvedValue(
      preview({
        differing: ["config/team/project.yml"],
        hub_only: ["config/team/members/bob/person.yml"],
        device_only: ["state/devices/d1.json"],
      }),
    );
    renderSettings();
    await lookUpHub(user);

    await user.click(await screen.findByRole("button", { name: t("sync.connect.join") }));

    expect(await screen.findByText(t("sync.preview.title"))).toBeInTheDocument();
    expect(screen.getByText(t("sync.preview.mode.join"))).toBeInTheDocument();
    expect(screen.getByText("config/team/project.yml")).toBeInTheDocument();
    expect(screen.getByText("state/devices/d1.json")).toBeInTheDocument();
    // Nothing has happened yet.
    expect(enableWorkspaceSync).not.toHaveBeenCalled();
  });

  it("joins only after the preview is confirmed", async () => {
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(
      connection({ workspace_ids: ["1f0a0000-0000-7000-8000-00000000000a"] }),
    );
    vi.mocked(previewWorkspaceSync).mockResolvedValue(preview());
    vi.mocked(enableWorkspaceSync).mockResolvedValue(status({ enabled: true, state: "idle" }));
    renderSettings();
    await lookUpHub(user);
    await user.click(await screen.findByRole("button", { name: t("sync.connect.join") }));

    await user.click(await screen.findByRole("button", { name: t("sync.preview.confirm") }));

    expect(enableWorkspaceSync).toHaveBeenCalledWith({
      hub: { endpoint: "user@hub.local" },
      workspace_id: "1f0a0000-0000-7000-8000-00000000000a",
    });
  });

  it("does nothing when the preview is cancelled", async () => {
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(
      connection({ workspace_ids: ["1f0a0000-0000-7000-8000-00000000000a"] }),
    );
    vi.mocked(previewWorkspaceSync).mockResolvedValue(preview());
    renderSettings();
    await lookUpHub(user);
    await user.click(await screen.findByRole("button", { name: t("sync.connect.join") }));

    await user.click(await screen.findByRole("button", { name: t("sync.preview.cancel") }));

    expect(enableWorkspaceSync).not.toHaveBeenCalled();
  });

  it("says the hub is busy and can be tried again in a moment", async () => {
    const user = userEvent.setup();
    vi.mocked(inspectHub).mockResolvedValue(connection());
    vi.mocked(enableWorkspaceSync).mockRejectedValue(
      new ApiRequestError({
        code: "workspace_sync_busy",
        message: "Synchronization is still finishing its last cycle. Try again in a moment.",
        context: {},
      }),
    );
    renderSettings();
    await lookUpHub(user);

    await user.click(await screen.findByRole("button", { name: t("sync.connect.register") }));

    expect(await screen.findByText(/Try again in a moment/)).toBeInTheDocument();
  });
});

describe("once connected", () => {
  it("offers reconnecting to a rebuilt hub", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(
      status({ enabled: true, state: "idle", hub_url: "user@hub:.guildbotics/hub/w.git" }),
    );
    vi.mocked(inspectHub).mockResolvedValue(connection());
    vi.mocked(changeWorkspaceSyncHub).mockResolvedValue(status({ enabled: true }));
    renderSettings();

    await user.click(await screen.findByRole("button", { name: t("sync.connected.change") }));
    await lookUpHub(user);
    await user.click(await screen.findByRole("button", { name: t("sync.connect.register") }));

    expect(changeWorkspaceSyncHub).toHaveBeenCalledTimes(1);
    expect(enableWorkspaceSync).not.toHaveBeenCalled();
  });

  it("lists what cannot be sent, with the reason for each", async () => {
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(
      status({
        enabled: true,
        state: "idle",
        unsendable_changes: [
          { path: "config/team/project.yml", reason: "is larger than the shared limit" },
        ],
      }),
    );
    renderSettings();

    expect(await screen.findByText(t("sync.unsendable.title"))).toBeInTheDocument();
    expect(screen.getByText("config/team/project.yml")).toBeInTheDocument();
    expect(screen.getByText("is larger than the shared limit")).toBeInTheDocument();
  });
});

describe("devices", () => {
  it("marks which row is this machine and offers to rename only that one", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceDevices).mockResolvedValue({
      device_id: "d1",
      devices: [
        {
          device_id: "d1",
          display_name: "mac-studio",
          os: "macos",
          joined_at: "2026-07-01T00:00:00+00:00",
          status: "active",
          ssh_public_key_fingerprint: "",
          is_self: true,
        },
        {
          device_id: "d2",
          display_name: "win-desktop",
          os: "windows",
          joined_at: "2026-07-02T00:00:00+00:00",
          status: "active",
          ssh_public_key_fingerprint: "",
          is_self: false,
        },
      ],
    });
    vi.mocked(renameThisDevice).mockResolvedValue({ device_id: "d1", devices: [] });
    renderSettings();

    expect(await screen.findByText("win-desktop")).toBeInTheDocument();
    expect(screen.getByText(t("sync.devices.self"))).toBeInTheDocument();

    const nameField = screen.getByLabelText(t("sync.devices.rename"));
    expect(nameField).toHaveValue("mac-studio");
    await user.clear(nameField);
    await user.type(nameField, "Work laptop");
    await user.click(screen.getByRole("button", { name: t("sync.devices.renameAction") }));

    expect(renameThisDevice).toHaveBeenCalledWith("Work laptop");
  });
});

describe("this device's SSH key", () => {
  it("creates the key on request and shows it to copy", async () => {
    const user = userEvent.setup();
    vi.mocked(createDeviceSshKey).mockResolvedValue({
      exists: true,
      path: "/home/u/.ssh/id_ed25519",
      public_key: "ssh-ed25519 AAAAC3Nz user@mac",
      fingerprint: "SHA256:key",
    });
    renderSettings();

    await user.click(await screen.findByRole("button", { name: t("sync.sshKey.create") }));

    expect(await screen.findByText("ssh-ed25519 AAAAC3Nz user@mac")).toBeInTheDocument();
  });
});

describe("hosting the hub here", () => {
  it("can be set up before this machine has a workspace", async () => {
    // The machine that hosts the hub is often the one that never gets a
    // workspace of its own, so hosting must not wait for one. Only the parts
    // that need something to share step aside.
    const user = userEvent.setup();
    vi.mocked(getConfigStatus).mockResolvedValue(configStatus({ workspace: null }));
    vi.mocked(createHub).mockResolvedValue({
      hosted: true,
      hub_root: "/home/u/.guildbotics/hub",
      hub_id: "hub-1",
      created_at: "2026-07-01T00:00:00+00:00",
      ssh_endpoint: "user@hub.local",
      workspace_ids: [],
    });
    renderSettings();

    expect(await screen.findByText(t("sync.settings.noWorkspaceTitle"))).toBeInTheDocument();
    expect(screen.queryByLabelText(t("sync.connect.endpoint"))).not.toBeInTheDocument();
    expect(screen.queryByText(t("sync.devices.title"))).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("sync.host.create") }));

    await waitFor(() =>
      expect(screen.getByText(t("sync.host.hosted", { count: 0 }))).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: t("sync.sshKey.create") })).toBeInTheDocument();
  });

  it("reports what this machine holds once it is a hub", async () => {
    const user = userEvent.setup();
    vi.mocked(createHub).mockResolvedValue({
      hosted: true,
      hub_root: "/home/u/.guildbotics/hub",
      hub_id: "hub-1",
      created_at: "2026-07-01T00:00:00+00:00",
      ssh_endpoint: "user@hub.local",
      workspace_ids: ["1f0a0000-0000-7000-8000-00000000000a"],
    });
    renderSettings();

    await user.click(await screen.findByRole("button", { name: t("sync.host.create") }));

    await waitFor(() =>
      expect(screen.getByText(t("sync.host.hosted", { count: 1 }))).toBeInTheDocument(),
    );
  });
});
