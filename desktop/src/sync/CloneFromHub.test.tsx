import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  cloneWorkspaceFromHub,
  getHubStatus,
  getWorkspaceSyncStatus,
  inspectHub,
  type ConfigStatus,
  type WorkspaceSyncStatus,
} from "../api/client";
import i18n from "../i18n";
import { CloneFromHubButton } from "./CloneFromHub";

const t = i18n.getFixedT("en");

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    cloneWorkspaceFromHub: vi.fn(),
    getHubStatus: vi.fn(),
    getWorkspaceSyncStatus: vi.fn(),
    inspectHub: vi.fn(),
    trustHub: vi.fn(),
  };
});

function syncStatus(workspaceId: string | null): WorkspaceSyncStatus {
  return {
    enabled: false,
    workspace_id: workspaceId,
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
  };
}

const CLONED: ConfigStatus = {
  cwd: "/tmp",
  workspace: "/tmp/second",
  config_dir: "/tmp/second/.guildbotics/config",
  project_file: "/tmp/second/.guildbotics/config/team/project.yml",
  project_file_exists: true,
  storage_dir: null,
};

function renderButton(onCloned = vi.fn(), destination = "/tmp/second") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MantineProvider>
        <CloneFromHubButton destination={destination} onCloned={onCloned} />
      </MantineProvider>
    </QueryClientProvider>,
  );
  return onCloned;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(syncStatus(null));
  vi.mocked(getHubStatus).mockResolvedValue({
    hosted: false,
    hub_root: "/home/u/.guildbotics/hub",
    hub_id: null,
    created_at: null,
    ssh_endpoint: null,
    workspace_ids: [],
  });
  vi.mocked(inspectHub).mockResolvedValue({
    endpoint: "user@hub.local",
    is_local: false,
    host_key_fingerprints: [],
    host_key_trusted: true,
    host_key_changed: false,
    workspace_ids: ["1f0a0000-0000-7000-8000-00000000000a"],
  });
});

describe("taking a copy from a hub", () => {
  it("cannot start without somewhere to put it", () => {
    renderButton(vi.fn(), "  ");

    expect(screen.getByRole("button", { name: t("sync.clone.action") })).toBeDisabled();
  });

  it("creates the copy in the chosen directory and hands back the new workspace", async () => {
    const user = userEvent.setup();
    const onCloned = renderButton();
    vi.mocked(cloneWorkspaceFromHub).mockResolvedValue(CLONED);

    await user.click(screen.getByRole("button", { name: t("sync.clone.action") }));
    await user.type(await screen.findByLabelText(t("sync.connect.endpoint")), "user@hub.local");
    await user.click(screen.getByRole("button", { name: t("sync.connect.inspect") }));
    await user.click(await screen.findByRole("button", { name: t("sync.clone.take") }));

    expect(cloneWorkspaceFromHub).toHaveBeenCalledWith({
      hub: { endpoint: "user@hub.local" },
      workspace_id: "1f0a0000-0000-7000-8000-00000000000a",
      workspace_dir: "/tmp/second",
    });
    expect(onCloned).toHaveBeenCalledWith(CLONED);
  });

  it("marks the workspace that is already open on this machine", async () => {
    // Taking that one would leave this machine with two copies of the same
    // workspace, so the row says what it is before the button is pressed.
    const user = userEvent.setup();
    renderButton();
    vi.mocked(getWorkspaceSyncStatus).mockResolvedValue(
      syncStatus("1f0a0000-0000-7000-8000-00000000000a"),
    );
    vi.mocked(inspectHub).mockResolvedValue({
      endpoint: "user@hub.local",
      is_local: false,
      host_key_fingerprints: [],
      host_key_trusted: true,
      host_key_changed: false,
      workspace_ids: [
        "1f0a0000-0000-7000-8000-00000000000a",
        "1f0a0000-0000-7000-8000-00000000000b",
      ],
    });

    await user.click(screen.getByRole("button", { name: t("sync.clone.action") }));
    await user.type(await screen.findByLabelText(t("sync.connect.endpoint")), "user@hub.local");
    await user.click(screen.getByRole("button", { name: t("sync.connect.inspect") }));

    const current = await screen.findByText("1f0a0000-0000-7000-8000-00000000000a");
    expect(
      await within(current.parentElement!).findByText(t("sync.clone.current")),
    ).toBeInTheDocument();
    const other = screen.getByText("1f0a0000-0000-7000-8000-00000000000b");
    expect(
      within(other.parentElement!).queryByText(t("sync.clone.current")),
    ).not.toBeInTheDocument();
  });

  it("says so when the directory already holds a workspace", async () => {
    const user = userEvent.setup();
    renderButton();
    vi.mocked(cloneWorkspaceFromHub).mockRejectedValue(
      Object.assign(new Error("That directory already holds a GuildBotics workspace."), {
        code: "workspace_already_exists",
      }),
    );

    await user.click(screen.getByRole("button", { name: t("sync.clone.action") }));
    await user.type(await screen.findByLabelText(t("sync.connect.endpoint")), "user@hub.local");
    await user.click(screen.getByRole("button", { name: t("sync.connect.inspect") }));
    await user.click(await screen.findByRole("button", { name: t("sync.clone.take") }));

    expect(await screen.findByText(/already holds a GuildBotics workspace/)).toBeInTheDocument();
  });
});
