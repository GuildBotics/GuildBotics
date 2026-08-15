import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { readStackContext } from "./stack-context";

// Journeys ⑦–⑩: sharing one workspace, against the REAL backend and REAL Git.
//
// The "sync" harness gives this stack its own temp HOME, so the hub it creates
// (`$HOME/.guildbotics/hub`) belongs to this run alone. The hub is on the same
// machine as the workspace, which is a supported configuration and removes SSH
// from the picture: what stays under test is the part SSH only transports —
// the repositories, the reconciliation rules, and what the UI says about them.
//
// A second machine is represented by a plain `git` client pushing to the hub.
// That is exactly what the far side of synchronization is, so the journeys that
// need someone else to get there first are real rather than simulated. What one
// process genuinely cannot show — two queues converging on their own — is left
// to the multi-machine rig.
//
// ⑦ enable synchronization on an existing workspace, then take a copy of it
// ⑧ a change that reaches the hub first wins; the later one is set aside
// ⑨ rebuild the hub elsewhere and reconnect to it
// ⑩ a change pushed straight to the hub arrives without anyone asking

const ctx = readStackContext("sync");
const PROJECT_YML = join(ctx.configDir, "team", "project.yml");
/** `FALLBACK_INTERVAL_SECONDS` in `guildbotics/sync/manager.py`, plus a cycle. */
const FALLBACK_WINDOW_MS = 90_000;

function hubRoot(): string {
  return join(ctx.homeDir, ".guildbotics", "hub");
}

function hubRepository(workspaceId: string): string {
  return join(hubRoot(), "workspaces", workspaceId, "repository.git");
}

/** The same call, but tolerant of a repository that is not ready yet. */
function gitOrEmpty(cwd: string, ...args: string[]): string {
  try {
    return git(cwd, ...args);
  } catch {
    return "";
  }
}

function git(cwd: string, ...args: string[]): string {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf-8",
    env: {
      ...process.env,
      GIT_AUTHOR_NAME: "e2e",
      GIT_AUTHOR_EMAIL: "e2e@example.invalid",
      GIT_COMMITTER_NAME: "e2e",
      GIT_COMMITTER_EMAIL: "e2e@example.invalid",
    },
  });
}

