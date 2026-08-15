import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { cloneWorkspaceFromHub, inspectHub, type ConfigStatus } from "../api/client";
import i18n from "../i18n";
import { CloneFromHubButton } from "./CloneFromHub";

const t = i18n.getFixedT("en");

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    cloneWorkspaceFromHub: vi.fn(),
    inspectHub: vi.fn(),
    trustHub: vi.fn(),
  };
});

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
  vi.mocked(inspectHub).mockResolvedValue({
    endpoint: "user@hub.local",
    is_local: false,
    host_key_fingerprints: [],
    host_key_trusted: true,
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
