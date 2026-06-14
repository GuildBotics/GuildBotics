import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// backend.ts reads `import.meta.env` and `localStorage` at module-evaluation
// time, so every test resets the module registry and re-imports it freshly.

const configureApi = vi.fn();
const setWorkspace = vi.fn(async () => ({}));
let apiBase = "http://127.0.0.1:8765";
const getApiBase = vi.fn(() => apiBase);

vi.mock("./client", () => ({
  configureApi,
  getApiBase,
  setWorkspace,
}));

const invoke = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

type BackendModule = typeof import("./backend");

async function loadBackend(): Promise<BackendModule> {
  vi.resetModules();
  return import("./backend");
}

function setApiBase(base: string) {
  apiBase = base;
}

type FetchMock = (url: string, init: RequestInit) => Promise<Response>;

function okResponse(): Response {
  return { ok: true, text: async () => "ok" } as unknown as Response;
}

function failResponse(status: number, body: string): Response {
  return { ok: false, status, text: async () => body } as unknown as Response;
}

function setTauriRuntime(enabled: boolean) {
  if (enabled) {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  } else {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  configureApi.mockReset();
  setWorkspace.mockReset();
  setWorkspace.mockResolvedValue({});
  getApiBase.mockClear();
  invoke.mockReset();
  apiBase = "http://127.0.0.1:8765";
  localStorage.clear();
  setTauriRuntime(false);
  vi.unstubAllEnvs();
});

afterEach(() => {
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  setTauriRuntime(false);
});

describe("startBackend - browser preview mode", () => {
  it("configures the API and health-checks without invoking Tauri", async () => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "preview-token");
    vi.stubEnv("VITE_GUILDBOTICS_API_BASE", "http://preview.test:9000");
    setApiBase("http://preview.test:9000");
    const fetchMock = vi.fn<FetchMock>(async () => okResponse());
    vi.stubGlobal("fetch", fetchMock);

    const backend = await loadBackend();
    await backend.startBackend();

    expect(configureApi).toHaveBeenCalledWith("preview-token", "http://preview.test:9000");
    expect(invoke).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://preview.test:9000/health");
    expect((init.headers as Record<string, string>)["X-GuildBotics-Session-Token"]).toBe(
      "preview-token",
    );
  });
});

describe("startBackend - Tauri runtime", () => {
  it("uses the port and token returned by backend_info", async () => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "");
    setTauriRuntime(true);
    invoke.mockResolvedValue({ port: 7777, token: "runtime-token" });
    const fetchMock = vi.fn<FetchMock>(async () => {
      setApiBase("http://127.0.0.1:7777");
      return okResponse();
    });
    vi.stubGlobal("fetch", fetchMock);
    // getApiBase returns whatever configureApi would have set.
    setApiBase("http://127.0.0.1:7777");

    const backend = await loadBackend();
    await backend.startBackend();

    expect(invoke).toHaveBeenCalledWith("backend_info");
    expect(configureApi).toHaveBeenCalledWith("runtime-token", "http://127.0.0.1:7777");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:7777/health");
    expect((init.headers as Record<string, string>)["X-GuildBotics-Session-Token"]).toBe(
      "runtime-token",
    );
  });

  it("throws a clear error when neither Tauri nor a static token is available", async () => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "");
    setTauriRuntime(false);

    const backend = await loadBackend();
    await expect(backend.startBackend()).rejects.toThrow(
      "GuildBotics backend is not configured for browser preview.",
    );
    expect(invoke).not.toHaveBeenCalled();
    expect(configureApi).not.toHaveBeenCalled();
  });
});

describe("waitForHealth", () => {
  it("retries past transient fetch failures then succeeds", async () => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "preview-token");
    vi.stubEnv("VITE_GUILDBOTICS_API_BASE", "http://preview.test:9000");
    setApiBase("http://preview.test:9000");
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("ECONNREFUSED"))
      .mockResolvedValueOnce(failResponse(503, "starting"))
      .mockResolvedValueOnce(okResponse());
    vi.stubGlobal("fetch", fetchMock);

    const backend = await loadBackend();
    const started = backend.startBackend();
    // Drain the two 300ms retry backoffs plus the awaited microtasks.
    await vi.advanceTimersByTimeAsync(700);
    await started;

    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("fails past the deadline including the last error", async () => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "preview-token");
    vi.stubEnv("VITE_GUILDBOTICS_API_BASE", "http://preview.test:9000");
    setApiBase("http://preview.test:9000");
    const fetchMock = vi.fn(async () => failResponse(500, "still booting"));
    vi.stubGlobal("fetch", fetchMock);

    const backend = await loadBackend();
    const started = backend.startBackend();
    const assertion = expect(started).rejects.toThrow(
      "GuildBotics backend did not start: still booting",
    );
    // Advance well past the 45s deadline so the retry loop exits.
    await vi.advanceTimersByTimeAsync(46_000);
    await assertion;
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });
});

