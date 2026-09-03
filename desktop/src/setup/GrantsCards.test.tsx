import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  evaluateGrant,
  type GrantEvaluation,
  type LocalGrants,
  type SandboxAccessStatus,
  type SharedGrants,
} from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { GrantsCards } from "./GrantsCards";

vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn() }));
vi.mock("@tauri-apps/api/path", () => ({ homeDir: vi.fn(async () => "/Users/me/") }));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  evaluateGrant: vi.fn(),
}));

const t = i18n.getFixedT("en");

/** How the device reports the saved grants: display form beside the grant file's spelling. */
const macStatus: SandboxAccessStatus = {
  documents: [{ path: "$HOME/Documents", grant: "Documents", access: "read", present: true }],
  paths: [{ path: "/opt/nowhere", grant: "/opt/nowhere", access: "read", present: false }],
  trees: [
    { path: "$HOME/.local", grant: ".local", sources: ["$HOME/.local/bin"] },
    {
      path: "/opt/homebrew",
      grant: "/opt/homebrew",
      sources: ["/opt/homebrew/bin", "/opt/homebrew/sbin"],
    },
  ],
  excluded: [
    {
      path: "$HOME/.codex/packages/standalone",
      source: "$HOME/.local/bin",
      reason: "it is under ~/.codex",
    },
  ],
  denied: [{ path: "$HOME/.ssh", builtin: true }],
  problem: "",
};

/** A Windows device spells the same rows with backslashes after `$HOME`. */
const windowsStatus: SandboxAccessStatus = {
  documents: [],
  paths: [
    {
      path: "$HOME\\AppData\\Local\\uv\\cache",
      grant: "AppData/Local/uv/cache",
      access: "read_write",
      present: false,
    },
  ],
  trees: [
    {
      path: "$HOME\\AppData\\Local\\Programs\\Python",
      grant: "AppData\\Local\\Programs\\Python",
      sources: ["$HOME\\AppData\\Local\\Programs\\Python\\Scripts"],
    },
  ],
  excluded: [],
  denied: [{ path: "$HOME\\.ssh", builtin: true }],
  problem: "",
};

function Harness({
  shared: initialShared = { documents: [] },
  local: initialLocal = { paths: [], deny: [] },
  status = macStatus,
  onShared,
  onLocal,
}: {
  shared?: SharedGrants;
  local?: LocalGrants;
  status?: SandboxAccessStatus;
  onShared?: (shared: SharedGrants) => void;
  onLocal?: (local: LocalGrants) => void;
}) {
  const [shared, setShared] = useState(initialShared);
  const [local, setLocal] = useState(initialLocal);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <MantineProvider env="test">
        <GrantsCards
          shared={shared}
          local={local}
          status={status}
          onSharedChange={(next) => {
            setShared(next);
            onShared?.(next);
          }}
          onLocalChange={(next) => {
            setLocal(next);
            onLocal?.(next);
          }}
        />
      </MantineProvider>
    </QueryClientProvider>
  );
}

function evaluation(overrides: Partial<GrantEvaluation>): GrantEvaluation {
  return {
    scope: "document",
    path: "",
    access: "read",
    valid: true,
    reason: "",
    present: true,
    sensitive: "",
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(evaluateGrant).mockImplementation(async ({ scope, path, access }) => {
    if (path === "..") {
      return evaluation({
        scope,
        path,
        access,
        valid: false,
        reason: "'..' must name a directory",
      });
    }
    if (scope === "deny") {
      return evaluation({ scope, path, access: "" });
    }
    return evaluation({
      scope,
      path,
      access,
      present: path !== "Projects/new",
      sensitive: path.startsWith(".ssh") ? "~/.ssh" : "",
    });
  });
});

