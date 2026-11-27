import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { choose, pickFromMenu } from "../test/mantine";
import { workspaceInitials } from "../components/Sidebar";
import { card } from "./fixtures";

const page = (items: unknown[]) => json(200, { items, next_cursor: null });
const workspace = (name: string) => ({ name, lanes: ["new"] });

function setup(empty = false) {
  const routes: Parameters<typeof fakeApi>[0] = {
    "GET /api/v1/session": json(200, SESSION),
    "GET /api/v1/workspaces": page(empty ? [] : [workspace("acme"), workspace("beta")]),
    "GET /api/v1/admin/diagnostics": json(200, {}),
    "GET /api/v1/members": page([]),
    "GET /api/v1/repos": page([]),
    "GET /api/v1/repos/acme/demo/tickets/1": json(200, card("1", "new")),
    "POST /api/v1/workspaces": json(201, workspace("gamma")),
  };
  for (const name of ["acme", "beta", "gamma"]) {
    routes[`GET /api/v1/workspaces/${name}`] = json(200, workspace(name));
    routes[`GET /api/v1/workspaces/${name}/board/cards`] = page([]);
  }
  return fakeApi(routes);
}

async function expectStart(name: string) {
  await waitFor(() => expect(window.location.pathname).toBe(`/workspaces/${name}/task-flow`));
  const nav = await screen.findByRole("navigation", { name: "Main" });
  await within(nav).findByRole("link", { name: "Task flow", current: "page" });
}

describe("workspace navigation", () => {
  it.each(["home", "switch", "create from sidebar", "create from home"] as const)(
    "opens the task flow for %s",
    async (entry) => {
      const fake = setup(entry === "create from home");
      window.localStorage.setItem("branvil.workspace", "beta");
      const user = userEvent.setup();
      const historyLength = window.history.length;
      renderApp(
        entry === "home" || entry === "create from home"
          ? "/workspaces"
          : entry === "switch"
            ? "/workspaces/acme/task-flow"
            : "/settings/repositories/members",
      );

      if (entry === "switch") {
        await choose(user, "Workspace", "beta");
      } else if (entry.startsWith("create")) {
        if (entry === "create from sidebar") await choose(user, "Workspace", "+ New workspace…");
        else await user.click(await screen.findByRole("button", { name: "New workspace" }));
        const dialog = await screen.findByRole("dialog", { name: "New workspace" });
        await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "gamma");
        await user.click(within(dialog).getByRole("button", { name: "Create" }));
        await waitFor(() => expect(fake.calls("POST", "/api/v1/workspaces")).toHaveLength(1));
      }
      const target = entry.startsWith("create") ? "gamma" : "beta";
      await expectStart(target);
      expect(window.localStorage.getItem("branvil.workspace")).toBe(target);
      if (entry === "home") expect(window.history.length).toBe(historyLength);
    },
  );

  it.each([
    [
      "task flow",
      "/workspaces/acme/task-flow",
      "/workspaces/beta/task-flow",
      "Task flow",
      "Task flow",
    ],
    [
      "pull requests",
      "/workspaces/acme/pulls",
      "/workspaces/beta/pulls",
      "Pull requests",
      "Pull requests",
    ],
    [
      "settings",
      "/settings/repositories/members",
      "/workspaces/beta/settings/repositories/members",
      "Settings",
      "Settings",
    ],
    [
      "repository settings",
      "/workspaces/acme/repos/demo/settings",
      "/workspaces/beta/settings/repositories",
      "Settings",
      "Settings",
    ],
    [
      "ticket",
      "/workspaces/acme/repos/demo/tickets/1",
      "/workspaces/beta/task-flow",
      "Task flow",
      "Task flow",
    ],
    [
      "repository",
      "/workspaces/acme/repos/demo",
      "/workspaces/beta/task-flow",
      "Task flow",
      "Pull requests",
    ],
    [
      "pull request",
      "/workspaces/acme/repos/demo/pulls/1",
      "/workspaces/beta/task-flow",
      "Task flow",
      "Pull requests",
    ],
    [
      "commit",
      "/workspaces/acme/repos/demo/commits/abc123",
      "/workspaces/beta/task-flow",
      "Task flow",
      "Pull requests",
    ],
    [
      "job",
      "/workspaces/acme/repos/demo/jobs/att_1",
      "/workspaces/beta/task-flow",
      "Task flow",
      "Pull requests",
    ],
  ])(
    "shows the current location and switches from %s to the expected page",
    async (_, from, to, active, current) => {
      setup();
      // Global settings use the remembered workspace.
      window.localStorage.setItem("branvil.workspace", "acme");
      renderApp(from);
      const before = await screen.findByRole("navigation", { name: "Main" });
      expect(
        within(before)
          .queryAllByRole("link", { current: "page" })
          .map((link) => link.textContent.trim()),
      ).toEqual(current ? [current] : []);
      await choose(userEvent.setup(), "Workspace", "beta");
      await waitFor(() => expect(window.location.pathname).toBe(to));
      const nav = screen.getByRole("navigation", { name: "Main" });
      expect(within(nav).getByRole("link", { name: active })).toHaveAttribute(
        "aria-current",
        "page",
      );
      expect(window.localStorage.getItem("branvil.workspace")).toBe("beta");
    },
  );

  it("links to the task flow, pull requests, releases, and settings", async () => {
    setup();
    renderApp("/workspaces/acme/task-flow");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    const flow = within(nav).getByRole("link", { name: "Task flow" });
    expect(flow).toHaveAttribute("href", "/workspaces/acme/task-flow");
    expect(flow).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("No tickets to show.")).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Releases" })).toHaveAttribute(
      "href",
      "/workspaces/acme/releases",
    );
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.textContent.trim()),
    ).toEqual([
      "Activity",
      "Task flow",
      "Pull requests",
      "Releases",
      "Service",
      "Edit Command",
      "Diagnostics",
      "Settings",
    ]);
  });
});

