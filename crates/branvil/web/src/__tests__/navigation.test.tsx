import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { choose } from "../test/mantine";
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
  await waitFor(() => expect(window.location.pathname).toBe(`/${name}`));
  const nav = await screen.findByRole("navigation", { name: "Main" });
  expect(await within(nav).findByRole("link", { name: "Task flow" })).toHaveAttribute("aria-current", "page");
}

describe("workspace navigation", () => {
  it.each(["home", "switch", "create from sidebar", "create from home"] as const)("opens the task flow for %s", async (entry) => {
    const fake = setup(entry === "create from home");
    window.localStorage.setItem("branvil.workspace", "beta");
    const user = userEvent.setup();
    const historyLength = window.history.length;
    renderApp(entry === "home" || entry === "create from home" ? "/" : entry === "switch" ? "/acme" : "/-/settings/members");

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
  });

  it.each([
    ["task flow", "/acme", "/beta", "Task flow", "Task flow"],
    ["pull requests", "/acme/-/pulls", "/beta/-/pulls", "Pull requests", "Pull requests"],
    ["settings", "/-/settings/members", "/-/settings", "Settings", "Settings"],
    ["repository settings", "/-/settings/repos/acme/demo", "/-/settings", "Settings", "Settings"],
    ["ticket", "/acme/demo/-/tickets/1", "/beta", "Task flow", "Task flow"],
    ["repository", "/acme/demo", "/beta", "Task flow", "Pull requests"],
    ["pull request", "/acme/demo/-/pulls/1", "/beta", "Task flow", "Pull requests"],
    ["commit", "/acme/demo/-/commits/abc123", "/beta", "Task flow", "Pull requests"],
    ["job", "/acme/demo/-/jobs/att_1", "/beta", "Task flow", "Pull requests"],
  ])("shows the current location and switches from %s to the expected page", async (_, from, to, active, current) => {
    setup();
    // Global settings use the remembered workspace.
    window.localStorage.setItem("branvil.workspace", "acme");
    renderApp(from);
    const before = await screen.findByRole("navigation", { name: "Main" });
    expect(within(before).queryAllByRole("link", { current: "page" }).map((link) => link.textContent)).toEqual(current ? [current] : []);
    await choose(userEvent.setup(), "Workspace", "beta");
    await waitFor(() => expect(window.location.pathname).toBe(to));
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: active })).toHaveAttribute("aria-current", "page");
    expect(window.localStorage.getItem("branvil.workspace")).toBe("beta");
  });

  it("links to the task flow, pull requests, and settings", async () => {
    setup();
    renderApp("/acme");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    const flow = within(nav).getByRole("link", { name: "Task flow" });
    expect(flow).toHaveAttribute("href", "/acme");
    expect(flow).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("No tickets to show.")).toBeInTheDocument();
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual(["Task flow", "Pull requests", "Settings"]);
  });
});

it("switches workspaces within settings even when storage cannot save the selection", async () => {
  setup();
  const save = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage disabled"); });
  try {
    renderApp("/-/settings/members");
    await choose(userEvent.setup(), "Workspace", "beta");
    await waitFor(() => expect(window.location.pathname).toBe("/-/settings"));
    expect(screen.getByRole("combobox", { name: "Workspace" })).toHaveValue("beta");
  } finally {
    save.mockRestore();
  }
});

it.each(["/acme/-/relations", "/acme/-/board"])("shows the removed URL %s as not found without redirecting", async (path) => {
  const fake = setup();
  renderApp(path);
  expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  expect(window.location.pathname).toBe(path);
  expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards")).toHaveLength(0);
});

describe("embedding host navigation", () => {
  it.each([null, { label: "Host screen", url: "/host/" }])("uses only the server supplied destination %j", async (navigation) => {
    fakeApi({
      "GET /api/v1/session": json(200, { ...SESSION, host_navigation: navigation }),
      "GET /api/v1/workspaces": page([]),
    });
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    if (navigation) expect(within(nav).getByRole("link", { name: navigation.label })).toHaveAttribute("href", navigation.url);
    else expect(within(nav).getAllByRole("link")).toHaveLength(1);
  });

  it("persists the sidebar language in the shared preference", async () => {
    setup(true);
    renderApp("/");
    await screen.findByRole("navigation", { name: "Main" });
    await choose(userEvent.setup(), "Display language", "日本語");
    expect(localStorage.getItem("guildbotics.appLanguage")).toBe("ja");
    expect(screen.getByRole("navigation", { name: "メイン" })).toBeVisible();
  });
});
