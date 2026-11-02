import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { fakeApi, json, ME, renderApp, SESSION } from "../test/fakeApi";
import { choose } from "../test/mantine";

const page = (items: unknown[]) => json(200, { items, next_cursor: null });

/** What every page reads: the session, the workspaces, the warnings. */
const SHELL = {
  "GET /api/v1/session": json(200, SESSION),
  "GET /api/v1/workspaces": page([{ name: "acme", lanes: [] }]),
  "GET /api/v1/admin/diagnostics": json(200, {}),
};

const REPO = {
  path: "acme/demo",
  default_branch: "main",
  clone_url: "http://127.0.0.1:7878/git/acme/demo.git",
  url: "",
  protected_branches: ["main"],
  required_checks: [],
  required_approvals: 1,
  auto_land: false,
  ci_network: false,
  labels: ["bug"],
  upstream: null,
  revision: 4,
};

/** Adds `values` to the pill input labelled `label`, one Enter each. */
async function addPills(user: ReturnType<typeof userEvent.setup>, label: string | RegExp, values: string[]) {
  const input = screen.getByLabelText(label, { selector: "input:not([type=hidden])" });
  for (const v of values) await user.type(input, `${v}{Enter}`);
}

describe("settings", () => {
  it.each(["en", "ja"] as const)("updates the sidebar immediately and remembers the display preference (%s)", async (language) => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/session": json(200, { ...SESSION, me: { ...ME, kind: "agent" } }),
      "GET /api/v1/members": page([]),
    });
    const user = userEvent.setup();
    const labels = language === "ja"
      ? { main: "メイン", display: "表示", flow: "タスクフロー", board: "タスクボード", toggle: "サイドメニューにタスクボードを表示する" }
      : { main: "Main", display: "Display", flow: "Task flow", board: "Task board", toggle: "Show the task board in the sidebar" };
    const first = renderApp("/-/settings/members", language);
    const nav = await screen.findByRole("navigation", { name: labels.main });
    await within(nav).findByRole("link", { name: labels.board });
    expect(within(nav).getAllByRole("link").slice(0, 2).map((link) => link.textContent)).toEqual([labels.flow, labels.board]);
    await user.click(await screen.findByRole("link", { name: labels.display }));
    const toggle = await screen.findByRole("switch", { name: labels.toggle });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    expect(within(nav).queryByRole("link", { name: labels.board })).not.toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: labels.flow })).toHaveAttribute("href", "/acme");
    first.unmount();

    const second = renderApp("/-/settings/display", language);
    const restored = await screen.findByRole("switch", { name: labels.toggle });
    expect(restored).not.toBeChecked();
    const restoredNav = screen.getByRole("navigation", { name: labels.main });
    await within(restoredNav).findByRole("link", { name: labels.flow });
    expect(within(restoredNav).queryByRole("link", { name: labels.board })).not.toBeInTheDocument();
    await user.click(restored);
    expect(within(restoredNav).getByRole("link", { name: labels.board })).toHaveAttribute("href", "/acme/-/board");
    second.unmount();

    renderApp("/-/settings/display", language);
    expect(await screen.findByRole("switch", { name: labels.toggle })).toBeChecked();
  });

  it("offers display settings before any workspace exists", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/workspaces": page([]) });
    renderApp("/-/settings/display");
    expect(await screen.findByRole("switch", { name: "Show the task board in the sidebar" })).toBeChecked();
    const sections = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(sections).getByRole("link", { name: "Display" })).toHaveAttribute("aria-current", "page");
  });

  it("shows the workspace's repositories apart from Branvil's members", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([REPO, { ...REPO, path: "beta/x" }]),
    });
    renderApp("/-/settings");
    const sections = await screen.findByRole("navigation", { name: "Settings sections" });
    expect(await within(sections).findByRole("link", { name: "acme" })).toHaveAttribute("aria-current", "page");
    expect(within(sections).getByRole("link", { name: "Members" })).toHaveAttribute("href", "/-/settings/members");
    // Only this workspace's repositories.
    const list = await screen.findByRole("list", { name: "Repositories" });
    expect(within(list).getAllByRole("listitem").map((li) => within(li).getByText(/\//, { selector: "strong" }).textContent)).toEqual(["acme/demo"]);
  });

  it("lists the owner, agents, and runners and creates agents or runners", async () => {
    const aiko = { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [], reviewers: [] };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/members": page([ME, aiko]),
      "POST /api/v1/members": json(201, { name: "rin", display_name: "rin", kind: "runner" }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/members");

    const list = await screen.findByRole("list", { name: "Members" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      expect.stringContaining("Owner (owner)"),
      expect.stringContaining("Aiko (aiko)"),
    ]);
    await user.click(screen.getByRole("button", { name: "Create member" }));
    const form = await screen.findByRole("form", { name: "Create member" });
    await user.type(within(form).getByRole("textbox", { name: "Name" }), "rin");
    // Only agents and runners can be created: there is one owner.
    await user.click(within(form).getByRole("combobox", { name: "Kind" }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["agent", "runner"]);
    await user.click(screen.getByRole("option", { name: "runner" }));
    await user.click(within(form).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(fake.calls("POST", "/api/v1/members")).toHaveLength(1));
    expect(fake.calls("POST", "/api/v1/members")[0]?.body).toEqual({ name: "rin", display_name: "", kind: "runner" });
  });

  it("creates a workspace from the sidebar's switcher and shows its task flow", async () => {
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([]),
      "POST /api/v1/workspaces": json(201, { name: "beta", lanes: [] }),
      "GET /api/v1/workspaces/beta": json(200, { name: "beta", lanes: ["new"] }),
      "GET /api/v1/workspaces/beta/board/cards": page([]),
    });
    const user = userEvent.setup();
    renderApp("/-/settings");
    await choose(user, "Workspace", "+ New workspace…");
    const dialog = await screen.findByRole("dialog", { name: "New workspace" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "beta");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(fake.calls("POST", "/api/v1/workspaces")).toHaveLength(1));
    expect(fake.calls("POST", "/api/v1/workspaces")[0]?.body).toEqual({ name: "beta" });
    await waitFor(() => expect(window.location.pathname).toBe("/beta"));
  });

  it("creates a repository in the shown workspace with the labels its tickets may carry", async () => {
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([]),
      "POST /api/v1/repos": json(201, {}),
    });
    const user = userEvent.setup();
    renderApp("/-/settings");
    await user.click(await screen.findByRole("button", { name: "Create repository" }));
    const form = await screen.findByRole("form", { name: "Create repository" });
    await user.type(within(form).getByRole("textbox", { name: "Name" }), "demo");
    await addPills(user, "Ticket labels", ["bug", "ui"]);
    await user.click(within(form).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(fake.calls("POST", "/api/v1/repos")).toHaveLength(1));
    expect(fake.calls("POST", "/api/v1/repos")[0]?.body).toEqual({ path: "acme/demo", default_branch: "main", labels: ["bug", "ui"] });
  });

  it("saves a repository's branches, CI and approvals, and labels with the revision the form was filled from", async () => {
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([REPO]),
      "PATCH /api/v1/repos/acme/demo": json(200, { ...REPO, required_checks: ["test", "lint"], ci_network: true, revision: 5 }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByText("http://127.0.0.1:7878/git/acme/demo.git")).toBeInTheDocument();
    // Required checks and approvals live on the CI tab, with the network for jobs.
    const basics = screen.getByRole("tabpanel", { name: "Basics" });
    expect(within(basics).getByLabelText("Protected branches", { selector: "input:not([type=hidden])" })).toBeInTheDocument();
    expect(within(basics).queryByLabelText("Required checks", { selector: "input:not([type=hidden])" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "CI" }));
    const network = screen.getByRole("switch", { name: /^Network for jobs/ });
    expect(network).not.toBeChecked();
    expect(screen.getByText(/public destinations only, never the Branvil machine or private networks/)).toBeInTheDocument();
    await user.click(network);
    await addPills(user, "Required checks", ["test", "lint"]);
    const approvals = screen.getByRole("textbox", { name: "Required approvals" });
    expect(approvals).toHaveValue("1");
    expect(screen.getByText(/must have approved the current head before a merge \(a later comment keeps an approval\); 0 needs none/)).toBeInTheDocument();
    await user.clear(approvals);
    await user.type(approvals, "2");
    await user.click(screen.getByRole("tab", { name: "Labels" }));
    await addPills(user, "Ticket labels", ["ui"]);
    await user.click(screen.getAllByRole("button", { name: "Save" })[0] as HTMLElement);
    await waitFor(() => expect(fake.calls("PATCH", "/api/v1/repos/acme/demo")).toHaveLength(1));
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo")[0]?.body).toEqual({
      revision: 4,
      default_branch: "main",
      protected_branches: ["main"],
      required_checks: ["test", "lint"],
      required_approvals: 2,
      auto_land: false,
      ci_network: true,
      labels: ["bug", "ui"],
    });
  });

  it.each([
    { upstream: null, says: /merges an open pull request by itself \(fast-forward\)/ },
    { upstream: { provider: "github", host: "GitHub", repository: "octo/demo", url: "", reader: { member: "aiko", chosen: false }, following: {} }, says: /publishes an open pull request to GitHub by itself, as its author/ },
  ])("turns automatic landing on from the CI tab and says what it does ($says)", async ({ upstream, says }) => {
    const repo = { ...REPO, upstream };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([repo]),
      "GET /api/v1/members": page([]),
      "PATCH /api/v1/repos/acme/demo": json(200, { ...repo, auto_land: true, revision: 5 }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("tab", { name: "CI" }));
    const ci = screen.getByRole("tabpanel", { name: "CI" });
    const auto = within(ci).getByRole("switch", { name: /^Land automatically/ });
    expect(auto).not.toBeChecked();
    expect(within(ci).getByText(says)).toBeInTheDocument();
    expect(within(ci).getByText(/keep it a draft to hold it/i)).toBeInTheDocument();
    await user.click(auto);
    await user.click(within(ci).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fake.calls("PATCH", "/api/v1/repos/acme/demo")).toHaveLength(1));
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo")[0]?.body).toMatchObject({ revision: 4, auto_land: true });
  });

  it("sets the notification webhook, shows only its host, and removes it", async () => {
    const secret = "https://hooks.example.test/services/T0/hook-71c2";
    const set = { set: true, host: "hooks.example.test", problem: "the webhook answered 500 Internal Server Error" };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/members": page([]),
      "GET /api/v1/admin/webhook": json(200, { set: false, host: null, problem: null }),
      "PUT /api/v1/admin/webhook": json(200, set),
      "DELETE /api/v1/admin/webhook": json(200, { set: false, host: null, problem: null }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings");
    await user.click(await screen.findByRole("link", { name: "Notifications" }));
    expect(await screen.findByText("Not set")).toBeInTheDocument();
    const form = screen.getByRole("form", { name: "Webhook URL" });
    expect(within(form).getByLabelText("Webhook URL")).not.toHaveAttribute("placeholder");
    await user.type(within(form).getByLabelText("Webhook URL"), secret);
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/admin/webhook")).toHaveLength(1));
    expect(fake.calls("PUT", "/api/v1/admin/webhook")[0]?.body).toEqual({ url: secret });
    expect(await screen.findByText("Set (hooks.example.test)")).toBeInTheDocument();
    expect(screen.getByText("The last send failed (retried): the webhook answered 500 Internal Server Error")).toBeInTheDocument();
    // The URL is gone from the page once saved; the field shows that one is saved.
    expect(within(form).getByLabelText("Webhook URL")).toHaveValue("");
    expect(within(form).getByLabelText("Webhook URL")).toHaveAttribute("placeholder", "••••••••••••");
    expect(document.body.innerHTML).not.toContain("hook-71c2");
    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(await screen.findByText("Not set")).toBeInTheDocument();
    expect(within(form).getByLabelText("Webhook URL")).not.toHaveAttribute("placeholder");
    expect(fake.calls("DELETE", "/api/v1/admin/webhook")).toHaveLength(1);
  });

  it("does not save a repository while its required approvals are empty", async () => {
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([REPO]),
      "PATCH /api/v1/repos/acme/demo": json(200, { ...REPO, required_approvals: 0, revision: 5 }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("tab", { name: "CI" }));
    const approvals = screen.getByRole("textbox", { name: "Required approvals" });
    await user.clear(approvals);
    const ci = screen.getByRole("tabpanel", { name: "CI" });
    const save = within(ci).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    await user.click(save);
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo")).toHaveLength(0);
    // 0, typed, is a number and is saved.
    await user.type(approvals, "0");
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(fake.calls("PATCH", "/api/v1/repos/acme/demo")).toHaveLength(1));
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo")[0]?.body).toMatchObject({ required_approvals: 0 });
  });

  it("sets an agent's code host account with a token that is sent once and never shown", async () => {
    const aiko = { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [], reviewers: [] };
    const withAccount = { ...aiko, accounts: [{ provider: "github", login: "aiko-bot", kind: "token", set_at: "2026-10-16T00:00:00Z" }] };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/members": [page([ME, aiko]), page([ME, withAccount])],
      "PUT /api/v1/members/aiko/accounts/github": json(200, withAccount),
      "GET /api/v1/members/aiko/tokens": page([]),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/members");
    const row = (await screen.findByText("Aiko (aiko)")).closest("[role=listitem]") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await user.click(within(row).getByRole("tab", { name: "Accounts on code hosts" }));
    await user.type(within(row).getByLabelText("GitHub token"), "gh-secret");
    await user.click(within(row).getByRole("button", { name: "Save account" }));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/members/aiko/accounts/github")).toHaveLength(1));
    expect(fake.calls("PUT", "/api/v1/members/aiko/accounts/github")[0]?.body).toEqual({ token: "gh-secret" });
    expect((await within(row).findAllByText("aiko-bot", { exact: false })).length).toBeGreaterThan(0);
    expect(screen.queryByDisplayValue("gh-secret")).not.toBeInTheDocument();
  });

  it("sets a member's picture from a file, shows it wherever the member appears, and removes it", async () => {
    const aiko = { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [], reviewers: [], avatar_url: null };
    const pictured = { ...aiko, avatar_url: "/api/v1/members/aiko/avatar?v=1234" };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/members": [page([ME, aiko]), page([ME, pictured]), page([ME, aiko])],
      "PUT /api/v1/members/aiko/avatar": json(200, pictured),
      "DELETE /api/v1/members/aiko/avatar": json(200, aiko),
      "GET /api/v1/members/aiko/tokens": page([]),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/members");
    const row = (await screen.findByText("Aiko (aiko)")).closest("[role=listitem]") as HTMLElement;
    expect(within(row).queryByRole("img")).not.toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    expect(within(row).queryByRole("button", { name: "Remove the picture" })).not.toBeInTheDocument();
    const file = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "aiko.png", { type: "image/png" });
    await user.upload(row.querySelector("input[type=file]") as HTMLInputElement, file);
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/members/aiko/avatar")).toHaveLength(1));
    const sent = fake.calls("PUT", "/api/v1/members/aiko/avatar")[0];
    expect(sent?.body).toBe(file);
    expect(sent?.headers["Content-Type"]).toBe("image/png");
    // Every avatar of the member (the row's and the editor's) shows the picture.
    await waitFor(() => expect(within(row).getAllByRole("img").map((i) => i.getAttribute("src"))).toEqual([pictured.avatar_url, pictured.avatar_url]));

    await user.click(within(row).getByRole("button", { name: "Remove the picture" }));
    await waitFor(() => expect(fake.calls("DELETE", "/api/v1/members/aiko/avatar")).toHaveLength(1));
    await waitFor(() => expect(within(row).queryByRole("img")).not.toBeInTheDocument());
  });

  it("chooses who is asked to review every pull request a member opens, one at a time", async () => {
    const aiko = { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [], reviewers: [] };
    const ken = { name: "ken", display_name: "Ken", kind: "agent", accounts: [], reviewers: [] };
    const ci = { name: "ci", display_name: "CI", kind: "runner", accounts: [], reviewers: [] };
    const asked = { ...aiko, reviewers: ["ken"] };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/members": [page([ME, aiko, ken, ci]), page([ME, asked, ken, ci]), page([ME, aiko, ken, ci])],
      "PUT /api/v1/members/aiko/reviewers/ken": json(200, asked),
      "DELETE /api/v1/members/aiko/reviewers/ken": json(200, aiko),
      "GET /api/v1/members/aiko/tokens": page([]),
      "GET /api/v1/members/ci/tokens": page([]),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/members");
    const row = (await screen.findByText("Aiko (aiko)")).closest("[role=listitem]") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await user.click(within(row).getByRole("tab", { name: "Reviewers" }));
    expect(within(row).getByText("No one is asked yet.")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Reviewers of aiko's pull requests: no one — click to change" }));
    // The member itself and runners are not offered.
    expect(await screen.findByRole("menuitem", { name: /^owner/ })).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByRole("menuitem", { name: /^aiko/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^ci/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: /^ken/ }));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/members/aiko/reviewers/ken")).toHaveLength(1));
    expect(fake.calls("PUT", "/api/v1/members/aiko/reviewers/ken")[0]?.headers["X-Branvil-CSRF"]).toBe("csrf-123");
    expect(await within(row).findByRole("button", { name: "Reviewers of aiko's pull requests: ken — click to change" })).toBeInTheDocument();
    expect(within(row).getByText("ken", { selector: "p" })).toBeInTheDocument();
    expect(await screen.findByRole("menuitem", { name: /^ken/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("menuitem", { name: /^ken/ }));
    await waitFor(() => expect(fake.calls("DELETE", "/api/v1/members/aiko/reviewers/ken")).toHaveLength(1));
    expect(await within(row).findByText("No one is asked yet.")).toBeInTheDocument();

    // A runner opens no pull requests: it has no reviewers.
    const runner = screen.getByText("CI (ci)").closest("[role=listitem]") as HTMLElement;
    await user.click(within(runner).getByRole("button", { name: "Edit" }));
    expect(within(runner).queryByRole("tab", { name: "Reviewers" })).not.toBeInTheDocument();
  });

  it("switches an account between a token and a GitHub App and sends each form's credential", async () => {
    const aiko = { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [{ provider: "github", login: "aiko-bot", kind: "token", set_at: "2026-10-16T00:00:00Z" }], reviewers: [] };
    const asApp = { ...aiko, accounts: [{ provider: "github", login: "octo-app[bot]", kind: "app", set_at: "2026-10-16T01:00:00Z" }] };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/members": [page([ME, aiko]), page([ME, asApp])],
      "PUT /api/v1/members/aiko/accounts/github": json(200, asApp),
      "GET /api/v1/members/aiko/tokens": page([]),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/members");
    const row = (await screen.findByText("Aiko (aiko)")).closest("[role=listitem]") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await user.click(within(row).getByRole("tab", { name: "Accounts on code hosts" }));
    // The account's kind is shown and the form starts on it.
    expect(within(row).getAllByText("Token").length).toBeGreaterThan(0);
    expect(within(row).getByRole("radio", { name: "Token" })).toBeChecked();
    // The saved token is never shown; its field says one is saved.
    expect(within(row).getByLabelText("Replace the GitHub token")).toHaveAttribute("placeholder", "••••••••••••");

    // The app form: nothing is sent until every field is filled.
    await user.click(within(row).getByRole("radio", { name: "GitHub App" }));
    expect(within(row).queryByLabelText("Replace the GitHub token")).not.toBeInTheDocument();
    expect(within(row).getByLabelText("Private key (PEM)")).not.toHaveAttribute("placeholder");
    const save = within(row).getByRole("button", { name: "Save account" });
    await user.type(within(row).getByLabelText("App ID"), " 4242 ");
    await user.type(within(row).getByLabelText("Installation ID"), "777");
    expect(save).toBeDisabled();
    // The key comes from a chosen file (or is pasted).
    const pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n";
    await user.upload(within(row).getByLabelText("Choose the key file"), new File([pem], "app.pem"));
    await waitFor(() => expect(within(row).getByLabelText("Private key (PEM)")).toHaveValue(pem));
    await user.click(save);
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/members/aiko/accounts/github")).toHaveLength(1));
    expect(fake.calls("PUT", "/api/v1/members/aiko/accounts/github")[0]?.body).toEqual({
      app: { app_id: "4242", installation_id: "777", private_key: pem },
    });
    // Saved: the login is the app's bot, and the key is gone from the page.
    expect((await within(row).findAllByText("octo-app[bot]")).length).toBeGreaterThan(0);
    expect(within(row).getByLabelText("Private key (PEM)")).toHaveValue("");
    expect(within(row).getByLabelText("Private key (PEM)")).toHaveAttribute("placeholder", "••••••••••••");
    expect(screen.queryByDisplayValue(pem)).not.toBeInTheDocument();

    // Back to a token: no token is saved now, and the token form sends only the token.
    await user.click(within(row).getByRole("radio", { name: "Token" }));
    expect(within(row).getByLabelText("Replace the GitHub token")).not.toHaveAttribute("placeholder");
    await user.type(within(row).getByLabelText("Replace the GitHub token"), "gh-new");
    await user.click(within(row).getByRole("button", { name: "Save account" }));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/members/aiko/accounts/github")).toHaveLength(2));
    expect(fake.calls("PUT", "/api/v1/members/aiko/accounts/github")[1]?.body).toEqual({ token: "gh-new" });
  });

  it("sets a repository's upstream, shows what following found and who reads it, and says when nobody can read it", async () => {
    const repo = { ...REPO, labels: [] };
    const following = {
      ...repo,
      upstream: {
        provider: "github",
        host: "GitHub",
        repository: "octo/demo",
        url: "https://github.com/octo/demo",
        reader: { member: "aiko", chosen: false },
        following: { tip: "a".repeat(40), checked_at: "2026-10-16T00:00:00Z", followed_at: null, error: null, stopped: null },
      },
      revision: 5,
    };
    const account = { provider: "github", login: "x", set_at: "2026-10-16T00:00:00Z" };
    const kenReads = { ...following, upstream: { ...following.upstream, reader: { member: "ken", chosen: true } }, revision: 6 };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": [page([repo]), page([following]), page([following]), page([kenReads])],
      "GET /api/v1/members": page([
        { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [account] },
        { name: "ken", display_name: "Ken", kind: "agent", accounts: [account] },
        { name: "mia", display_name: "Mia", kind: "agent", accounts: [], reviewers: [] },
      ]),
      "PUT /api/v1/repos/acme/demo/upstream": [
        { status: 409, body: { error: { code: "no_account", message: "No member has an account." } } },
        json(200, following),
        json(200, kenReads),
      ],
    });
    const user = userEvent.setup();
    renderApp("/-/settings");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("tab", { name: "Upstream" }));
    expect(screen.getByText("No upstream: pull requests are merged here.")).toBeInTheDocument();
    const form = screen.getByRole("form", { name: "Set upstream" });
    await user.type(within(form).getByRole("textbox", { name: "Repository there" }), "octo/demo");
    await user.click(within(form).getByRole("button", { name: "Set upstream" }));
    expect(
      await screen.findByText("Set a member's GitHub account under Members first (an agent's bot account is enough): it reads the upstream."),
    ).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Set upstream" }));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/repos/acme/demo/upstream")).toHaveLength(2));
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/upstream")[1]?.body).toEqual({ provider: "github", repository: "octo/demo" });
    expect(await screen.findByRole("link", { name: "GitHub octo/demo" })).toHaveAttribute("href", "https://github.com/octo/demo");
    expect(screen.getByText("main follows")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "a".repeat(10) })).toHaveAttribute("href", `/acme/demo/-/commits/${following.upstream.following.tip}`);
    await user.click(screen.getByRole("tab", { name: "Basics" }));
    expect(screen.getByRole("textbox", { name: "Default branch" })).toHaveAttribute("readonly");
    await user.click(screen.getByRole("tab", { name: "Upstream" }));

    // Who reads it, and changing it explicitly (only members with an account are offered).
    expect(screen.getByText("aiko", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("(picked)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change" })).toBeDisabled();
    await user.click(screen.getByRole("combobox", { name: "Change the reading account" }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["aiko (x)", "ken (x)", "Pick again"]);
    await user.click(screen.getByRole("option", { name: "ken (x)" }));
    await user.click(screen.getByRole("button", { name: "Change" }));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/repos/acme/demo/upstream")).toHaveLength(3));
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/upstream")[2]?.body).toEqual({
      provider: "github",
      repository: "octo/demo",
      reader: "ken",
    });
    expect(await screen.findByText("(chosen)")).toBeInTheDocument();
  });
});