describe("GrantsCards", () => {
  it("lists documents and, for this device, PATH directories, added paths, denies, and what stays closed", () => {
    render(
      <Harness
        shared={{ documents: [{ path: "Documents", access: "read" }] }}
        local={{
          paths: [
            { path: ".cache/uv", access: "read_write" },
            { path: "/opt/nowhere", access: "read" },
          ],
          deny: ["/opt/homebrew/etc"],
        }}
      />,
    );

    const documents = screen.getByTestId("grants:document");
    expect(documents).toHaveTextContent("Documents");
    expect(documents).toHaveTextContent(t("setup.intelligence.grants.presentHere"));
    expect(
      within(documents).getByRole("combobox", {
        name: t("setup.intelligence.grants.accessFor", { path: "Documents" }),
      }),
    ).toHaveValue(t("setup.intelligence.grants.accessLabels.read"));
    const device = screen.getByTestId("grants:device");
    expect(device).toHaveTextContent("PATH: /opt/homebrew/bin, /opt/homebrew/sbin");
    expect(device).toHaveTextContent(".cache/uv");
    // A path this device lacks is marked on its row, where it can be fixed.
    expect(device).toHaveTextContent(`/opt/nowhere${t("setup.intelligence.grants.absentHere")}`);
    expect(device).toHaveTextContent("/opt/homebrew/etc");
    expect(device).toHaveTextContent("$HOME/.codex/packages/standalone");
    expect(device).toHaveTextContent("it is under ~/.codex");
    expect(device).toHaveTextContent(`$HOME/.ssh${t("setup.intelligence.deviceAccess.builtin")}`);
  });

  it("denies and reopens a PATH directory from its access select, saved relative to the home", async () => {
    const user = userEvent.setup();
    const onLocal = vi.fn();
    render(<Harness onLocal={onLocal} />);
    const device = within(screen.getByTestId("grants:device"));
    const access = device.getByRole("combobox", {
      name: t("setup.intelligence.grants.accessFor", { path: "$HOME/.local" }),
    });
    expect(access).toHaveValue(t("setup.intelligence.grants.accessLabels.read"));

    await user.click(access);
    await user.click(
      await screen.findByRole("option", { name: t("setup.intelligence.deviceAccess.deny") }),
    );
    expect(onLocal).toHaveBeenLastCalledWith({ paths: [], deny: [".local"] });
    expect(access).toHaveValue(t("setup.intelligence.deviceAccess.deny"));

    await user.click(access);
    await user.click(
      await screen.findByRole("option", { name: t("setup.intelligence.grants.accessLabels.read") }),
    );
    expect(onLocal).toHaveBeenLastCalledWith({ paths: [], deny: [] });
  });

  it("denies a PATH directory in place on a device that spells paths with backslashes", async () => {
    const user = userEvent.setup();
    const onLocal = vi.fn();
    render(
      <Harness
        status={windowsStatus}
        local={{ paths: [{ path: "AppData/Local/uv/cache", access: "read_write" }], deny: [] }}
        onLocal={onLocal}
      />,
    );
    const device = within(screen.getByTestId("grants:device"));
    const python = "$HOME\\AppData\\Local\\Programs\\Python";
    const access = device.getByRole("combobox", {
      name: t("setup.intelligence.grants.accessFor", { path: python }),
    });
    // The added path is matched to its row by the file's spelling, whatever
    // separators the device shows it with.
    expect(device.getByText("AppData/Local/uv/cache").parentElement).toHaveTextContent(
      t("setup.intelligence.grants.absentHere"),
    );

    await user.click(access);
    await user.click(
      await screen.findByRole("option", { name: t("setup.intelligence.deviceAccess.deny") }),
    );

    // The deny is written as the device spells the tree, so the row it
    // closes is the one that changes: no second row for the same directory.
    expect(onLocal).toHaveBeenLastCalledWith({
      paths: [{ path: "AppData/Local/uv/cache", access: "read_write" }],
      deny: ["AppData\\Local\\Programs\\Python"],
    });
    expect(access).toHaveValue(t("setup.intelligence.deviceAccess.deny"));
    expect(
      device.getAllByRole("combobox", {
        name: t("setup.intelligence.grants.accessFor", { path: python }),
      }),
    ).toHaveLength(1);
    expect(
      device.queryByRole("combobox", {
        name: t("setup.intelligence.grants.accessFor", {
          path: "AppData\\Local\\Programs\\Python",
        }),
      }),
    ).not.toBeInTheDocument();
  });

  it("adds a typed path as read/write or as a deny once the keystrokes settle, and removes them", async () => {
    const user = userEvent.setup();
    const onLocal = vi.fn();
    render(<Harness onLocal={onLocal} />);
    const device = within(screen.getByTestId("grants:device"));
    const path = device.getByRole("textbox", { name: t("setup.intelligence.grants.path") });
    const kind = device.getByRole("combobox", { name: t("setup.intelligence.grants.access") });
    const add = device.getByRole("button", { name: t("setup.intelligence.grants.add") });

    await user.type(path, "..");
    await device.findByText("'..' must name a directory");
    expect(add).toBeDisabled();

    await user.clear(path);
    await user.type(path, ".cache/uv");
    await user.click(kind);
    await user.click(
      await screen.findByRole("option", {
        name: t("setup.intelligence.grants.accessLabels.read_write"),
      }),
    );
    await waitFor(() => expect(add).toBeEnabled());
    await user.click(add);
    expect(onLocal).toHaveBeenLastCalledWith({
      paths: [{ path: ".cache/uv", access: "read_write" }],
      deny: [],
    });
    expect(screen.queryByText(t("setup.intelligence.grants.duplicate"))).not.toBeInTheDocument();

    await user.type(path, "/opt/homebrew/etc");
    await user.click(kind);
    await user.click(
      await screen.findByRole("option", { name: t("setup.intelligence.deviceAccess.deny") }),
    );
    await waitFor(() => expect(add).toBeEnabled());
    const judged = vi.mocked(evaluateGrant).mock.calls.map(([args]) => [args.scope, args.path]);
    expect(judged.filter(([, p]) => p.startsWith("/opt"))).toEqual([["deny", "/opt/homebrew/etc"]]);
    await user.click(add);
    expect(onLocal).toHaveBeenLastCalledWith({
      paths: [{ path: ".cache/uv", access: "read_write" }],
      deny: ["/opt/homebrew/etc"],
    });

    // A deny row carries the same select: turning it into a read grant moves
    // it between the two lists in place.
    const etc = device.getByRole("combobox", {
      name: t("setup.intelligence.grants.accessFor", { path: "/opt/homebrew/etc" }),
    });
    expect(etc).toHaveValue(t("setup.intelligence.deviceAccess.deny"));
    await user.click(etc);
    await user.click(
      await screen.findByRole("option", { name: t("setup.intelligence.grants.accessLabels.read") }),
    );
    expect(onLocal).toHaveBeenLastCalledWith({
      paths: [
        { path: ".cache/uv", access: "read_write" },
        { path: "/opt/homebrew/etc", access: "read" },
      ],
      deny: [],
    });
    await user.click(
      device.getByRole("button", {
        name: t("setup.intelligence.grants.remove", { path: "/opt/homebrew/etc" }),
      }),
    );
    expect(onLocal).toHaveBeenLastCalledWith({
      paths: [{ path: ".cache/uv", access: "read_write" }],
      deny: [],
    });
    await user.click(
      device.getByRole("button", {
        name: t("setup.intelligence.grants.remove", { path: ".cache/uv" }),
      }),
    );
    expect(onLocal).toHaveBeenLastCalledWith({ paths: [], deny: [] });
  });

  it("changes a shared document's access in place", async () => {
    const user = userEvent.setup();
    const onShared = vi.fn();
    render(
      <Harness
        shared={{ documents: [{ path: "Documents", access: "read" }] }}
        onShared={onShared}
      />,
    );
    const access = within(screen.getByTestId("grants:document")).getByRole("combobox", {
      name: t("setup.intelligence.grants.accessFor", { path: "Documents" }),
    });

    await user.click(access);
    await user.click(
      await screen.findByRole("option", {
        name: t("setup.intelligence.grants.accessLabels.read_write"),
      }),
    );

    expect(onShared).toHaveBeenLastCalledWith({
      documents: [{ path: "Documents", access: "read_write" }],
    });
  });

  it("picks a shared document directory inside the home and shows it relative to it", async () => {
    const { open } = await import("@tauri-apps/plugin-dialog");
    vi.mocked(open).mockResolvedValueOnce("/Users/me/Documents/shared");
    Object.assign(window, { __TAURI_INTERNALS__: {} });
    try {
      const user = userEvent.setup();
      render(<Harness />);
      const documents = within(screen.getByTestId("grants:document"));
      const path = documents.getByRole("textbox", { name: t("setup.intelligence.grants.path") });

      await user.click(
        documents.getByRole("button", { name: t("setup.intelligence.grants.choose") }),
      );
      await waitFor(() => expect(path).toHaveValue("Documents/shared"));
      expect(open).toHaveBeenCalledWith({
        directory: true,
        multiple: false,
        defaultPath: "/Users/me",
      });
    } finally {
      delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    }
  });

  it("fills the path from the directory picker, relative to the home when under it", async () => {
    const { open } = await import("@tauri-apps/plugin-dialog");
    vi.mocked(open).mockResolvedValueOnce("/Users/me/.cache/pnpm").mockResolvedValueOnce("/opt/x");
    Object.assign(window, { __TAURI_INTERNALS__: {} });
    try {
      const user = userEvent.setup();
      render(<Harness />);
      const device = within(screen.getByTestId("grants:device"));
      const path = device.getByRole("textbox", { name: t("setup.intelligence.grants.path") });
      const choose = device.getByRole("button", {
        name: t("setup.intelligence.grants.choose"),
      });

      await user.click(choose);
      await waitFor(() => expect(path).toHaveValue(".cache/pnpm"));
      await user.click(choose);
      await waitFor(() => expect(path).toHaveValue("/opt/x"));
      expect(open).toHaveBeenCalledWith({
        directory: true,
        multiple: false,
        defaultPath: undefined,
      });
    } finally {
      delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    }
  });

  it("relativizes a picked directory to a Windows home directory", async () => {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const { homeDir } = await import("@tauri-apps/api/path");
    vi.mocked(homeDir).mockResolvedValueOnce("C:\\Users\\me\\");
    vi.mocked(open).mockResolvedValueOnce("C:\\Users\\me\\AppData\\Local\\uv\\cache");
    Object.assign(window, { __TAURI_INTERNALS__: {} });
    try {
      const user = userEvent.setup();
      render(<Harness status={windowsStatus} />);
      const device = within(screen.getByTestId("grants:device"));
      const path = device.getByRole("textbox", { name: t("setup.intelligence.grants.path") });

      await user.click(device.getByRole("button", { name: t("setup.intelligence.grants.choose") }));
      await waitFor(() => expect(path).toHaveValue("AppData\\Local\\uv\\cache"));
    } finally {
      delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    }
  });

  it("judges a typed document path, warns on sensitive ones, and adds it", async () => {
    const user = userEvent.setup();
    const onShared = vi.fn();
    render(<Harness onShared={onShared} />);
    const documents = within(screen.getByTestId("grants:document"));
    const path = documents.getByRole("textbox", { name: t("setup.intelligence.grants.path") });
    const add = documents.getByRole("button", { name: t("setup.intelligence.grants.add") });

    await user.type(path, "..");
    await documents.findByText("'..' must name a directory");

    await user.clear(path);
    await user.type(path, ".ssh");
    await documents.findByText(t("setup.intelligence.grants.sensitiveTitle"));
    await waitFor(() => expect(add).toBeEnabled());
    await user.click(add);

    expect(onShared).toHaveBeenLastCalledWith({
      documents: [{ path: ".ssh", access: "read" }],
    });
    // The emptied field does not flash "already added" while the old text settles.
    expect(path).toHaveValue("");
    expect(screen.queryByText(t("setup.intelligence.grants.duplicate"))).not.toBeInTheDocument();
  });
});
