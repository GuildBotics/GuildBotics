import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CiRunner, CiSettings } from "../api/types";
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

const LIMITS = {
  max_cpus: 4,
  max_memory_mib: 4096,
  work_quota_mib: 16384,
  max_timeout_secs: 3600,
  cache_max_mib: 16384,
  max_jobs: 1,
  memory_reserve_percent: 10,
  cpu_multiplier_percent: 0,
};
const HUB_RUNNER: CiRunner = {
  name: "branvil-runner",
  connection: "online",
  last_seen_at: "2026-10-18T05:00:00.000Z",
  executor: "microsandbox",
  executor_state: "ready",
  executor_detail: null,
  architecture: "arm64",
  machine: null,
  resource_wait: null,
  version: "hub-build",
  version_matches: true,
  max_jobs: 1,
  running_jobs: 1,
  missing_images: [],
  limits: LIMITS,
  limits_source: "default",
};
const REMOTE_RUNNER: CiRunner = {
  ...HUB_RUNNER,
  name: "desktop-runner",
  connection: "offline",
  architecture: "amd64",
  version: "older-build",
  version_matches: false,
  running_jobs: 0,
  missing_images: ["debian:bookworm"],
  limits: { ...LIMITS, max_cpus: 6, max_jobs: 2 },
  limits_source: "runner",
};
const NEW_RUNNER: CiRunner = {
  name: "new-runner",
  connection: "never_seen",
  last_seen_at: null,
  executor: null,
  executor_state: null,
  executor_detail: null,
  architecture: null,
  version: null,
  version_matches: null,
  max_jobs: null,
  running_jobs: 0,
  machine: null,
  resource_wait: null,
  missing_images: null,
  limits: LIMITS,
  limits_source: "default",
};

const CI = {
  runner: {
    mode: "auto",
    state: "ready",
    detail: "1 runner(s) connected.",
    runners: [HUB_RUNNER],
  },
  executor: {
    msb: "/Users/u/.guildbotics/data/msb/bin/msb",
    problem: null,
    home: "/Users/u/.guildbotics/runner/msb-home",
  },
  limits: LIMITS,
  images: { error: null, cached: [CACHED], missing: [] },
  operation: null,
  repositories: [
    { path: "acme/demo", ci_network: false },
    { path: "acme/web", ci_network: true },
  ],
};

const withImages = (images: Partial<CiSettings["images"]>, operation: unknown = null) => ({
  ...CI,
  images: { ...CI.images, ...images },
  operation,
});

const RUNNING = {
  kind: "load",
  target: "/Users/u/image.tar",
  state: "running",
  detail: null,
  started_at: "2026-10-18T06:00:00Z",
  finished_at: null,
  loaded: [],
};

