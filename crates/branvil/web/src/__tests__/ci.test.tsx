import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CiSettings } from "../api/types";
import { apiError, fakeApi, json, ME, renderApp, SESSION } from "../test/fakeApi";

const page = (items: unknown[]) => json(200, { items, next_cursor: null });

const SHELL = {
  "GET /api/v1/session": json(200, SESSION),
  "GET /api/v1/workspaces": page([{ name: "acme", lanes: [] }]),
  "GET /api/v1/admin/diagnostics": json(200, {}),
};

const DIGEST = `sha256:${"4".repeat(64)}`;
const DEBIAN_DIGEST = `sha256:${"d".repeat(64)}`;

const CACHED = {
  reference: "guildbotics/agent-environment:dev",
  digest: DIGEST,
  size_bytes: 3701474304,
  created_at: "2026-10-18T12:00:00Z",
  used_by: [
    { repository: "sandbox/ci-check", jobs: ["tools", "long"] },
    { repository: "acme/demo", jobs: ["test"] },
  ],
};

const CI = {
  runner: {
    mode: "auto",
    state: "ready",
    detail: "1 runner(s) connected.",
    runners: [{ name: "branvil-runner", last_seen_at: "2026-10-18T05:00:00.000Z", executor: "microsandbox", executor_state: "ready", executor_detail: null }],
  },
  executor: { msb: "/Users/u/.guildbotics/data/msb/bin/msb", problem: null, home: "/Users/u/.branvil/msb-home" },
  limits: { max_cpus: 4, max_memory_mib: 4096, max_timeout_secs: 3600, cache_max_mib: 2048, max_jobs: 1 },
  images: { error: null, cached: [CACHED], missing: [] },
  operation: null,
  repositories: [
    { path: "acme/demo", ci_network: false },
    { path: "acme/web", ci_network: true },
  ],
};

const withImages = (images: Partial<CiSettings["images"]>, operation: unknown = null) => ({ ...CI, images: { ...CI.images, ...images }, operation });

const RUNNING = { kind: "load", target: "/Users/u/image.tar", state: "running", detail: null, started_at: "2026-10-18T06:00:00Z", finished_at: null, loaded: [] };