it("switches workspaces within settings even when storage cannot save the selection", async () => {
  setup();
  const save = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("Storage disabled");
  });
  try {
    renderApp("/settings/repositories/members");
    await choose(userEvent.setup(), "Workspace", "beta");
    await waitFor(() =>
      expect(window.location.pathname).toBe("/workspaces/beta/settings/repositories/members"),
    );
    expect(screen.getByRole("combobox", { name: "Workspace" })).toHaveValue("beta");
  } finally {
    save.mockRestore();
  }
});

it.each(["/acme/-/relations", "/acme/-/board"])(
  "shows the removed URL %s as not found without redirecting",
  async (path) => {
    const fake = setup();
    renderApp(path);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(window.location.pathname).toBe(path);
    expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards")).toHaveLength(0);
  },
);

describe("shared navigation", () => {
  it.each([
    ["/", "/activity"],
    ["/activity", "/activity"],
    ["/service", "/service"],
    ["/overview", "/service"],
    ["/commands", "/commands"],
    ["/diagnostics", "/diagnostics"],
    ["/setup?section=members&tab=patrol#focus", "/setup?section=members&tab=patrol#focus"],
    ["/settings/repositories/members", "/settings/repositories/members"],
  ])("opens %s in the remembered workspace after the list loads", async (entry, target) => {
    let release!: (value: ReturnType<typeof json>) => void;
    const list = new Promise<ReturnType<typeof json>>((resolve) => {
      release = resolve;
    });
    const fake = fakeApi({
      "GET /-/guildbotics/api/config/status": json(200, {
        branvil_available: true,
        project_file_exists: true,
        workspace_name: "acme",
      }),
      "GET /api/v1/workspaces": () => list,
    });
    window.localStorage.setItem("branvil.workspace", "beta");
    renderApp(entry);
    await waitFor(() => expect(fake.calls("GET", "/api/v1/workspaces")).toHaveLength(1));
    expect(window.location.pathname + window.location.search + window.location.hash).toBe(entry);
    expect(fake.calls("GET", "/-/guildbotics/api/team")).toHaveLength(0);
    const main = screen.getByRole("navigation", { name: "Main" });
    expect(within(main).getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/setup");
    expect(within(main).queryByRole("link", { name: "Task flow" })).not.toBeInTheDocument();
    release(page([workspace("acme"), workspace("beta")]));
    await waitFor(() =>
      expect(window.location.pathname + window.location.search + window.location.hash).toBe(
        "/workspaces/beta" + target,
      ),
    );
    if (entry.startsWith("/setup") || entry.startsWith("/settings/")) {
      const nav = screen.getByRole("navigation", { name: "Main" });
      await within(nav).findByRole("link", { name: "Settings", current: "page" });
      expect(await screen.findByRole("combobox", { name: "Workspace" })).toHaveValue("beta");
      await waitFor(() => expect(fake.calls("GET", "/-/guildbotics/api/team")).toHaveLength(1));
      expect(
        fake.calls("GET", "/-/guildbotics/api/team")[0]?.headers["X-GuildBotics-Workspace"],
      ).toBe("beta");
    }
  });

  it("keeps the remembered workspace until a failed list can be retried", async () => {
    fakeApi({
      "GET /-/guildbotics/api/config/status": json(200, {
        branvil_available: true,
        project_file_exists: true,
        workspace_name: "acme",
      }),
      "GET /api/v1/workspaces": [
        json(503, {}),
        json(503, {}),
        json(503, {}),
        page([workspace("acme"), workspace("beta")]),
      ],
    });
    window.localStorage.setItem("branvil.workspace", "beta");
    renderApp("/setup");
    const retry = await screen.findByRole("button", { name: "Retry" });
    expect(window.location.pathname).toBe("/setup");
    expect(window.localStorage.getItem("branvil.workspace")).toBe("beta");
    await userEvent.click(retry);
    await waitFor(() => expect(window.location.pathname).toBe("/workspaces/beta/setup"));
  });

  it("keeps the settings section and drops the previous workspace's member editor state", async () => {
    setup();
    renderApp("/workspaces/acme/setup?section=members&tab=patrol&person_id=alice");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Workspace" })).toBeEnabled());
    await choose(userEvent.setup(), "Workspace", "beta");
    await waitFor(() => {
      expect(window.location.pathname).toBe("/workspaces/beta/setup");
      expect(window.location.search).toBe("?section=members");
    });
  });

  it("shows the workspace from a deep link and uses it throughout the sidebar", async () => {
    setup();
    window.localStorage.setItem("branvil.workspace", "beta");
    renderApp("/workspaces/acme/repos/demo/tickets/1");
    expect(await screen.findByRole("combobox", { name: "Workspace" })).toHaveValue("acme");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Edit Command" })).toHaveAttribute(
      "href",
      "/workspaces/acme/commands",
    );
    expect(within(nav).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/workspaces/acme/setup",
    );
    await userEvent.click(within(nav).getByRole("link", { name: "Pull requests" }));
    expect(screen.getByRole("navigation", { name: "Main" })).toBe(nav);
    expect(window.location.pathname).toBe("/workspaces/acme/pulls");
  });

  it("marks a list failure and enables the selected workspace after retry", async () => {
    fakeApi({
      "GET /api/v1/workspaces": [
        json(503, {}),
        json(503, {}),
        json(503, {}),
        page([workspace("acme"), workspace("beta")]),
      ],
    });
    renderApp("/workspaces/beta/pulls");
    const select = await screen.findByRole("combobox", { name: "Workspace" });
    expect(select).toHaveValue("beta");
    expect(select).toBeDisabled();
    expect(await screen.findByText("Could not load workspaces.")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(select).toBeEnabled());
    expect(select).toHaveValue("beta");
    expect(screen.queryByText("Could not load workspaces.")).not.toBeInTheDocument();
  });

  it("persists the sidebar language in the shared preference", async () => {
    setup(true);
    renderApp("/workspaces");
    await screen.findByRole("navigation", { name: "Main" });
    await choose(userEvent.setup(), "Display language", "日本語");
    expect(localStorage.getItem("guildbotics.appLanguage")).toBe("ja");
    expect(screen.getByRole("navigation", { name: "メイン" })).toBeVisible();
  });
});

