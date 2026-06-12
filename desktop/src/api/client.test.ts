import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiRequestError,
  configureApi,
  ensureAgentField,
  getAgentFieldState,
  getApiBase,
  getCommandOptions,
  getConfigStatus,
  getIntelligenceConfig,
  getMemberConfig,
  getPromptTrace,
  getRuntimeDebug,
  runScenarioDiagnostics,
  setWorkspace,
  subscribeEvents,
  subscribeLogs,
  updateRuntimeDebug,
  verify,
  type RuntimeEvent,
  type RuntimeLog,
  type StreamStatus,
} from "./client";

type FetchArgs = { url: string; init: RequestInit };

function jsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function captureFetch(response: Response): { calls: FetchArgs[]; mock: ReturnType<typeof vi.fn> } {
  const calls: FetchArgs[] = [];
  const mock = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return response;
  });
  vi.stubGlobal("fetch", mock);
  return { calls, mock };
}

function headerValue(init: RequestInit, name: string): string | undefined {
  return (init.headers as Record<string, string>)[name];
}

const ORIGINAL_BASE = getApiBase();

beforeEach(() => {
  // Reset module state to a known token + base for deterministic assertions.
  configureApi("test-token", "http://127.0.0.1:8765");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  // Restore the base URL that other test files may rely on.
  configureApi("", ORIGINAL_BASE);
});

describe("configureApi", () => {
  it("stores the token and base URL", () => {
    configureApi("abc123", "http://example.test:9000");
    expect(getApiBase()).toBe("http://example.test:9000");

    const { calls } = captureFetch(jsonResponse({ ok: true }));
    return getConfigStatus().then(() => {
      expect(calls[0].url).toBe("http://example.test:9000/config/status");
      expect(headerValue(calls[0].init, "X-GuildBotics-Session-Token")).toBe("abc123");
    });
  });

  it("keeps the existing base URL when none is provided", () => {
    configureApi("only-token");
    expect(getApiBase()).toBe("http://127.0.0.1:8765");
  });
});

describe("request headers and body", () => {
  it("sends the session token and JSON content type on GET", async () => {
    const { calls } = captureFetch(jsonResponse({ cwd: "/ws" }));
    await getConfigStatus();

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://127.0.0.1:8765/config/status");
    expect(calls[0].init.method).toBe("GET");
    expect(headerValue(calls[0].init, "Content-Type")).toBe("application/json");
    expect(headerValue(calls[0].init, "X-GuildBotics-Session-Token")).toBe("test-token");
    expect(calls[0].init.body).toBeUndefined();
  });

  it("serializes the body as JSON on POST", async () => {
    const { calls } = captureFetch(jsonResponse({ cwd: "/ws" }));
    await setWorkspace({ workspace_dir: "/projects/demo" });

    expect(calls[0].url).toBe("http://127.0.0.1:8765/workspace");
    expect(calls[0].init.method).toBe("POST");
    expect(headerValue(calls[0].init, "X-GuildBotics-Session-Token")).toBe("test-token");
    expect(calls[0].init.body).toBe(JSON.stringify({ workspace_dir: "/projects/demo" }));
  });

  it("omits the body for POST without a payload", async () => {
    const { calls } = captureFetch(jsonResponse({ ok: true }));
    await verify();

    expect(calls[0].url).toBe("http://127.0.0.1:8765/verify");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.body).toBeUndefined();
  });

  it("POSTs the project identity for getAgentFieldState", async () => {
    const { calls } = captureFetch(
      jsonResponse({ available: true, exists: false, options: [], missing: [] }),
    );
    const body = {
      owner: "acme",
      project_id: "9",
      github_project_url: "https://github.com/orgs/acme/projects/9",
    };
    await getAgentFieldState(body);

    expect(calls[0].url).toBe("http://127.0.0.1:8765/config/project/agent-field");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.body).toBe(JSON.stringify(body));
  });

  it("POSTs to the ensure endpoint for ensureAgentField", async () => {
    const { calls } = captureFetch(
      jsonResponse({ available: true, exists: true, options: [], missing: [] }),
    );
    const body = {
      owner: "acme",
      project_id: "9",
      github_project_url: "https://github.com/orgs/acme/projects/9",
    };
    await ensureAgentField(body);

    expect(calls[0].url).toBe("http://127.0.0.1:8765/config/project/agent-field/ensure");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.body).toBe(JSON.stringify(body));
  });
});

