import { configureApi, disconnectApi, setWorkspace } from "./client";

const STATIC_TOKEN = import.meta.env.VITE_GUILDBOTICS_API_TOKEN ?? "";
const STATIC_BASE = import.meta.env.VITE_GUILDBOTICS_API_BASE ?? "http://127.0.0.1:8765";

export type CliAgentSkillStatus =
  | "up_to_date"
  | "user_modified"
  | "unmanaged"
  | "missing"
  | "outdated"
  | "agent_home_missing"
  | "error";

export type CliAgentSkillState = {
  agent: string;
  agent_home: string | null;
  skill_path: string | null;
  status: CliAgentSkillStatus;
  can_force_update: boolean;
  error?: string;
};

export type CliAgentSkillStatusesResponse = {
  agents: CliAgentSkillState[];
  error?: string;
};

export type BootstrapLog = {
  path: string;
  tail: string;
};

export async function getBootstrapLog(): Promise<BootstrapLog | null> {
  if (!isTauriRuntime()) {
    return null;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<BootstrapLog>("bootstrap_log");
}

/** Why the host closed the connection; `reason` is a code the screen words. */
export type BackendClosure = { reason: string; detail: string };

/** The backend is gone for good: only restarting the app starts it again. */
export class BackendClosedError extends Error {
  constructor(readonly closure: BackendClosure) {
    super(`GuildBotics backend is not running: ${closure.reason} ${closure.detail}`);
  }
}

const closedListeners = new Set<(error: BackendClosedError) => void>();
let closedBy: BackendClosedError | null = null;
let watchingForClose: Promise<unknown> | null = null;

/** Be told, with the host's reason, once the backend has gone for good. */
export function onBackendClosed(listener: (error: BackendClosedError) => void): () => void {
  closedListeners.add(listener);
  return () => closedListeners.delete(listener);
}

/**
 * Connect the frontend to the Local API backend.
 *
 * The sidecar process is owned by the Tauri (Rust) host: it is spawned once per
 * app process and killed when the app exits. The frontend only discovers the
 * port + session token via the `backend_info` command and reuses that running
 * backend. This avoids starting a second sidecar (and the resulting session
 * token / port collision) when a closed window is reopened. The host answers
 * only once the sidecar announced the port it bound, and owns the deadline for
 * that.
 */
export async function startBackend() {
  // Dev / browser preview: the backend is started externally with a fixed token.
  if (STATIC_TOKEN) {
    configureApi(STATIC_TOKEN, STATIC_BASE);
    await waitForHealth(STATIC_BASE, STATIC_TOKEN, Date.now() + 45_000);
    return;
  }

  if (!isTauriRuntime()) {
    throw new Error("GuildBotics backend is not configured for browser preview.");
  }

  // Listening first: the host does not repeat an exit that came before.
  await watchForClose();
  const { invoke } = await import("@tauri-apps/api/core");
  for (;;) {
    const info = await invoke<{ port: number; token: string } | null>("backend_info").catch(
      (closure: BackendClosure) => {
        throw new BackendClosedError(closure);
      },
    );
    if (info) {
      const base = `http://127.0.0.1:${info.port}`;
      configureApi(info.token, base);
      // The port is announced before the backend loads the workspace, which can
      // wait on a keychain prompt; the host ends the wait if the backend goes.
      await waitForHealth(base, info.token, Infinity);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
}

function watchForClose() {
  watchingForClose ??= import("@tauri-apps/api/event").then(({ listen }) =>
    listen<BackendClosure>("backend-closed", ({ payload }) => {
      disconnectApi();
      const error = new BackendClosedError(payload);
      closedBy = error;
      closedListeners.forEach((listener) => listener(error));
    }),
  );
  return watchingForClose;
}

/** Whether the app itself can be started again, which brings a new backend. */
export function canRestartApp() {
  return isTauriRuntime();
}

export async function restartApp() {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("restart_app");
}

/**
 * Switch the workspace the backend operates in. The backend changes its working
 * directory at runtime via `POST /workspace`, so there is no need to restart the
 * sidecar (which would orphan the previous process).
 */
export async function restartBackend(workspace: string) {
  await setWorkspace({ workspace_dir: workspace });
}

export async function stopBackend() {
  // The sidecar lifecycle is owned by the Rust host (killed on app exit), so
  // there is nothing for the frontend to tear down here.
}

export async function getCliAgentSkillStatuses(): Promise<CliAgentSkillStatusesResponse> {
  if (!isTauriRuntime()) {
    return { agents: [] };
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<CliAgentSkillStatusesResponse>("cli_agent_skill_statuses");
}

export async function forceUpdateCliAgentSkill(
  agent: CliAgentSkillState["agent"],
): Promise<CliAgentSkillState> {
  if (!isTauriRuntime()) {
    throw new Error("GuildBotics Desktop is required to update AI CLI tool skills.");
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<CliAgentSkillState>("force_update_cli_agent_skill", { agent });
}

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function waitForHealth(base: string, token: string, deadline: number) {
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    if (closedBy) {
      throw closedBy;
    }
    try {
      const response = await fetch(`${base}/health`, {
        headers: { "X-GuildBotics-Session-Token": token },
      });
      if (response.ok) {
        return;
      }
      lastError = await response.text();
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`GuildBotics backend did not start: ${String(lastError)}`);
}
