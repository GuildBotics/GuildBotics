import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// backend.ts reads `import.meta.env` at module-evaluation time, so every test
// resets the module registry and re-imports it freshly.

// backend.ts drives the real connection state in ./client, so what a late
// answer could reopen is observed; requests are seen at `fetch`.

const invoke = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

type ClosedHandler = (event: { payload: { reason: string; detail: string } }) => void;
const closedHandlers: ClosedHandler[] = [];
const listen = vi.fn(async (_event: string, handler: ClosedHandler) => {
  closedHandlers.push(handler);
  return () => undefined;
});
vi.mock("@tauri-apps/api/event", () => ({ listen }));

type BackendModule = typeof import("./backend");

async function loadBackend(): Promise<BackendModule> {
  vi.resetModules();
  return import("./backend");
}

/** The connection state of the registry `loadBackend` last loaded. */
function loadClient() {
  return import("./client");
}

/** A promise settled by the test, to order answers against the exit. */
function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
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
  invoke.mockReset();
  listen.mockClear();
  closedHandlers.length = 0;
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
    const fetchMock = vi.fn<FetchMock>(async () => okResponse());
    vi.stubGlobal("fetch", fetchMock);

    const backend = await loadBackend();
    await backend.startBackend();

    expect((await loadClient()).connected()).toEqual({
      token: "preview-token",
      base: "http://preview.test:9000",
    });
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
  beforeEach(() => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "");
    setTauriRuntime(true);
  });

  it("waits for the announced port, listening for an exit first", async () => {
    invoke
      .mockImplementationOnce(async () => {
        expect(listen).toHaveBeenCalledWith("backend-closed", expect.any(Function));
        return null;
      })
      .mockResolvedValueOnce({ port: 7777, token: "runtime-token" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse()),
    );

    const backend = await loadBackend();
    const started = backend.startBackend();
    await vi.advanceTimersByTimeAsync(300);
    await started;

    expect(invoke).toHaveBeenCalledTimes(2);
    expect(invoke).toHaveBeenCalledWith("backend_info");
    expect((await loadClient()).connected()).toEqual({
      token: "runtime-token",
      base: "http://127.0.0.1:7777",
    });
  });

  it("does not let a ready answer that arrives after the exit reopen the connection", async () => {
    const answer = deferred<{ port: number; token: string }>();
    invoke.mockReturnValue(answer.promise);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const backend = await loadBackend();
    const client = await loadClient();
    const started = backend.startBackend().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    closedHandlers[0]({ payload: { reason: "exited", detail: "code 1" } });
    answer.resolve({ port: 7777, token: "runtime-token" });

    expect(await started).toBeInstanceOf(backend.BackendClosedError);
    expect(client.memberAvatarUrl("alice")).toBeUndefined();
    await expect(client.getConfigStatus()).rejects.toBeInstanceOf(backend.BackendClosedError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not let a health answer that arrives after the exit complete the start", async () => {
    invoke.mockResolvedValue({ port: 7777, token: "runtime-token" });
    const health = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => health.promise),
    );

    const backend = await loadBackend();
    const client = await loadClient();
    const started = backend.startBackend().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    closedHandlers[0]({ payload: { reason: "exited", detail: "code 1" } });
    health.resolve(okResponse());

    expect(await started).toBeInstanceOf(backend.BackendClosedError);
    expect(() => client.connected()).toThrow(backend.BackendClosedError);
  });

  it("keeps waiting past the preview deadline while the backend loads the workspace", async () => {
    invoke.mockResolvedValue({ port: 7777, token: "runtime-token" });
    const fetchMock = vi.fn<FetchMock>(async () => {
      // A keychain prompt holds the backend before it answers.
      throw new Error("timed out");
    });
    vi.stubGlobal("fetch", fetchMock);

    const backend = await loadBackend();
    let settled = false;
    const started = backend.startBackend().finally(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(settled).toBe(false);

    fetchMock.mockResolvedValue(okResponse());
    await vi.advanceTimersByTimeAsync(300);
    await started;
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:7777/health");
    expect((init.headers as Record<string, string>)["X-GuildBotics-Session-Token"]).toBe(
      "runtime-token",
    );
  });

  it("stops waiting once the host reports the backend gone", async () => {
    invoke.mockResolvedValue({ port: 7777, token: "runtime-token" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("timed out");
      }),
    );

    const backend = await loadBackend();
    const started = backend.startBackend().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(300);
    closedHandlers[0]({ payload: { reason: "timeout", detail: "45" } });
    await vi.advanceTimersByTimeAsync(300);

    expect(await started).toBeInstanceOf(backend.BackendClosedError);
  });

  it("fails with the host's reason and connects nothing", async () => {
    const closure = { reason: "duplicate_notice", detail: "" };
    invoke.mockRejectedValue(closure);

    const backend = await loadBackend();
    const client = await loadClient();

    const failure = await backend.startBackend().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(backend.BackendClosedError);
    expect((failure as InstanceType<typeof backend.BackendClosedError>).closure).toEqual(closure);
    expect(() => client.connected()).toThrow("GuildBotics backend is not running.");
  });

  it("disconnects and tells its listeners once the host reports an exit", async () => {
    invoke.mockResolvedValue({ port: 7777, token: "runtime-token" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse()),
    );
    const backend = await loadBackend();
    const client = await loadClient();
    const closed = vi.fn();
    backend.onBackendClosed(closed);
    await backend.startBackend();
    await backend.startBackend();

    expect(listen).toHaveBeenCalledTimes(1);
    const closure = { reason: "exited", detail: "code 137" };
    closedHandlers[0]({ payload: closure });

    expect(() => client.connected()).toThrow(backend.BackendClosedError);
    expect(client.memberAvatarUrl("alice")).toBeUndefined();
    expect(closed).toHaveBeenCalledWith(expect.objectContaining({ closure }));
  });

  it("lets a quit through while nothing guards it, until stopped", async () => {
    const unlisten = vi.fn();
    listen.mockImplementationOnce(async () => unlisten);
    const backend = await loadBackend();

    const stop = backend.letQuitsThrough();
    await vi.advanceTimersByTimeAsync(0);
    const [event, handler] = listen.mock.calls[0];
    expect(event).toBe("app://quit-requested");
    await (handler as () => Promise<void>)();
    expect(invoke).toHaveBeenCalledWith("quit_app");

    stop();
    await vi.advanceTimersByTimeAsync(0);
    expect(unlisten).toHaveBeenCalled();
  });

  it("restarts the app through the host", async () => {
    const backend = await loadBackend();

    expect(backend.canRestartApp()).toBe(true);
    await backend.restartApp();

    expect(invoke).toHaveBeenCalledWith("restart_app");
  });
});

