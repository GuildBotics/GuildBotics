import { Command } from "@tauri-apps/plugin-shell";

import { configureApi, setWorkspace } from "./client";

const API_BASE = import.meta.env.VITE_GUILDBOTICS_API_BASE ?? "http://127.0.0.1:8765";
const STATIC_TOKEN = import.meta.env.VITE_GUILDBOTICS_API_TOKEN ?? "";

let backendProcess: { kill(): Promise<void> } | null = null;
let currentWorkspace = localStorage.getItem("guildbotics.workspace") ?? "";

export async function startBackend() {
  const token = STATIC_TOKEN || crypto.randomUUID();
  configureApi(token);

  if (STATIC_TOKEN) {
    await waitForHealth(token);
    if (currentWorkspace) {
      try {
        await applyWorkspace(currentWorkspace);
      } catch (error) {
        console.warn("Unable to restore GuildBotics workspace", error);
      }
    }
    return;
  }

  const command = Command.sidecar("binaries/guildbotics-app-api", [
    "--host",
    "127.0.0.1",
    "--port",
    "8765",
    "--token",
    token,
  ], currentWorkspace ? { cwd: currentWorkspace } : undefined);
  backendProcess = await command.spawn();
  await waitForHealth(token);
}

export async function restartBackend(workspace: string) {
  localStorage.setItem("guildbotics.workspace", workspace);
  currentWorkspace = workspace;
  if (STATIC_TOKEN) {
    await applyWorkspace(workspace);
    return;
  }
  await stopBackend();
  await startBackend();
}

export async function stopBackend() {
  await backendProcess?.kill();
  backendProcess = null;
}

async function applyWorkspace(workspace: string) {
  try {
    await setWorkspace({ workspace_dir: workspace });
  } catch (error) {
    localStorage.removeItem("guildbotics.workspace");
    currentWorkspace = "";
    throw error;
  }
}

async function waitForHealth(token: string) {
  const deadline = Date.now() + 15_000;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${API_BASE}/health`, {
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
