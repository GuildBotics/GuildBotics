import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as backend from "./backend";

const invoke = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

const listen = vi.fn();
vi.mock("@tauri-apps/api/event", () => ({ listen }));

type FetchMock = (url: string, init: RequestInit) => Promise<Response>;

function setTauriRuntime(enabled: boolean) {
  if (enabled) {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  } else {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  }
}

beforeEach(() => {
  invoke.mockReset();
  listen.mockReset();
  setTauriRuntime(false);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  setTauriRuntime(false);
});

describe("backendEntry", () => {
  const LINK = "http://127.0.0.1:8765/-/enter?next=/quick.html#code-1";

  it("asks the host until the backend is up, then answers its entry link", async () => {
    vi.useFakeTimers();
    vi.stubEnv("DEV", false);
    invoke.mockResolvedValueOnce(null).mockResolvedValueOnce({ url: LINK });

    const entered = backend.backendEntry("/quick.html");
    await vi.advanceTimersByTimeAsync(300);

    await expect(entered).resolves.toBe(LINK);
    expect(invoke).toHaveBeenCalledTimes(2);
    expect(invoke).toHaveBeenCalledWith("backend_entry", { next: "/quick.html" });
  });

  it("goes through the dev server, which proxies /api and keeps HMR, in vite dev", async () => {
    vi.stubEnv("DEV", true);
    invoke.mockResolvedValue({ url: LINK });

    await expect(backend.backendEntry("/quick.html")).resolves.toBe(
      `${window.location.origin}/-/enter?next=/quick.html#code-1`,
    );
  });

  it("rejects with the host's reason once the backend closed", async () => {
    const closure = { reason: "exited", detail: "code 137" };
    invoke.mockRejectedValue(closure);

    const failure = await backend.backendEntry("/").catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(backend.BackendClosedError);
    expect((failure as backend.BackendClosedError).closure).toEqual(closure);
  });
});

describe("Desktop host commands", () => {
  beforeEach(() => setTauriRuntime(true));

  it("lets a quit through while nothing guards it, until stopped", async () => {
    const unlisten = vi.fn();
    listen.mockResolvedValue(unlisten);

    const stop = backend.letQuitsThrough();
    await vi.waitFor(() => expect(listen).toHaveBeenCalled());
    const [event, handler] = listen.mock.calls[0];
    expect(event).toBe("app://quit-requested");
    await (handler as () => Promise<void>)();
    expect(invoke).toHaveBeenCalledWith("quit_app");

    stop();
    await vi.waitFor(() => expect(unlisten).toHaveBeenCalled());
  });

  it("restarts the app through the host", async () => {
    await backend.restartApp();

    expect(invoke).toHaveBeenCalledWith("restart_app");
  });

  it("reads the startup log through the host", async () => {
    invoke.mockResolvedValue({ path: "/logs/bootstrap.log", tail: "started" });

    await expect(backend.getBootstrapLog()).resolves.toEqual({
      path: "/logs/bootstrap.log",
      tail: "started",
    });
    expect(invoke).toHaveBeenCalledWith("bootstrap_log");
  });
});

describe("restartBackend", () => {
  it("updates the backend workspace", async () => {
    const fetchMock = vi.fn<FetchMock>(
      async () => ({ ok: true, json: async () => ({}) }) as unknown as Response,
    );
    vi.stubGlobal("fetch", fetchMock);

    await backend.restartBackend("/projects/demo");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/workspace");
    expect(JSON.parse(String(init.body))).toEqual({ workspace_dir: "/projects/demo" });
  });

  it("propagates backend workspace failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("boom");
      }),
    );

    await expect(backend.restartBackend("/projects/demo")).rejects.toThrow("boom");
  });
});

describe("AI CLI tool skill commands", () => {
  it("returns an empty status list outside Tauri", async () => {
    setTauriRuntime(false);

    await expect(backend.getCliAgentSkillStatuses()).resolves.toEqual({ agents: [] });
    expect(invoke).not.toHaveBeenCalled();
  });

  it("loads skill statuses through Tauri", async () => {
    setTauriRuntime(true);
    invoke.mockResolvedValue({
      agents: [
        {
          agent: "codex",
          agent_home: "/home/.codex",
          skill_path: "/home/.agents/skills/guildbotics/SKILL.md",
          status: "up_to_date",
          can_force_update: false,
        },
      ],
    });

    const statuses = await backend.getCliAgentSkillStatuses();

    expect(invoke).toHaveBeenCalledWith("cli_agent_skill_statuses");
    expect(statuses.agents[0].status).toBe("up_to_date");
  });

  it("force-updates a skill through Tauri", async () => {
    setTauriRuntime(true);
    invoke.mockResolvedValue({
      agent: "codex",
      agent_home: "/home/.codex",
      skill_path: "/home/.agents/skills/guildbotics/SKILL.md",
      status: "up_to_date",
      can_force_update: false,
    });

    await backend.forceUpdateCliAgentSkill("codex");

    expect(invoke).toHaveBeenCalledWith("force_update_cli_agent_skill", { agent: "codex" });
  });
});