it.each([
  ["demo", "DE"],
  ["GuildBotics", "GB"],
  ["my-team", "MT"],
  ["acme_web.app", "AW"],
  ["x", "X"],
  ["開発チーム", "開発"],
  ["", ""],
])("names the workspace %j by the initials %j on the icon rail", (name, initials) => {
  expect(workspaceInitials(name)).toBe(initials);
});

describe("collapsible sidebar", () => {
  it("collapses to icons on a wide screen, remembers it, and keeps every control usable", async () => {
    setup();
    const user = userEvent.setup();
    const view = renderApp("/workspaces/acme/task-flow");
    await expectStart("acme");
    await user.click(screen.getByRole("button", { name: "Collapse the menu", expanded: true }));
    expect(window.localStorage.getItem("guildbotics.sidebar")).toBe("collapsed");
    expect(screen.getByRole("button", { name: "Expand the menu", expanded: false })).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Workspace" })).not.toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    const link = within(nav).getByRole("link", { name: "Pull requests" });
    await user.hover(link);
    expect(await screen.findByRole("tooltip", { name: "Pull requests" })).toBeVisible();

    expect(screen.getByRole("button", { name: "Workspace: acme" })).toHaveTextContent("AC");
    await pickFromMenu(user, "Workspace: acme", "beta");
    await expectStart("beta");
    await pickFromMenu(user, "Display language", "日本語");
    expect(localStorage.getItem("guildbotics.appLanguage")).toBe("ja");
    expect(await screen.findByRole("button", { name: "メニューを開く" })).toBeVisible();

    view.unmount();
    renderApp("/workspaces/beta/task-flow", "ja");
    await user.click(await screen.findByRole("button", { name: "メニューを開く" }));
    expect(window.localStorage.getItem("guildbotics.sidebar")).toBe("expanded");
    expect(await screen.findByRole("combobox", { name: "ワークスペース" })).toHaveValue("beta");
  });

  it("stays a rail on a narrow screen and opens the menu over the page until the next move", async () => {
    const media = vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
      matches: query === "(max-width: 900px)" || query === "(prefers-reduced-motion: reduce)",
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    try {
      setup();
      window.localStorage.setItem("guildbotics.sidebar", "expanded");
      const user = userEvent.setup();
      renderApp("/workspaces/acme/task-flow");
      await expectStart("acme");
      const expand = () => screen.getByRole("button", { name: "Expand the menu", expanded: false });
      expect(screen.queryByRole("combobox", { name: "Workspace" })).not.toBeInTheDocument();

      await user.click(expand());
      expect(await screen.findByRole("combobox", { name: "Workspace" })).toHaveValue("acme");
      const nav = screen.getByRole("navigation", { name: "Main" });
      await user.click(within(nav).getByRole("link", { name: "Releases" }));
      await waitFor(() => expect(window.location.pathname).toBe("/workspaces/acme/releases"));
      expect(expand()).toBeVisible();
      expect(screen.queryByRole("combobox", { name: "Workspace" })).not.toBeInTheDocument();

      await user.click(expand());
      await user.keyboard("{Escape}");
      expect(expand()).toBeVisible();
      expect(window.localStorage.getItem("guildbotics.sidebar")).toBe("expanded");
    } finally {
      media.mockRestore();
    }
  });
});
