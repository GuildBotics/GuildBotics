import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createDeviceSshKey, createHub, getDeviceSshKey, getHubStatus } from "../api/client";
import i18n from "../i18n";
import { DeviceSettings } from "./DeviceSettings";

const t = i18n.getFixedT("en");

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    createDeviceSshKey: vi.fn(),
    createHub: vi.fn(),
    getDeviceSshKey: vi.fn(),
    getHubStatus: vi.fn(),
  };
});

function renderSettings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MantineProvider>
        <DeviceSettings />
      </MantineProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
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
  it("asks for nothing beyond this machine", async () => {
    // The machine that hosts the hub is often the one that never gets a
    // workspace of its own, so nothing here may depend on one being selected.
    const user = userEvent.setup();
    vi.mocked(createHub).mockResolvedValue({
      hosted: true,
      hub_root: "/home/u/.guildbotics/hub",
      hub_id: "hub-1",
      created_at: "2026-07-01T00:00:00+00:00",
      ssh_endpoint: "user@hub.local",
      workspace_ids: [],
    });
    renderSettings();

    await user.click(await screen.findByRole("button", { name: t("sync.host.create") }));

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