async function api(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`http://${ctx.host}:${ctx.backendPort}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-GuildBotics-Session-Token": ctx.token,
      ...(init.headers ?? {}),
    },
  });
}

async function syncStatus(): Promise<{
  enabled: boolean;
  workspace_id: string | null;
  state: string;
}> {
  return (await api("/workspace/sync")).json();
}

async function openSyncSettings(page: Page): Promise<void> {
  await page.goto("/#/setup?section=sync");
  await expect(page.getByRole("heading", { name: "Sync and devices" })).toBeVisible();
}

/** Reach the hub on this machine: an empty address means no network and no host key. */
async function lookUpLocalHub(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Look up" }).click();
}

/**
 * A second machine, standing in as a bare Git client.
 *
 * Returns the commit it put on the hub, so a caller can assert against what the
 * far side actually holds rather than against its own expectations.
 */
function pushFromAnotherDevice(workspaceId: string, description: string): string {
  const clone = mkdtempSync(join(tmpdir(), "guildbotics-e2e-other-device-"));
  git(clone, "clone", hubRepository(workspaceId), ".");
  const project = join(clone, "config", "team", "project.yml");
  writeFileSync(
    project,
    readFileSync(project, "utf-8").replace(/^description: .*$/m, `description: ${description}`),
  );
  git(clone, "add", "config/team/project.yml");
  git(clone, "commit", "-m", "from another device");
  git(clone, "push", "origin", "HEAD:main");
  const head = git(clone, "rev-parse", "HEAD").trim();
  rmSync(clone, { recursive: true, force: true });
  return head;
}

test.describe.configure({ mode: "serial" });

test("⑦ enables synchronization on an existing workspace", async ({ page }) => {
  await openSyncSettings(page);

  await page.getByRole("button", { name: "Host the hub here" }).click();
  await expect(page.getByText(/Hosting the hub/)).toBeVisible();

  await lookUpLocalHub(page);
  await page.getByRole("button", { name: "Register this workspace on the hub" }).click();

  await expect(page.getByRole("heading", { name: "Connected hub" })).toBeVisible({
    timeout: 30_000,
  });
  const status = await syncStatus();
  expect(status.enabled).toBe(true);
  expect(status.workspace_id).toBeTruthy();

  // The hub holds the workspace's config, not just an empty repository.
  const workspaceId = status.workspace_id as string;
  await expect
    .poll(() => git(hubRepository(workspaceId), "ls-tree", "-r", "--name-only", "main"), {
      timeout: 30_000,
    })
    .toContain("config/team/project.yml");
});

test("⑦ the sidebar reports the workspace as shared", async ({ page }) => {
  await page.goto("/#/service");

  await expect(page.getByRole("button", { name: /Synchronization:/ })).toBeVisible({
    timeout: 30_000,
  });
});

test("⑦ takes a copy of the workspace into a new directory", async ({ page }) => {
  const status = await syncStatus();
  const destination = mkdtempSync(join(tmpdir(), "guildbotics-e2e-copy-"));
  // The clone endpoint refuses a directory that already holds a workspace, so
  // the copy goes into a fresh child of it.
  const target = join(destination, "workspace");

  const response = await api("/workspace/sync/clone", {
    method: "POST",
    body: JSON.stringify({
      hub: { endpoint: "" },
      workspace_id: status.workspace_id,
      workspace_dir: target,
    }),
  });

  expect(response.status).toBe(200);
  expect(existsSync(join(target, ".guildbotics", "config", "team", "project.yml"))).toBe(true);
  // The copy carries the same workspace identity, which is what makes it the
  // same workspace rather than a new one that happens to look alike.
  const copied = JSON.parse(
    readFileSync(join(target, ".guildbotics", "state", "workspace.json"), "utf-8"),
  );
  expect(copied.workspace_id).toBe(status.workspace_id);

  // Put the backend back on the original workspace for the journeys below.
  await api("/workspace", {
    method: "POST",
    body: JSON.stringify({ workspace_dir: ctx.workspaceDir }),
  });
  rmSync(destination, { recursive: true, force: true });
  await page.goto("/#/service");
});

test("⑧ keeps the change that reached the hub first and sets the other aside", async ({ page }) => {
  // What this journey is about is the rule, not the timer, so it asks for a
  // cycle the way the user can ("Try again") instead of waiting one out.
  // Journey ⑩ below is the one that proves the timer.
  const { workspace_id: workspaceId } = await syncStatus();
  pushFromAnotherDevice(workspaceId as string, "from the other device");

  // Now change the same file here, after the hub has already moved on.
  writeFileSync(
    PROJECT_YML,
    readFileSync(PROJECT_YML, "utf-8").replace(
      /^description: .*$/m,
      "description: from this device",
    ),
  );

  await api("/workspace/sync/retry", { method: "POST" });

  // The hub's version of the contended file is what survives, there and here.
  // The hub's head may well move past their commit: whatever else this device
  // was carrying that does not collide is reapplied on top and sent.
  await expect
    .poll(() => git(hubRepository(workspaceId as string), "show", "main:config/team/project.yml"), {
      timeout: 30_000,
    })
    .toContain("from the other device");
  await expect
    .poll(() => readFileSync(PROJECT_YML, "utf-8"), { timeout: 30_000 })
    .toContain("from the other device");

  // The change that lost is kept on this device, and named in Activity.
  const refs = git(
    join(ctx.workspaceDir, ".guildbotics"),
    "for-each-ref",
    "refs/guildbotics/rejected",
  );
  expect(refs.trim()).not.toBe("");

  await page.goto("/#/activity");
  await expect(page.getByRole("button", { name: /Update not applied/ })).toBeVisible({
    timeout: 30_000,
  });
});

test("⑧ the set aside change is described but never handed over", async ({ page }) => {
  await page.goto("/#/activity");
  const pin = page.getByRole("button", { name: /Update not applied/ }).first();
  await pin.hover();

  await expect(page.getByText("This change was not applied")).toBeVisible();
  await expect(page.getByText("config/team/project.yml").first()).toBeVisible();
  // What it held is not shown here, only where to find it.
  await expect(page.getByText(/recovery steps in the README/)).toBeVisible();
});

test("⑩ takes in a change pushed straight to the hub, with nothing asked of the user", async ({
  page,
}) => {
  // There is no push notification in this issue, so the queue's own periodic
  // check is the only thing that can notice. This is the state the product
  // ships in, not a fault being injected.
  test.setTimeout(FALLBACK_WINDOW_MS + 60_000);
  const { workspace_id: workspaceId } = await syncStatus();
  pushFromAnotherDevice(workspaceId as string, "arrived on its own");

  await page.goto("/#/service");

  await expect
    .poll(() => readFileSync(PROJECT_YML, "utf-8"), { timeout: FALLBACK_WINDOW_MS })
    .toContain("arrived on its own");
});

test("⑨ reconnects to a hub rebuilt somewhere else", async ({ page }) => {
  // Reconnecting stops the queue first, and the queue is at that moment failing
  // against a hub that no longer exists, so this takes more than one cycle.
  test.setTimeout(180_000);
  const before = await syncStatus();
  const workspaceId = before.workspace_id as string;

  // The hub is lost. Everything it held still exists in this workspace's own
  // repository, which is what makes rebuilding possible at all.
  rmSync(hubRoot(), { recursive: true, force: true });

  await openSyncSettings(page);
  await page.getByRole("button", { name: "Host the hub here" }).click();
  await expect(page.getByText(/Hosting the hub/)).toBeVisible();

  await page.getByRole("button", { name: "Connect to a different hub" }).click();
  await lookUpLocalHub(page);
  await page.getByRole("button", { name: "Register this workspace on the hub" }).click();

  await expect
    .poll(() => gitOrEmpty(hubRepository(workspaceId), "ls-tree", "-r", "--name-only", "main"), {
      timeout: 120_000,
    })
    .toContain("config/team/project.yml");

  const after = await syncStatus();
  expect(after.enabled).toBe(true);
  // Rebuilding does not re-identify the workspace; the copies still match.
  expect(after.workspace_id).toBe(workspaceId);
});