describe("GET query parameter encoding", () => {
  it("encodes limit and path for getPromptTrace", async () => {
    const { calls } = captureFetch(jsonResponse({ events: [] }));
    await getPromptTrace(50, "/var/logs/trace file.jsonl");

    const url = new URL(calls[0].url);
    expect(url.pathname).toBe("/prompt-trace");
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("path")).toBe("/var/logs/trace file.jsonl");
    // The space must be percent-encoded in the raw query string.
    expect(calls[0].url).toContain("trace+file.jsonl");
  });

  it("omits the path parameter when not provided", async () => {
    const { calls } = captureFetch(jsonResponse({ events: [] }));
    await getPromptTrace();

    expect(calls[0].url).toBe("http://127.0.0.1:8765/prompt-trace?limit=20");
  });

  it("fetches runtime debug status", async () => {
    const { calls } = captureFetch(jsonResponse({ enabled: false }));
    await getRuntimeDebug();

    expect(calls[0].url).toBe("http://127.0.0.1:8765/runtime/debug");
  });

  it("updates runtime debug status", async () => {
    const { calls } = captureFetch(jsonResponse({ enabled: true }));
    await updateRuntimeDebug({ enabled: true });

    expect(calls[0].url).toBe("http://127.0.0.1:8765/runtime/debug");
    expect(calls[0].init.method).toBe("PUT");
    expect(calls[0].init.body).toBe(JSON.stringify({ enabled: true }));
  });

  it("encodes person_id for runScenarioDiagnostics", async () => {
    const { calls } = captureFetch(jsonResponse({ ok: true }));
    await runScenarioDiagnostics("alice/dev");

    expect(calls[0].url).toBe("http://127.0.0.1:8765/diagnostics/scenario?person_id=alice%2Fdev");
    expect(calls[0].init.method).toBe("POST");
  });

  it("omits the query when runScenarioDiagnostics has no person id", async () => {
    const { calls } = captureFetch(jsonResponse({ ok: true }));
    await runScenarioDiagnostics();

    expect(calls[0].url).toBe("http://127.0.0.1:8765/diagnostics/scenario");
  });

  it("encodes person_id for getIntelligenceConfig", async () => {
    const { calls } = captureFetch(jsonResponse({ config_dir: "/c" }));
    await getIntelligenceConfig("bob smith");

    expect(calls[0].url).toBe("http://127.0.0.1:8765/config/intelligences?person_id=bob%20smith");
  });

  it("encodes person for getCommandOptions", async () => {
    const { calls } = captureFetch(jsonResponse({ options: [] }));
    await getCommandOptions("a&b");

    expect(calls[0].url).toBe("http://127.0.0.1:8765/commands/options?person=a%26b");
  });

  it("omits the query when getCommandOptions has no person", async () => {
    const { calls } = captureFetch(jsonResponse({ options: [] }));
    await getCommandOptions();

    expect(calls[0].url).toBe("http://127.0.0.1:8765/commands/options");
  });

  it("encodes the path segment for getMemberConfig", async () => {
    const { calls } = captureFetch(jsonResponse({ person_id: "x" }));
    await getMemberConfig("team/alice");

    expect(calls[0].url).toBe("http://127.0.0.1:8765/config/members/team%2Falice");
    expect(calls[0].init.method).toBe("GET");
  });
});

describe("error handling", () => {
  it("throws ApiRequestError with code, message and context on non-2xx", async () => {
    const payload = {
      code: "member_not_found",
      message: "No such member",
      context: { person_id: "ghost" },
    };
    captureFetch(jsonResponse(payload, { ok: false, status: 404 }));

    const error = await getConfigStatus().catch((err) => err);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect(error.name).toBe("ApiRequestError");
    expect(error.code).toBe("member_not_found");
    expect(error.message).toBe("No such member");
    expect(error.context).toEqual({ person_id: "ghost" });
  });

  it("preserves validation-error context intact", async () => {
    const context = {
      errors: [{ loc: ["body", "person_id"], msg: "field required", type: "value_error.missing" }],
      nested: { deep: [1, 2, 3] },
    };
    captureFetch(
      jsonResponse(
        { code: "validation_error", message: "Invalid request", context },
        { ok: false, status: 422 },
      ),
    );

    const error = await setWorkspace({ workspace_dir: "" }).catch((err) => err);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect(error.code).toBe("validation_error");
    expect(error.context).toEqual(context);
  });

  it("defaults context to an empty object when missing from payload", async () => {
    captureFetch(jsonResponse({ code: "boom", message: "kaboom" }, { ok: false, status: 500 }));

    const error = await getConfigStatus().catch((err) => err);
    expect(error.code).toBe("boom");
    expect(error.context).toEqual({});
  });

  it("falls back to an HTTP error when the body is not JSON", async () => {
    const response = {
      ok: false,
      status: 502,
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
      text: async () => "<html>Bad Gateway</html>",
    } as unknown as Response;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response),
    );

    const error = await getConfigStatus().catch((err) => err);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect(error.code).toBe("http_error");
    expect(error.message).toBe("HTTP 502");
    expect(error.context).toEqual({});
  });

  it("falls back when the JSON payload lacks code or message", async () => {
    captureFetch(jsonResponse({ detail: "nope" }, { ok: false, status: 400 }));

    const error = await getConfigStatus().catch((err) => err);
    expect(error.code).toBe("http_error");
    expect(error.message).toBe("HTTP 400");
  });
});

