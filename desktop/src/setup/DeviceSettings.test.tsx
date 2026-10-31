import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getConfigStatus, getRegisteredWorkspaces, unregisterWorkspace } from "../api/client";
import i18n from "../i18n";
import { DeviceSettings } from "./DeviceSettings";
import { TestMantineProvider } from "../test/TestMantineProvider";

const t = i18n.getFixedT("en");

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    getRegisteredWorkspaces: vi.fn(),
    getConfigStatus: vi.fn(),
    unregisterWorkspace: vi.fn(),
  };
});

function renderSettings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <DeviceSettings />
      </TestMantineProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(getRegisteredWorkspaces).mockResolvedValue([]);
  vi.mocked(getConfigStatus).mockResolvedValue({ workspace: null } as Awaited<
    ReturnType<typeof getConfigStatus>
  >);
});

describe("registered workspaces", () => {
  it("keeps the confirmation open during removal and shows a later failure", async () => {
    vi.mocked(getRegisteredWorkspaces).mockResolvedValue(["/work/one"]);
    let reject!: (error: Error) => void;
    vi.mocked(unregisterWorkspace).mockReturnValue(
      new Promise((_, fail) => {
        reject = fail;
      }),
    );
    const user = userEvent.setup();
    renderSettings();
    await user.click(
      await screen.findByRole("button", {
        name: t("setup.device.registeredWorkspaces.remove", { path: "/work/one" }),
      }),
    );
    await user.click(
      screen.getByRole("button", { name: t("setup.device.registeredWorkspaces.confirmLabel") }),
    );
    await user.keyboard("{Escape}");
    await user.click(document.body);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("setup.device.registeredWorkspaces.cancel") }),
    ).toBeDisabled();
    reject(new Error("Removal failed"));
    const error = await screen.findByText("Removal failed");
    expect(error.closest('[role="dialog"]')).not.toBeNull();
    expect(
      screen.getByRole("button", { name: t("setup.device.registeredWorkspaces.cancel") }),
    ).toBeEnabled();
  });
  it("allows cancellation and displays mutation failures inside the confirmation", async () => {
    vi.mocked(getRegisteredWorkspaces).mockResolvedValue(["/work/one"]);
    vi.mocked(unregisterWorkspace).mockRejectedValue(new Error("Registry is locked"));
    const user = userEvent.setup();
    renderSettings();
    const open = await screen.findByRole("button", {
      name: t("setup.device.registeredWorkspaces.remove", { path: "/work/one" }),
    });
    await user.click(open);
    await user.click(
      screen.getByRole("button", { name: t("setup.device.registeredWorkspaces.cancel") }),
    );
    expect(unregisterWorkspace).not.toHaveBeenCalled();
    await user.click(open);
    await user.click(
      screen.getByRole("button", { name: t("setup.device.registeredWorkspaces.confirmLabel") }),
    );
    const error = await screen.findByText("Registry is locked");
    expect(error.closest('[role="dialog"]')).not.toBeNull();
    await user.click(
      screen.getByRole("button", { name: t("setup.device.registeredWorkspaces.cancel") }),
    );
    await user.click(open);
    expect(screen.queryByText("Registry is locked")).not.toBeInTheDocument();
  });
  it("marks the selected workspace and disables its removal", async () => {
    vi.mocked(getConfigStatus).mockResolvedValue({ workspace: "/work/one" } as Awaited<
      ReturnType<typeof getConfigStatus>
    >);
    vi.mocked(getRegisteredWorkspaces).mockResolvedValue(["/work/one"]);
    renderSettings();
    expect(
      await screen.findByText(t("setup.device.registeredWorkspaces.active")),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: t("setup.device.registeredWorkspaces.remove", { path: "/work/one" }),
      }),
    ).toBeDisabled();
    expect(unregisterWorkspace).not.toHaveBeenCalled();
  });
  it("lists every location and removes only the requested registration", async () => {
    vi.mocked(getRegisteredWorkspaces).mockResolvedValue(["/work/one", "/work/two"]);
    vi.mocked(unregisterWorkspace).mockResolvedValue(["/work/two"]);
    const user = userEvent.setup();
    renderSettings();
    await user.click(
      await screen.findByRole("button", {
        name: t("setup.device.registeredWorkspaces.remove", { path: "/work/one" }),
      }),
    );
    expect(unregisterWorkspace).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: t("setup.device.registeredWorkspaces.confirmLabel") }),
    );
    expect(unregisterWorkspace).toHaveBeenCalledWith("/work/one", expect.anything());
    await waitFor(() => expect(screen.queryByText("/work/one")).not.toBeInTheDocument());
    expect(screen.getByText("/work/two")).toBeInTheDocument();
  });
});
