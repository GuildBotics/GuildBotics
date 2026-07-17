import { configureApi, getApiBase, setWorkspace } from "./client";

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

/**
 * Connect the frontend to the Local API backend.
 *
 * The sidecar process is owned by the Tauri (Rust) host: it is spawned once per
 * app process and killed when the app exits. The frontend only discovers the
 * port + session token via the `backend_info` command and reuses that running
 * backend. This avoids starting a second sidecar (and the resulting session
 * token / port collision) when a closed window is reopened.
 */
export async function startBackend() {
  // Dev / browser preview: the backend is started externally with a fixed token.
  if (STATIC_TOKEN) {
    configureApi(STATIC_TOKEN, STATIC_BASE);
    await waitForHealth(STATIC_TOKEN);
    return;
  }

  if (!isTauriRuntime()) {
    throw new Error("GuildBotics backend is not configured for browser preview.");
  }

  const { invoke } = await import("@tauri-apps/api/core");
  const info = await invoke<{ port: number; token: string }>("backend_info");
  configureApi(info.token, `http://127.0.0.1:${info.port}`);
  await waitForHealth(info.token);
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

async function waitForHealth(token: string) {
  // The packaged sidecar is a PyInstaller one-file binary that unpacks itself
  // into a temp dir on first launch, which can take ~10s on a fresh Mac before
  // uvicorn answers. Keep a generous deadline so the cold start is not flagged
  // as a backend failure.
  const deadline = Date.now() + 45_000;
  const base = getApiBase();
  let lastError: unknown = null;
  while (Date.now() < deadline) {
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