describe("CI settings", () => {
  it("shows each part of the settings in its own card to the owner", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, CI) });
    renderApp("/-/settings/ci");
    const sections = await screen.findByRole("navigation", { name: "Settings sections" });
    expect(within(sections).getByRole("link", { name: "CI" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByRole("heading", { name: "CI" })).toBeInTheDocument();
    expect(screen.getByText("The runner that runs jobs, what one job may use, and the images jobs run in.")).toBeInTheDocument();

    const runner = await screen.findByRole("region", { name: "Runner" });
    expect(within(runner).getByText("Ready")).toBeInTheDocument();
    expect(within(runner).getByText("1 runner(s) connected.")).toBeInTheDocument();
    expect(within(runner).getByRole("switch", { name: /^Run the runner on the Branvil machine/ })).toBeChecked();
    expect(within(within(runner).getByRole("list", { name: "Connected runners" })).getByText("branvil-runner")).toBeInTheDocument();

    const executor = screen.getByRole("region", { name: "Executor" });
    expect(within(executor).getByText("/Users/u/.guildbotics/data/msb/bin/msb")).toBeInTheDocument();
    expect(within(executor).getByText("/Users/u/.branvil/msb-home")).toBeInTheDocument();

    // The only Save button is the job resources', inside their card.
    const limits = screen.getByRole("region", { name: "Job resources" });
    expect(screen.getAllByRole("button", { name: "Save" })).toEqual([within(limits).getByRole("button", { name: "Save" })]);
    for (const other of ["Runner", "Executor", "Images", "Network"]) expect(screen.getByRole("region", { name: other })).not.toContainElement(within(limits).getByRole("form"));

    // Each repository links to its editor on the CI tab.
    const network = within(screen.getByRole("region", { name: "Network" })).getByRole("list", { name: "Network" });
    const [demo, web] = within(network).getAllByRole("listitem") as [HTMLElement, HTMLElement];
    expect(within(demo).getByRole("link", { name: "acme/demo" })).toHaveAttribute("href", "/-/settings/repos/acme/demo");
    expect(within(demo).getByText("Not allowed")).toBeInTheDocument();
    expect(within(web).getByText("Allowed")).toBeInTheDocument();
  });

  it("lists the cached images with their size, date, users, and digest, and copies the tag as the reference", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, CI) });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    const images = await screen.findByRole("region", { name: "Images" });
    expect(within(images).queryByRole("alert")).not.toBeInTheDocument();
    const [row] = within(within(images).getByRole("list", { name: "In the cache" })).getAllByRole("listitem") as [HTMLElement];
    expect(within(row).getByText("guildbotics/agent-environment:dev")).toBeInTheDocument();
    expect(row).toHaveTextContent("3.4 GiB · 10/18/2026 · Used by: sandbox/ci-check (tools, long) · acme/demo (test)");
    expect(within(row).queryByText("Added")).not.toBeInTheDocument();
    // The digest of the version the cache holds on one line, cut in the middle; the full value in its title.
    expect(within(row).getByText(`sha256:44444444…${"4".repeat(9)}`)).toHaveAttribute("title", DIGEST);
    // A pipeline names the image by its tag.
    await user.click(within(row).getByRole("button", { name: "Copy reference" }));
    expect(await navigator.clipboard.readText()).toBe("guildbotics/agent-environment:dev");
    expect(await within(row).findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("leaves out who uses an image no pipeline names", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, withImages({ cached: [{ ...CACHED, used_by: [] }] })) });
    renderApp("/-/settings/ci");
    const [row] = within(await screen.findByRole("list", { name: "In the cache" })).getAllByRole("listitem") as [HTMLElement];
    expect(row).toHaveTextContent("3.4 GiB · 10/18/2026");
    expect(row).not.toHaveTextContent("Used by");
  });

  it("invites to add an image when the cache holds none", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, withImages({ cached: [] })) });
    renderApp("/-/settings/ci");
    const images = await screen.findByRole("region", { name: "Images" });
    expect(within(images).getByText("Add an image for jobs to run in")).toBeInTheDocument();
    expect(within(images).getByText("Write the added image's reference as image in .branvil/ci.toml.")).toBeInTheDocument();
    expect(within(images).queryByRole("list")).not.toBeInTheDocument();
    expect(within(images).getByRole("button", { name: "Add image" })).toBeInTheDocument();
  });

  it("warns of each image pipelines name that the cache does not hold", async () => {
    const missing = [
      { image: "guildbotics/agent-environment:next", repositories: [{ repository: "GuildBotics/GuildBotics", jobs: ["test", "desktop-quality"] }] },
      {
        image: "alpine:3.20",
        repositories: [
          { repository: "acme/web", jobs: ["lint"] },
          { repository: "acme/demo", jobs: ["docs"] },
        ],
      },
    ];
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, withImages({ missing })) });
    renderApp("/-/settings/ci");
    const images = await screen.findByRole("region", { name: "Images" });
    const [agent, absent] = within(images).getAllByRole("alert") as [HTMLElement, HTMLElement];
    expect(agent).toHaveTextContent("The image that test, desktop-quality of GuildBotics/GuildBotics use is not in the cache");
    expect(within(agent).getByText("guildbotics/agent-environment:next")).toBeInTheDocument();
    expect(within(agent).getByText("Jobs fail to start until it is added. Add it with Add image.")).toBeInTheDocument();
    expect(absent).toHaveTextContent("The image that lint of acme/web · docs of acme/demo use is not in the cache");
    expect(within(absent).getByText("alpine:3.20")).toBeInTheDocument();
    expect(within(absent).getByText("Jobs fail to start until it is added. Add it with Add image.")).toBeInTheDocument();
    // The cached image is still listed under the warnings.
    expect(within(images).getByRole("list", { name: "In the cache" })).toBeInTheDocument();
  });

  it("adds an image from a file: the source switches the field, and the dialog opens fresh again", async () => {
    const failed = { ...RUNNING, state: "failed", detail: "Not an image archive.", finished_at: "2026-10-18T06:00:01Z" };
    const fake = fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, CI), "POST /api/v1/admin/ci/images/load": json(202, { ...CI, operation: failed }) });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    await user.click(within(await screen.findByRole("region", { name: "Images" })).getByRole("button", { name: "Add image" }));
    let dialog = await screen.findByRole("dialog", { name: "Add image" });
    expect(within(dialog).getByRole("radio", { name: "Pull from a registry" })).toBeChecked();
    expect(within(dialog).getByRole("textbox", { name: "Image reference" })).toHaveAttribute("placeholder", "debian:bookworm");
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeDisabled();

    await user.click(within(dialog).getByRole("radio", { name: "Load from a file on the Branvil machine" }));
    expect(within(dialog).queryByRole("textbox", { name: "Image reference" })).not.toBeInTheDocument();
    const path = within(dialog).getByRole("textbox", { name: "Absolute path of the archive" });
    expect(path).toHaveAttribute("placeholder", "/path/to/image.tar");
    expect(path).toHaveAccessibleDescription("A docker save or OCI archive (.tar) on the machine that runs Branvil.");
    await user.type(path, " /Users/u/image.tar ");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(fake.calls("POST", "/api/v1/admin/ci/images/load")).toHaveLength(1));
    expect(fake.calls("POST", "/api/v1/admin/ci/images/load")[0]?.body).toEqual({ path: "/Users/u/image.tar" });
    expect(await within(dialog).findByText("Not an image archive.")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Add image" }));
    dialog = await screen.findByRole("dialog", { name: "Add image" });
    expect(within(dialog).getByRole("radio", { name: "Pull from a registry" })).toBeChecked();
    expect(within(dialog).getByRole("textbox", { name: "Image reference" })).toHaveValue("");
    expect(within(dialog).queryByText("Not an image archive.")).not.toBeInTheDocument();
  });

  it("pulls an image, follows the running operation to what it added, and marks it in the list", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const pulling = { ...RUNNING, kind: "pull", target: "debian:bookworm" };
    const pulled = { ...pulling, state: "succeeded", finished_at: "2026-10-18T06:01:00Z", loaded: [{ reference: "debian:bookworm" }] };
    const debian = { ...CACHED, reference: "debian:bookworm", digest: DEBIAN_DIGEST, size_bytes: 123 * 2 ** 20, used_by: [] };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": [json(200, CI), json(200, withImages({ cached: [CACHED, debian] }, pulled))],
      "POST /api/v1/admin/ci/images/pull": [apiError(409, "conflict", "An image operation is running."), json(202, { ...CI, operation: pulling })],
    });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    await user.click(await screen.findByRole("button", { name: "Add image" }));
    const dialog = await screen.findByRole("dialog", { name: "Add image" });
    await user.type(within(dialog).getByRole("textbox", { name: "Image reference" }), " debian:bookworm ");

    // Another operation started elsewhere: the service refuses, and the dialog says so.
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    expect(await within(dialog).findByText("Another image operation is running. Try again when it has finished.")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(fake.calls("POST", "/api/v1/admin/ci/images/pull")).toHaveLength(2));
    expect(fake.calls("POST", "/api/v1/admin/ci/images/pull")[1]?.body).toEqual({ reference: "debian:bookworm" });
    // While it runs, no other operation can start, and the page polls until it ends.
    expect(await within(dialog).findByText("An image is being added: debian:bookworm")).toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Image reference" }), "alpine:3.20");
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeDisabled();
    expect(fake.calls("GET", "/api/v1/admin/ci")).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    const added = await within(dialog).findByRole("list", { name: "Added images" });
    expect(fake.calls("GET", "/api/v1/admin/ci")).toHaveLength(2);
    expect(within(added).getByText("Added debian:bookworm")).toBeInTheDocument();
    await user.click(within(added).getByRole("button", { name: "Copy reference" }));
    expect(await navigator.clipboard.readText()).toBe("debian:bookworm");
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeEnabled();

    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const [agent, pulledRow] = within(screen.getByRole("list", { name: "In the cache" })).getAllByRole("listitem") as [HTMLElement, HTMLElement];
    expect(within(pulledRow).getByText("Added")).toBeInTheDocument();
    expect(within(agent).queryByText("Added")).not.toBeInTheDocument();
  });

  it("says why Add is busy when an operation started elsewhere is running", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, { ...CI, operation: RUNNING }) });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    const images = await screen.findByRole("region", { name: "Images" });
    expect(within(images).getByText("An image is being added: /Users/u/image.tar")).toBeInTheDocument();
    await user.click(within(images).getByRole("button", { name: "Add image" }));
    const dialog = await screen.findByRole("dialog", { name: "Add image" });
    expect(within(dialog).getByText("An image is being added: /Users/u/image.tar")).toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Image reference" }), "debian:bookworm");
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeDisabled();
  });

  it("opens a repository's editor on its CI tab from the network list", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, CI),
      "GET /api/v1/repos": page([
        { path: "acme/demo", default_branch: "main", clone_url: "", url: "", protected_branches: [], required_checks: [], ci_network: false, labels: [], upstream: null, revision: 1 },
      ]),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    await user.click(await screen.findByRole("link", { name: "acme/demo" }));
    expect(window.location.pathname).toBe("/-/settings/repos/acme/demo");
    expect(await screen.findByRole("tab", { name: "CI", selected: true })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /^Network for jobs/ })).not.toBeChecked();
  });

  it("turns the runner off and saves the job limits as numbers", async () => {
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, CI),
      "PUT /api/v1/admin/ci/runner": json(200, { ...CI, runner: { ...CI.runner, mode: "off", state: "no_runner", detail: "No runner.", runners: [] } }),
      "PUT /api/v1/admin/ci/limits": json(200, { ...CI, limits: { ...CI.limits, max_cpus: 8 } }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    await user.click(await screen.findByRole("switch", { name: /^Run the runner on the Branvil machine/ }));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/admin/ci/runner")).toHaveLength(1));
    expect(fake.calls("PUT", "/api/v1/admin/ci/runner")[0]?.body).toEqual({ mode: "off" });
    expect(await screen.findByText("No runner is connected.")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /^Run the runner on the Branvil machine/ })).not.toBeChecked();

    const limits = screen.getByRole("form", { name: "Job resources" });
    expect(
      screen.getByText(
        "Every job runs with these CPUs and this memory, in a microVM of its own. Up to the number of parallel jobs run at once, using that many times the CPUs and memory. The time limit caps a pipeline's timeout_secs and is the limit of a job that gives none. Saved values apply from the next job the runner starts (running jobs go on).",
      ),
    ).toBeInTheDocument();
    const cpus = within(limits).getByRole("textbox", { name: "CPUs" });
    await user.clear(cpus);
    await user.type(cpus, "8");
    const jobs = within(limits).getByRole("textbox", { name: "Parallel jobs" });
    await user.clear(jobs);
    await user.type(jobs, "2");
    await user.click(within(limits).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/admin/ci/limits")).toHaveLength(1));
    expect(fake.calls("PUT", "/api/v1/admin/ci/limits")[0]?.body).toEqual({ max_cpus: 8, max_memory_mib: 4096, max_timeout_secs: 3600, cache_max_mib: 2048, max_jobs: 2 });
    expect(await within(limits).findByText("Saved.")).toBeInTheDocument();
  });

  it("follows a runner switched on until it reports", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const starting = { ...CI, runner: { ...CI.runner, state: "no_runner", detail: "No runner has contacted Branvil.", runners: [] } };
    const fake = fakeApi({ ...SHELL, "GET /api/v1/admin/ci": [json(200, starting), json(200, CI)] });
    renderApp("/-/settings/ci");
    const runner = await screen.findByRole("region", { name: "Runner" });
    expect(within(runner).getByText("No runner")).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/admin/ci")).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(3_000));
    expect(await within(runner).findByText("Ready")).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/admin/ci")).toHaveLength(2);
  });

  it("shows that the service waits for the running jobs before it stops", async () => {
    const stopping = { ...CI, runner: { ...CI.runner, state: "stopping", detail: "Branvil is stopping (an update or a restart)." } };
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, stopping) });
    renderApp("/-/settings/ci");
    const runner = await screen.findByRole("region", { name: "Runner" });
    expect(within(runner).getByText("Stopping")).toBeInTheDocument();
    expect(within(runner).getByText("Branvil is stopping (an update or a restart).")).toBeInTheDocument();
  });

  it("is not offered to members other than the owner", async () => {
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/session": json(200, { ...SESSION, me: { ...ME, name: "aiko", kind: "agent" } }),
      "GET /api/v1/members": page([]),
      "GET /api/v1/admin/ci": json(200, CI),
    });
    renderApp("/-/settings/ci");
    const sections = await screen.findByRole("navigation", { name: "Settings sections" });
    expect(within(sections).getByRole("link", { name: "Members" })).toHaveAttribute("aria-current", "page");
    expect(within(sections).queryByRole("link", { name: "CI" })).not.toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/admin/ci")).toHaveLength(0);
  });
});