type SocketHandlers = {
  onopen?: () => void;
  onmessage?: (event: { data: string }) => void;
  onerror?: () => void;
  onclose?: () => void;
};

class MockWebSocket implements SocketHandlers {
  static instances: MockWebSocket[] = [];
  url: string;
  close = vi.fn();
  onopen?: () => void;
  onmessage?: (event: { data: string }) => void;
  onerror?: () => void;
  onclose?: () => void;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
}

describe("websocket subscriptions", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
  });

  it("connects to /events with the token and drives status transitions", () => {
    configureApi("ws-token", "http://127.0.0.1:8765");
    const events: RuntimeEvent[] = [];
    const statuses: StreamStatus[] = [];

    const unsubscribe = subscribeEvents(
      (event) => events.push(event),
      (status) => statuses.push(status),
    );

    const socket = MockWebSocket.instances[0];
    expect(socket.url).toBe("ws://127.0.0.1:8765/events?token=ws-token");
    expect(statuses).toEqual(["connecting"]);

    socket.onopen?.();
    expect(statuses).toEqual(["connecting", "connected"]);

    socket.onmessage?.({
      data: JSON.stringify({
        type: "task_started",
        trace_id: "r1",
        payload: { id: 1 },
        timestamp: "2026-06-05T00:00:00Z",
      }),
    });
    expect(events).toEqual([
      {
        type: "task_started",
        trace_id: "r1",
        payload: { id: 1 },
        timestamp: "2026-06-05T00:00:00Z",
      },
    ]);

    socket.onerror?.();
    expect(statuses).toEqual(["connecting", "connected", "error"]);

    socket.onclose?.();
    expect(statuses).toEqual(["connecting", "connected", "error", "disconnected"]);

    unsubscribe();
    expect(socket.close).toHaveBeenCalledTimes(1);
  });

  it("connects to /logs with the encoded token and parses log messages", () => {
    configureApi("a b&c", "http://127.0.0.1:8765");
    const logs: RuntimeLog[] = [];
    const statuses: StreamStatus[] = [];

    subscribeLogs(
      (log) => logs.push(log),
      (status) => statuses.push(status),
    );

    const socket = MockWebSocket.instances[0];
    expect(socket.url).toBe("ws://127.0.0.1:8765/logs?token=a%20b%26c");
    expect(statuses).toEqual(["connecting"]);

    socket.onmessage?.({
      data: JSON.stringify({
        level: "INFO",
        message: "hello",
        trace_id: null,
        timestamp: "2026-06-05T00:00:00Z",
      }),
    });
    expect(logs).toEqual([
      { level: "INFO", message: "hello", trace_id: null, timestamp: "2026-06-05T00:00:00Z" },
    ]);
  });

  it("works without an onStatus callback", () => {
    configureApi("ws-token", "http://127.0.0.1:8765");
    expect(() => subscribeEvents(() => undefined)).not.toThrow();
    const socket = MockWebSocket.instances[0];
    expect(() => socket.onopen?.()).not.toThrow();
    expect(() => socket.onerror?.()).not.toThrow();
    expect(() => socket.onclose?.()).not.toThrow();
  });
});

describe("websocketBase protocol conversion", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
  });

  it("converts http:// to ws://", () => {
    configureApi("t", "http://localhost:1234");
    subscribeEvents(() => undefined);
    expect(MockWebSocket.instances[0].url).toBe("ws://localhost:1234/events?token=t");
  });

  it("converts https:// to wss:// and strips the trailing slash", () => {
    configureApi("t", "https://api.example.com");
    subscribeEvents(() => undefined);
    expect(MockWebSocket.instances[0].url).toBe("wss://api.example.com/events?token=t");
  });
});