describe("startBackend - neither Tauri nor browser preview", () => {
  it("throws a clear error when neither Tauri nor a static token is available", async () => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "");

    const backend = await loadBackend();
    const client = await loadClient();
    await expect(backend.startBackend()).rejects.toThrow(
      "GuildBotics backend is not configured for browser preview.",
    );
    expect(invoke).not.toHaveBeenCalled();
    expect(() => client.connected()).toThrow("GuildBotics backend is not running.");
  });
});

describe("waitForHealth", () => {
  it("retries past transient fetch failures then succeeds", async () => {
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "preview-token");
    vi.stubEnv("VITE_GUILDBOTICS_API_BASE", "http://preview.test:9000");
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
  it("updates the backend workspace", async () => {
    const fetchMock = vi.fn<FetchMock>(
      async () => ({ ok: true, json: async () => ({}) }) as unknown as Response,
    );
    vi.stubGlobal("fetch", fetchMock);
    const backend = await loadBackend();
    (await loadClient()).configureApi("token", "http://127.0.0.1:7777");

    await backend.restartBackend("/projects/demo");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:7777/workspace");
    expect(JSON.parse(String(init.body))).toEqual({ workspace_dir: "/projects/demo" });
  });

  it("propagates backend workspace failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("boom");
      }),
    );
    const backend = await loadBackend();
    (await loadClient()).configureApi("token", "http://127.0.0.1:7777");

    await expect(backend.restartBackend("/projects/demo")).rejects.toThrow("boom");
  });
});

describe("AI CLI tool skill commands", () => {
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
          skill_path: "/home/.agents/skills/guildbotics/SKILL.md",
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
      skill_path: "/home/.agents/skills/guildbotics/SKILL.md",
      status: "up_to_date",
      can_force_update: false,
    });

    const backend = await loadBackend();
    await backend.forceUpdateCliAgentSkill("codex");

    expect(invoke).toHaveBeenCalledWith("force_update_cli_agent_skill", { agent: "codex" });
  });
});

describe("workspace persistence", () => {
  it("does not read the legacy frontend workspace value on startup", async () => {
    localStorage.setItem("guildbotics.workspace", "/restored");
    vi.stubEnv("VITE_GUILDBOTICS_API_TOKEN", "preview-token");
    vi.stubEnv("VITE_GUILDBOTICS_API_BASE", "http://preview.test:9000");
    const fetchMock = vi.fn<FetchMock>(async () => okResponse());
    vi.stubGlobal("fetch", fetchMock);

    const backend = await loadBackend();
    await backend.startBackend();

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["http://preview.test:9000/health"]);
    expect(localStorage.getItem("guildbotics.workspace")).toBe("/restored");
  });
});

describe("stopBackend", () => {
  it("resolves without side effects", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const backend = await loadBackend();
    await expect(backend.stopBackend()).resolves.toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