describe("CI settings", () => {
  it("shows the blocked job, runner and resource shortfall, and reports unknown capacity", async () => {
    const resource_wait = {
      attempt_id: "att_waiting",
      repo: "acme/demo",
      job: "rust-test",
      cpu_shortfall: 2,
      memory_shortfall_mib: 2048,
    };
    const machine = { logical_cpus: 8, total_memory_mib: 16384, available_memory_mib: 1024 };
    fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, {
        ...CI,
        runner: {
          ...CI.runner,
          runners: [{ ...HUB_RUNNER, machine, resource_wait }, REMOTE_RUNNER],
        },
      }),
    });
    renderApp("/-/settings/ci");
    expect(await screen.findByText("Machine capacity is unknown")).toBeInTheDocument();
    expect(
      screen.getByText("8 logical CPUs, 16384 MiB memory, 1024 MiB available"),
    ).toBeInTheDocument();
    const queue = screen.getByRole("region", { name: "CI jobs waiting for capacity" });
    expect(within(queue).getByRole("link", { name: "acme/demo · rust-test" })).toHaveAttribute(
      "href",
      "/acme/demo/-/jobs/att_waiting",
    );
    expect(queue).toHaveTextContent("branvil-runner is short by 2 vCPUs and 2048 MiB of memory");
  });
  it.each([
    ["en", 1, 0, "branvil-runner has a vCPU shortfall of 1"],
    ["en", 2, 0, "branvil-runner has a vCPU shortfall of 2"],
    ["en", 0, 2048, "branvil-runner is short by 2048 MiB of memory"],
    ["ja", 2, 0, "branvil-runner で vCPU 2 不足"],
    ["ja", 0, 2048, "branvil-runner でメモリ 2048 MiB 不足"],
  ] as const)(
    "shows only missing resources in %s when short by %i vCPUs and %i MiB",
    async (language, cpu_shortfall, memory_shortfall_mib, label) => {
      const resource_wait = {
        attempt_id: "att_waiting",
        repo: "acme/demo",
        job: "rust-test",
        cpu_shortfall,
        memory_shortfall_mib,
      };
      fakeApi({
        ...SHELL,
        "GET /api/v1/admin/ci": json(200, {
          ...CI,
          runner: { ...CI.runner, runners: [{ ...HUB_RUNNER, resource_wait }] },
        }),
      });
      renderApp("/-/settings/ci", language);
      const link = await screen.findByRole("link", { name: "acme/demo · rust-test" });
      expect(link.parentElement?.textContent).toBe(`acme/demo · rust-test — ${label}`);
    },
  );
  it("shows a commit headroom shortfall on its own", async () => {
    const resource_wait = {
      attempt_id: "att_waiting",
      repo: "acme/demo",
      job: "rust-test",
      cpu_shortfall: 0,
      memory_shortfall_mib: 0,
      commit_shortfall_mib: 4096,
    };
    fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, {
        ...CI,
        runner: { ...CI.runner, runners: [{ ...HUB_RUNNER, resource_wait }] },
      }),
    });
    renderApp("/-/settings/ci");
    const link = await screen.findByRole("link", { name: "acme/demo · rust-test" });
    expect(link.parentElement?.textContent).toBe(
      "acme/demo · rust-test — branvil-runner is short by 4096 MiB of commit headroom",
    );
  });
  it("shows each part of the settings in its own card to the owner", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, CI) });
    renderApp("/-/settings/ci");
    const sections = await screen.findByRole("navigation", { name: "Settings sections" });
    expect(within(sections).getByRole("link", { name: "CI" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(await screen.findByRole("heading", { name: "CI" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "The runner that runs jobs, what one job may use, and the images jobs run in.",
      ),
    ).toBeInTheDocument();

    const runner = await screen.findByRole("region", { name: "Runner" });
    expect(within(runner).getByText("Ready")).toBeInTheDocument();
    expect(within(runner).getByText("1 runner(s) connected.")).toBeInTheDocument();
    expect(
      within(runner).getByRole("switch", { name: /^Run the runner on the Branvil machine/ }),
    ).toBeChecked();
    expect(
      within(within(runner).getByRole("list", { name: "Runners" })).getByText("branvil-runner"),
    ).toBeInTheDocument();

    const executor = screen.getByRole("region", { name: "Executor" });
    expect(
      within(executor).getByText("/Users/u/.guildbotics/data/msb/bin/msb"),
    ).toBeInTheDocument();
    expect(within(executor).getByText("/Users/u/.guildbotics/runner/msb-home")).toBeInTheDocument();

    // The only Save button is the job resources', inside their card.
    const limits = screen.getByRole("region", { name: "Job resources" });
    expect(screen.getAllByRole("button", { name: "Save" })).toEqual([
      within(limits).getByRole("button", { name: "Save" }),
    ]);
    for (const other of ["Runner", "Executor", "Images", "Network"])
      expect(screen.getByRole("region", { name: other })).not.toContainElement(
        within(limits).getByRole("form"),
      );

    // Each repository links to its editor on the CI tab.
    const network = within(screen.getByRole("region", { name: "Network" })).getByRole("list", {
      name: "Network",
    });
    const [demo, web] = within(network).getAllByRole("listitem") as [HTMLElement, HTMLElement];
    expect(within(demo).getByRole("link", { name: "acme/demo" })).toHaveAttribute(
      "href",
      "/-/settings/repos/acme/demo",
    );
    expect(within(demo).getByText("Not allowed")).toBeInTheDocument();
    expect(within(web).getByText("Allowed")).toBeInTheDocument();
  });

  it("lists the cached images with their size, date, users, and digest, and copies the tag as the reference", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, CI) });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    const images = await screen.findByRole("region", { name: "Images" });
    expect(within(images).queryByRole("alert")).not.toBeInTheDocument();
    const [row] = within(within(images).getByRole("list", { name: "In the cache" })).getAllByRole(
      "listitem",
    ) as [HTMLElement];
    expect(within(row).getByText("guildbotics/agent-environment:dev")).toBeInTheDocument();
    expect(row).toHaveTextContent(
      "3.4 GiB · 10/18/2026 · Used by: sandbox/ci-check (tools, long) · acme/demo (test)",
    );
    expect(within(row).queryByText("Added")).not.toBeInTheDocument();
    // The digest of the version the cache holds on one line, cut in the middle; the full value in its title.
    expect(within(row).getByText(`sha256:44444444…${"4".repeat(9)}`)).toHaveAttribute(
      "title",
      DIGEST,
    );
    // A pipeline names the image by its tag.
    await user.click(within(row).getByRole("button", { name: "Copy reference" }));
    expect(await navigator.clipboard.readText()).toBe("guildbotics/agent-environment:dev");
    expect(await within(row).findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("leaves out who uses an image no pipeline names", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, withImages({ cached: [{ ...CACHED, used_by: [] }] })),
    });
    renderApp("/-/settings/ci");
    const [row] = within(await screen.findByRole("list", { name: "In the cache" })).getAllByRole(
      "listitem",
    ) as [HTMLElement];
    expect(row).toHaveTextContent("3.4 GiB · 10/18/2026");
    expect(row).not.toHaveTextContent("Used by");
  });

  it("invites to add an image when the cache holds none", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, withImages({ cached: [] })) });
    renderApp("/-/settings/ci");
    const images = await screen.findByRole("region", { name: "Images" });
    expect(within(images).getByText("Add an image for jobs to run in")).toBeInTheDocument();
    expect(
      within(images).getByText(
        "Write the added image's reference as image in .guildbotics-ci.toml.",
      ),
    ).toBeInTheDocument();
    expect(within(images).queryByRole("list")).not.toBeInTheDocument();
    expect(within(images).getByRole("button", { name: "Add image" })).toBeInTheDocument();
  });

  it("warns of each image pipelines name that the cache does not hold", async () => {
    const missing = [
      {
        image: "guildbotics/agent-environment:next",
        repositories: [
          { repository: "GuildBotics/GuildBotics", jobs: ["test", "desktop-quality"] },
        ],
      },
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
    expect(agent).toHaveTextContent(
      "The image that test, desktop-quality of GuildBotics/GuildBotics use is not in the cache",
    );
    expect(within(agent).getByText("guildbotics/agent-environment:next")).toBeInTheDocument();
    expect(
      within(agent).getByText("Jobs fail to start until it is added. Add it with Add image."),
    ).toBeInTheDocument();
    expect(absent).toHaveTextContent(
      "The image that lint of acme/web · docs of acme/demo use is not in the cache",
    );
    expect(within(absent).getByText("alpine:3.20")).toBeInTheDocument();
    expect(
      within(absent).getByText("Jobs fail to start until it is added. Add it with Add image."),
    ).toBeInTheDocument();
    // The cached image is still listed under the warnings.
    expect(within(images).getByRole("list", { name: "In the cache" })).toBeInTheDocument();
  });

  it("adds an image from a file: the source switches the field, and the dialog opens fresh again", async () => {
    const failed = {
      ...RUNNING,
      state: "failed",
      detail: "Not an image archive.",
      finished_at: "2026-10-18T06:00:01Z",
    };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, CI),
      "POST /api/v1/admin/ci/images/load": json(202, { ...CI, operation: failed }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    await user.click(
      within(await screen.findByRole("region", { name: "Images" })).getByRole("button", {
        name: "Add image",
      }),
    );
    let dialog = await screen.findByRole("dialog", { name: "Add image" });
    expect(within(dialog).getByRole("radio", { name: "Pull from a registry" })).toBeChecked();
    expect(within(dialog).getByRole("textbox", { name: "Image reference" })).toHaveAttribute(
      "placeholder",
      "debian:bookworm",
    );
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeDisabled();

    await user.click(
      within(dialog).getByRole("radio", { name: "Load from a file on the Branvil machine" }),
    );
    expect(
      within(dialog).queryByRole("textbox", { name: "Image reference" }),
    ).not.toBeInTheDocument();
    const path = within(dialog).getByRole("textbox", { name: "Absolute path of the archive" });
    expect(path).toHaveAttribute("placeholder", "/path/to/image.tar");
    expect(path).toHaveAccessibleDescription(
      "A docker save or OCI archive (.tar) on the machine that runs Branvil.",
    );
    await user.type(path, " /Users/u/image.tar ");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(fake.calls("POST", "/api/v1/admin/ci/images/load")).toHaveLength(1));
    expect(fake.calls("POST", "/api/v1/admin/ci/images/load")[0]?.body).toEqual({
      path: "/Users/u/image.tar",
    });
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
    const pulled = {
      ...pulling,
      state: "succeeded",
      finished_at: "2026-10-18T06:01:00Z",
      loaded: [{ reference: "debian:bookworm" }],
    };
    const debian = {
      ...CACHED,
      reference: "debian:bookworm",
      digest: DEBIAN_DIGEST,
      size_bytes: 123 * 2 ** 20,
      used_by: [],
    };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": [
        json(200, CI),
        json(200, withImages({ cached: [CACHED, debian] }, pulled)),
      ],
      "POST /api/v1/admin/ci/images/pull": [
        apiError(409, "conflict", "An image operation is running."),
        json(202, { ...CI, operation: pulling }),
      ],
    });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    await user.click(await screen.findByRole("button", { name: "Add image" }));
    const dialog = await screen.findByRole("dialog", { name: "Add image" });
    await user.type(
      within(dialog).getByRole("textbox", { name: "Image reference" }),
      " debian:bookworm ",
    );

    // Another operation started elsewhere: the service refuses, and the dialog says so.
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    expect(
      await within(dialog).findByText(
        "Another image operation is running. Try again when it has finished.",
      ),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(fake.calls("POST", "/api/v1/admin/ci/images/pull")).toHaveLength(2));
    expect(fake.calls("POST", "/api/v1/admin/ci/images/pull")[1]?.body).toEqual({
      reference: "debian:bookworm",
    });
    // While it runs, no other operation can start, and the page polls until it ends.
    expect(
      await within(dialog).findByText("An image is being added: debian:bookworm"),
    ).toBeInTheDocument();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Image reference" }),
      "alpine:3.20",
    );
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
    const [agent, pulledRow] = within(
      screen.getByRole("list", { name: "In the cache" }),
    ).getAllByRole("listitem") as [HTMLElement, HTMLElement];
    expect(within(pulledRow).getByText("Added")).toBeInTheDocument();
    expect(within(agent).queryByText("Added")).not.toBeInTheDocument();
  });

  it("says why Add is busy when an operation started elsewhere is running", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, { ...CI, operation: RUNNING }) });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    const images = await screen.findByRole("region", { name: "Images" });
    expect(
      within(images).getByText("An image is being added: /Users/u/image.tar"),
    ).toBeInTheDocument();
    await user.click(within(images).getByRole("button", { name: "Add image" }));
    const dialog = await screen.findByRole("dialog", { name: "Add image" });
    expect(
      within(dialog).getByText("An image is being added: /Users/u/image.tar"),
    ).toBeInTheDocument();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Image reference" }),
      "debian:bookworm",
    );
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeDisabled();
  });

  it("opens a repository's editor on its CI tab from the network list", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, CI),
      "GET /api/v1/repos": page([
        {
          path: "acme/demo",
          default_branch: "main",
          clone_url: "",
          url: "",
          protected_branches: [],
          responder: { member: "aiko", chosen: false, automatic: "aiko" },
          post_merge_jobs: [],
          ci_network: false,
          labels: [],
          upstream: null,
          revision: 1,
        },
      ]),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    await user.click(await screen.findByRole("link", { name: "acme/demo" }));
    expect(window.location.pathname).toBe("/-/settings/repos/acme/demo");
    expect(await screen.findByRole("tab", { name: "CI", selected: true })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /^Network for jobs/ })).not.toBeChecked();
  });

  it("turns the automatic runner off and keeps its last report", async () => {
    const stopped = {
      ...CI.runner,
      mode: "off",
      state: "no_runner",
      detail: "No runner.",
      runners: [{ ...HUB_RUNNER, connection: "offline" }],
    };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, CI),
      "PUT /api/v1/admin/ci/runner": json(200, { ...CI, runner: stopped }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    await user.click(
      await screen.findByRole("switch", { name: /^Run the runner on the Branvil machine/ }),
    );
    expect(await screen.findByText("Contact lost")).toBeInTheDocument();
    expect(fake.calls("PUT", "/api/v1/admin/ci/runner")[0]?.body).toEqual({ mode: "off" });
    expect(
      screen.getByRole("switch", { name: /^Run the runner on the Branvil machine/ }),
    ).not.toBeChecked();
  });

  it("shows a version mismatch warning for the only online runner when the automatic runner is off", async () => {
    const runner = {
      ...CI.runner,
      mode: "off",
      state: "executor_unavailable",
      detail: "runner build differs",
      version_mismatch: { name: REMOTE_RUNNER.name, version: REMOTE_RUNNER.version },
      runners: [{ ...REMOTE_RUNNER, connection: "online" }],
    };
    fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, { ...CI, runner }),
      "GET /api/v1/admin/diagnostics": json(200, { runner }),
    });
    renderApp("/-/settings/ci");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "desktop-runner reported version older-build, which differs from the Hub.",
    );
  });

  it("shows runner reports and saves the selected offline runner's resources without a global warning", async () => {
    const offlineMatch = { ...HUB_RUNNER, name: "stopped-runner", connection: "offline" };
    const runner = { ...CI.runner, runners: [HUB_RUNNER, REMOTE_RUNNER, NEW_RUNNER, offlineMatch] };
    const saved = { ...REMOTE_RUNNER, limits: { ...REMOTE_RUNNER.limits, max_cpus: 8 } };
    const path = "/api/v1/admin/ci/runners/desktop-runner/limits";
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": json(200, { ...CI, runner }),
      "GET /api/v1/admin/diagnostics": json(200, { runner }),
      [`PUT ${path}`]: json(200, {
        ...CI,
        runner: { ...runner, runners: [HUB_RUNNER, saved, NEW_RUNNER, offlineMatch] },
      }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/ci");
    const list = await screen.findByRole("list", { name: "Runners" });
    const row = (name: string) =>
      within(
        within(list)
          .getAllByRole("listitem")
          .find((item) => within(item).queryByRole("radio", { name }))!,
      );
    const online = row(HUB_RUNNER.name);
    const stopped = row(offlineMatch.name);
    const tone = (label: HTMLElement) =>
      label.closest<HTMLElement>(".mantine-Badge-root")?.style.getPropertyValue("--badge-bg");
    expect(tone(online.getByText("Hub version (hub-build)"))).toContain("success");
    expect(online.getByText("1 / 1 running attempts")).toBeInTheDocument();
    for (const offline of [row(REMOTE_RUNNER.name), stopped]) {
      expect(tone(offline.getByText("Contact lost"))).toContain("warning");
      expect(tone(offline.getByText("microsandbox · Ready"))).toBe("var(--color-bg-hover)");
      expect(offline.getByText("Last report")).toBeInTheDocument();
    }
    expect(tone(stopped.getByText("Hub version (hub-build)"))).toBe("var(--color-bg-hover)");
    expect(
      within(list).getByText("Version differs from the Hub (older-build)"),
    ).toBeInTheDocument();
    expect(within(list).getByText("Missing images for amd64: debian:bookworm")).toBeInTheDocument();
    expect(within(list).getByText("No contact since the Hub started")).toBeInTheDocument();
    await waitFor(() => expect(fake.calls("GET", "/api/v1/admin/diagnostics")).toHaveLength(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("guildbotics runner join")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Set up SSH forwarding before joining" }),
    ).toHaveAttribute("href");

    const limits = () => screen.getByRole("form", { name: "Job resources" });
    expect(within(limits()).getByRole("textbox", { name: "CPUs" })).toHaveValue("4");
    expect(screen.getByText("Hub defaults")).toBeInTheDocument();
    await user.click(within(list).getByRole("radio", { name: "desktop-runner" }));
    const cpus = within(limits()).getByRole("textbox", { name: "CPUs" });
    expect(cpus).toHaveValue("6");
    expect(screen.getByText("Saved for this runner")).toBeInTheDocument();
    expect(within(limits()).getByRole("textbox", { name: "Parallel jobs" })).toHaveValue("2");
    await user.clear(cpus);
    await user.type(cpus, "8");
    await user.click(within(limits()).getByRole("button", { name: "Save" }));
    expect(await within(limits()).findByText("Saved.")).toBeInTheDocument();
    expect(fake.calls("PUT", path)[0]?.body).toEqual(saved.limits);
    await user.click(within(list).getByRole("radio", { name: "new-runner" }));
    expect(within(limits()).getByRole("textbox", { name: "CPUs" })).toHaveValue("4");
  });

  it("follows a runner switched on until it reports", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const starting = {
      ...CI,
      runner: {
        ...CI.runner,
        state: "no_runner",
        detail: "No runner has contacted Branvil.",
        runners: [],
      },
    };
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/admin/ci": [json(200, starting), json(200, CI)],
    });
    renderApp("/-/settings/ci");
    const runner = await screen.findByRole("region", { name: "Runner" });
    expect(within(runner).getByText("No runner")).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/admin/ci")).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(3_000));
    expect(await within(runner).findByText("Ready")).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/admin/ci")).toHaveLength(2);
  });

  it("shows that the service waits for the running jobs before it stops", async () => {
    const stopping = {
      ...CI,
      runner: {
        ...CI.runner,
        state: "stopping",
        detail: "Branvil is stopping (an update or a restart).",
      },
    };
    fakeApi({ ...SHELL, "GET /api/v1/admin/ci": json(200, stopping) });
    renderApp("/-/settings/ci");
    const runner = await screen.findByRole("region", { name: "Runner" });
    expect(within(runner).getByText("Stopping")).toBeInTheDocument();
    expect(
      within(runner).getByText("Branvil is stopping (an update or a restart)."),
    ).toBeInTheDocument();
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
    expect(within(sections).getByRole("link", { name: "Members" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(sections).queryByRole("link", { name: "CI" })).not.toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/admin/ci")).toHaveLength(0);
  });
});