describe("restartBackend", () => {
  it("updates localStorage and the backend workspace", async () => {
    const backend = await loadBackend();
    await backend.restartBackend("/projects/demo");

    expect(localStorage.getItem("guildbotics.workspace")).toBe("/projects/demo");
    expect(setWorkspace).toHaveBeenCalledWith({ workspace_dir: "/projects/demo" });
  });

  it("clears guildbotics.workspace when setWorkspace fails", async () => {
    localStorage.setItem("guildbotics.workspace", "/old");
    setWorkspace.mockRejectedValueOnce(new Error("boom"));

    const backend = await loadBackend();
    await expect(backend.restartBackend("/projects/demo")).rejects.toThrow("boom");

    expect(localStorage.getItem("guildbotics.workspace")).toBeNull();
  });
});

describe("CLI agent skill commands", () => {
  it("returns an empty status list outside Tauri", async () => {
    setTauriRuntime(false);

    const backend = await loadBackend();

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
          skill_path: "/home/.codex/skills/guildbotics/SKILL.md",
          status: "up_to_date",
          can_force_update: false,
        },
      ],
    });

    const backend = await loadBackend();
    const statuses = await backend.getCliAgentSkillStatuses();

    expect(invoke).toHaveBeenCalledWith("cli_agent_skill_statuses");
    expect(statuses.agents[0].status).toBe("up_to_date");
  });

  it("force-updates a skill through Tauri", async () => {
    setTauriRuntime(true);
    invoke.mockResolvedValue({
      agent: "codex",
      agent_home: "/home/.codex",
      skill_path: "/home/.codex/skills/guildbotics/SKILL.md",
      status: "up_to_date",
      can_force_update: false,
    });

    const backend = await loadBackend();
    await backend.forceUpdateCliAgentSkill("codex");

    expect(invoke).toHaveBeenCalledWith("force_update_cli_agent_skill", { agent: "codex" });
  });
});

describe("restoreWorkspace via startBackend", () => {
  it("applies a previously stored workspace on startup", async () => {
    localStorage.setItem("guildbotics.workspace", "/restored");
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "preview-token");
    vi.stubEnv("VITE_GUILDBOTICS_API_BASE", "http://preview.test:9000");
    setApiBase("http://preview.test:9000");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse()),
    );

    const backend = await loadBackend();
    await backend.startBackend();

    expect(setWorkspace).toHaveBeenCalledWith({ workspace_dir: "/restored" });
  });

  it("cleans localStorage on restore failure without breaking startup", async () => {
    localStorage.setItem("guildbotics.workspace", "/restored");
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "preview-token");
    vi.stubEnv("VITE_GUILDBOTICS_API_BASE", "http://preview.test:9000");
    setApiBase("http://preview.test:9000");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse()),
    );
    setWorkspace.mockRejectedValueOnce(new Error("restore failed"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const backend = await loadBackend();
    // startBackend must resolve even though workspace restore failed.
    await expect(backend.startBackend()).resolves.toBeUndefined();

    expect(localStorage.getItem("guildbotics.workspace")).toBeNull();
    expect(warn).toHaveBeenCalled();
  });

  it("does nothing when no workspace is stored", async () => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "preview-token");
    vi.stubEnv("VITE_GUILDBOTICS_API_BASE", "http://preview.test:9000");
    setApiBase("http://preview.test:9000");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse()),
    );

    const backend = await loadBackend();
    await backend.startBackend();

    expect(setWorkspace).not.toHaveBeenCalled();
  });
});

describe("stopBackend", () => {
  it("resolves without side effects", async () => {
    const backend = await loadBackend();
    await expect(backend.stopBackend()).resolves.toBeUndefined();
    expect(setWorkspace).not.toHaveBeenCalled();
  });
});
