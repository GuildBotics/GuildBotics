import { MantineProvider, createTheme } from "@mantine/core";
import { Notifications, notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { configureApi } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { SetupPage } from "../setup/SetupPage";

const t = i18n.getFixedT("en");
const BASE = "http://127.0.0.1:8765";
const TOKEN = "integration-token";

// jsdom lacks scrollIntoView, which Mantine's Combobox calls when opening a Select.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

vi.mock("@tauri-apps/plugin-shell", () => ({ open: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(), save: vi.fn() }));

// ---------------------------------------------------------------------------
// Mock HTTP boundary: a recording fetch that matches "METHOD /pathname" against
// a route table and returns canned JSON. The real api/client builds the request
// (URL, method, headers, JSON body, query string), so every recorded call
// reflects exactly what the client would send over the wire.
// ---------------------------------------------------------------------------

type RecordedRequest = {
  method: string;
  url: string;
  pathname: string;
  query: URLSearchParams;
  headers: Record<string, string>;
  body: unknown;
};

type RouteHandler = (req: RecordedRequest) => { status?: number; body: unknown };

class MockServer {
  routes = new Map<string, RouteHandler>();
  requests: RecordedRequest[] = [];

  on(method: string, pathname: string, handler: RouteHandler): this {
    this.routes.set(`${method} ${pathname}`, handler);
    return this;
  }

  json(method: string, pathname: string, body: unknown): this {
    return this.on(method, pathname, () => ({ body }));
  }

  requestsFor(method: string, pathname: string): RecordedRequest[] {
    return this.requests.filter((r) => r.method === method && r.pathname === pathname);
  }

  lastBody(method: string, pathname: string): unknown {
    const matches = this.requestsFor(method, pathname);
    return matches[matches.length - 1]?.body;
  }

  private handle(input: string, init?: RequestInit): Response {
    const url = new URL(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = (init?.headers as Record<string, string>) ?? {};
    const body = init?.body == null ? undefined : JSON.parse(String(init.body));
    const record: RecordedRequest = {
      method,
      url: input,
      pathname: url.pathname,
      query: url.searchParams,
      headers,
      body,
    };
    this.requests.push(record);

    if (url.pathname === "/health") {
      return jsonResponse(200, { status: "ok" });
    }
    const handler = this.routes.get(`${method} ${url.pathname}`);
    if (!handler) {
      return jsonResponse(404, {
        code: "not_found",
        message: `No mock route for ${method} ${url.pathname}`,
        context: {},
      });
    }
    const result = handler(record);
    return jsonResponse(result.status ?? 200, result.body);
  }

  install(): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, init?: RequestInit) => this.handle(input, init)),
    );
  }
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

// ---------------------------------------------------------------------------
// Mock WebSocket boundary: a controllable fake the test can push events into.
// ---------------------------------------------------------------------------

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  close = vi.fn();
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  static find(suffix: string): FakeWebSocket {
    const socket = FakeWebSocket.instances.find((s) => s.url.includes(suffix));
    if (!socket) {
      throw new Error(`No websocket opened for ${suffix}`);
    }
    return socket;
  }
}

function installWebSocket(): void {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
}

// ---------------------------------------------------------------------------
// Canned response factories.
// ---------------------------------------------------------------------------

function configStatus(overrides: Record<string, unknown> = {}) {
  return {
    cwd: "/workspace",
    env_file: "/workspace/.env",
    env_file_exists: true,
    config_dir: "/workspace/.guildbotics/config",
    project_file: "/workspace/.guildbotics/config/project.yml",
    project_file_exists: true,
    storage_dir: "/workspace/.guildbotics",
    ...overrides,
  };
}

function projectConfig(overrides: Record<string, unknown> = {}) {
  return {
    config_dir: "/workspace/.guildbotics/config",
    env_file_path: "/workspace/.env",
    language: "en",
    description: "Demo project",
    llm_api_type: "openai",
    cli_agent: "codex",
    github_enabled: false,
    github_project_url: "",
    provider_api_keys: { openai: true, gemini: false, anthropic: false },
    ...overrides,
  };
}

function llmProviders() {
  return {
    providers: [
      {
        provider: "openai",
        label: "OpenAI",
        order: 10,
        api_key_env: "OPENAI_API_KEY",
        model_class: "agno.models.openai.OpenAIChat",
        model_id: "gpt-5-mini",
      },
      {
        provider: "gemini",
        label: "Google Gemini",
        order: 20,
        api_key_env: "GOOGLE_API_KEY",
        model_class: "agno.models.google.Gemini",
        model_id: "gemini-3-flash-preview",
      },
      {
        provider: "anthropic",
        label: "Anthropic Claude",
        order: 30,
        api_key_env: "ANTHROPIC_API_KEY",
        model_class: "agno.models.anthropic.Claude",
        model_id: "claude-haiku-4-5",
      },
    ],
  };
}

function team(overrides: Record<string, unknown> = {}) {
  return {
    project: { name: "Demo", language_code: "en", language_name: "English" },
    members: [{ person_id: "alice", name: "Alice", is_active: true, roles: ["product"] }],
    ...overrides,
  };
}

function runtimeUnit(target: "scheduler" | "events", overrides: Record<string, unknown> = {}) {
  return {
    target,
    state: "stopped",
    running: false,
    started_at: null,
    stopped_at: null,
    error: null,
    routine_commands: [],
    max_consecutive_errors: null,
    routine_interval_minutes: null,
    active_member_count: 1,
    worker_count: 0,
    scheduled_source_enabled: null,
    routine_source_enabled: null,
    event_queue_source_enabled: null,
    subscription_count: 0,
    listener_count: 0,
    cycle_count: 0,
    cycle_failure_count: 0,
    events_drained_count: 0,
    ...overrides,
  };
}

function runtimeStatus(overrides: Record<string, unknown> = {}) {
  return {
    scheduler: runtimeUnit("scheduler"),
    events: runtimeUnit("events"),
    ...overrides,
  };
}

function promptTrace() {
  return {
    enabled: false,
    env_file: "",
    env_file_exists: false,
    trace_file: "",
    output_trace_file: "",
    default_trace_file: "",
    trace_file_exists: false,
    event_count: 0,
    events: [],
  };
}

function runtimeDebug(overrides: { enabled?: boolean } = {}) {
  const enabled = overrides.enabled ?? false;
  return {
    enabled,
    log_level: enabled ? "DEBUG" : "INFO",
    agno_debug: enabled,
    env_file: "/workspace/.env",
    env_file_exists: true,
  };
}

function catalogCommand(overrides: Record<string, unknown> = {}) {
  return {
    command: "workflows/sample",
    label: "Sample workflow",
    description: "A sample command",
    category: "workflow",
    source: "workspace",
    path: "/workspace/commands/sample.py",
    arguments: [
      { name: "topic", kind: "positional", required: true, default: "" },
      { name: "mode", kind: "keyword", required: false, default: "" },
    ],
    supports_raw_args: true,
    recommended_input: "",
    requirements: [],
    ...overrides,
  };
}

function configWriteResponse() {
  return { project: null, member: null, intelligence: null };
}

// Wire up the routes the App/Setup pages need so we can drive the UI. Each test
// overrides only the specific responses it asserts against.
function serviceServer(): MockServer {
  return new MockServer()
    .json("GET", "/config/status", configStatus())
    .json("GET", "/team", team())
    .json("GET", "/scheduler/routines", {
      routines: [{ command: "workflows/ticket_driven_workflow", requires_github: false }],
    })
    .json("GET", "/scheduler/status", runtimeStatus())
    .json("GET", "/config/project", projectConfig())
    .json("GET", "/commands/options", { options: [] })
    .json("GET", "/commands/routine-options", { options: [] })
    .json("GET", "/prompt-trace", promptTrace())
    .json("GET", "/runtime/debug", runtimeDebug())
    .json("PUT", "/runtime/debug", runtimeDebug({ enabled: true }))
    .json("POST", "/scheduler/start", runtimeStatus())
    .json("POST", "/scheduler/stop", runtimeStatus());
}

function renderApp(server: MockServer, path: string) {
  server.install();
  installWebSocket();
  configureApi(TOKEN, BASE);
  const theme = createTheme({ primaryColor: "dark", defaultRadius: "md" });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme}>
      <Notifications />
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

function renderSetup(server: MockServer, path: string) {
  server.install();
  installWebSocket();
  configureApi(TOKEN, BASE);
  const theme = createTheme({ primaryColor: "dark", defaultRadius: "md" });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme}>
      <Notifications />
      <QueryClientProvider client={queryClient}>
        <MemoryRouter
          future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
          initialEntries={[path]}
        >
          <SetupPage />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  configureApi(TOKEN, BASE);
});

afterEach(() => {
  notifications.clean();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  configureApi("", BASE);
});

describe("Service Runtime integration (real client + mock server)", () => {
  it("sends the real POST /scheduler/start with token, JSON body and sources", async () => {
    const server = serviceServer();
    const user = userEvent.setup();
    renderApp(server, "/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(server.requestsFor("POST", "/scheduler/start")).toHaveLength(1));
    const call = server.requestsFor("POST", "/scheduler/start")[0];
    expect(call.url).toBe(`${BASE}/scheduler/start`);
    expect(call.headers["X-GuildBotics-Session-Token"]).toBe(TOKEN);
    expect(call.headers["Content-Type"]).toBe("application/json");
    expect(call.body).toMatchObject({
      sources: { scheduled: true, routine: true, event_queue: true },
      routine_interval_minutes: expect.any(Number),
      max_consecutive_errors: expect.any(Number),
    });
    expect(call.body).not.toHaveProperty("routine_commands");
  });

  it("encodes sources and the edited interval / max errors into the start body", async () => {
    const server = serviceServer();
    const user = userEvent.setup();
    renderApp(server, "/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const eventsSwitch = screen
      .getByText(t("overview.eventsCard.title"))
      .closest(".service-unit-panel")
      ?.querySelector('[role="switch"]') as HTMLElement;
    await user.click(eventsSwitch);

    const interval = screen.getByRole("textbox", { name: t("overview.routineIntervalMinutes") });
    await user.clear(interval);
    await user.type(interval, "30");
    const maxErrors = screen.getByRole("textbox", { name: t("overview.maxConsecutiveErrors") });
    await user.clear(maxErrors);
    await user.type(maxErrors, "7");

    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(server.requestsFor("POST", "/scheduler/start")).toHaveLength(1));
    expect(server.lastBody("POST", "/scheduler/start")).toMatchObject({
      sources: { scheduled: true, routine: true, event_queue: false },
      routine_interval_minutes: 30,
      max_consecutive_errors: 7,
    });
    expect(server.lastBody("POST", "/scheduler/start")).not.toHaveProperty("routine_commands");
  });

  it("omits routine_commands and sends only the event queue source for the event-only target", async () => {
    const server = serviceServer();
    const user = userEvent.setup();
    renderApp(server, "/service");
    await screen.findByRole("heading", { name: t("service.title") });

    const routineSwitch = screen
      .getByText(t("overview.routineSourceCard.title"))
      .closest(".service-unit-panel")
      ?.querySelector('[role="switch"]') as HTMLElement;
    const scheduledSwitch = screen
      .getByText(t("overview.scheduledSourceCard.title"))
      .closest(".service-unit-panel")
      ?.querySelector('[role="switch"]') as HTMLElement;
    await user.click(routineSwitch);
    await user.click(scheduledSwitch);

    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    await waitFor(() => expect(server.requestsFor("POST", "/scheduler/start")).toHaveLength(1));
    const body = server.lastBody("POST", "/scheduler/start") as Record<string, unknown>;
    expect(body).toMatchObject({
      sources: { scheduled: false, routine: false, event_queue: true },
    });
    expect(body).not.toHaveProperty("routine_commands");
  });

  it("sends the real POST /scheduler/stop with the token when running", async () => {
    const server = serviceServer();
    server.json(
      "GET",
      "/scheduler/status",
      runtimeStatus({ scheduler: runtimeUnit("scheduler", { state: "running", running: true }) }),
    );
    const user = userEvent.setup();
    renderApp(server, "/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(await screen.findByRole("button", { name: t("overview.stop") }));

    await waitFor(() => expect(server.requestsFor("POST", "/scheduler/stop")).toHaveLength(1));
    const call = server.requestsFor("POST", "/scheduler/stop")[0];
    expect(call.url).toBe(`${BASE}/scheduler/stop`);
    expect(call.headers["X-GuildBotics-Session-Token"]).toBe(TOKEN);
    expect(call.body).toBeUndefined();
  });

  it("surfaces an ApiRequestError from a non-2xx /scheduler/start as a UI alert", async () => {
    const server = serviceServer();
    server.on("POST", "/scheduler/start", () => ({
      status: 409,
      body: { code: "scheduler_running", message: "already running", context: {} },
    }));
    const user = userEvent.setup();
    renderApp(server, "/service");
    await screen.findByRole("heading", { name: t("service.title") });

    await user.click(screen.getByRole("button", { name: t("overview.start") }));

    expect(await screen.findByText(t("overview.startError"))).toBeInTheDocument();
    expect(screen.getByText("already running")).toBeInTheDocument();
  });
});

describe("Setup integration (real client + mock server)", () => {
  it("drives first-time setup to a member add and project init with real requests", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    const server = new MockServer()
      .json("GET", "/config/status", configStatus({ project_file_exists: false }))
      .json("POST", "/workspace", configStatus({ project_file_exists: false }))
      .on("GET", "/team", () => ({
        status: 404,
        body: { code: "not_found", message: "missing", context: {} },
      }))
      .json("GET", "/config/project", projectConfig())
      .json("GET", "/commands/options", { options: [] })
      .json("GET", "/commands/routine-options", { options: [] })
      .json("GET", "/intelligences/cli-agents/detection", {
        agents: [{ name: "codex", executable: "codex", detected: true, path: "/usr/bin/codex" }],
      })
      .json("GET", "/intelligences/model-providers", llmProviders())
      .json("GET", "/config/roles", {
        roles: [{ role_id: "product", summary: "Product", description: "" }],
      })
      .json("POST", "/config/members", configWriteResponse())
      .json("POST", "/config/init", configWriteResponse());
    const user = userEvent.setup();
    renderSetup(server, "/setup");

    await screen.findByRole("heading", { name: "First setup" });
    await waitFor(() => expect(screen.getByLabelText("Workspace")).toHaveValue("/workspace"));

    await user.type(screen.getByLabelText("Project description"), "Demo project");
    // The GitHub use/don't decision now lives in the Project section.
    await user.click(await screen.findByRole("textbox", { name: "GitHub integration" }));
    await user.click(await screen.findByRole("option", { name: "Do not use GitHub" }));
    await user.click(screen.getByRole("button", { name: "LLM / AI CLI tools" }));
    await user.click(
      await screen.findByRole("button", {
        name: t("setup.intelligence.apiKeyButtonLabel", { provider: "OpenAI" }),
      }),
    );
    await user.type(await screen.findByLabelText("OpenAI API key"), "sk-test");

    await user.click(screen.getByRole("button", { name: "Members" }));
    await user.type(await screen.findByLabelText("Member ID"), "alice");
    await user.type(screen.getByLabelText("Display name"), "Alice");
    await user.type(screen.getByLabelText("Roles"), "product");
    await user.click(await screen.findByRole("option", { name: /^product\b/ }));
    await user.click(screen.getByRole("button", { name: "Add member" }));

    // The real POST /config/members request is built and sent by the client.
    await waitFor(() => expect(server.requestsFor("POST", "/config/members")).toHaveLength(1));
    const memberCall = server.requestsFor("POST", "/config/members")[0];
    expect(memberCall.url).toBe(`${BASE}/config/members`);
    expect(memberCall.headers["X-GuildBotics-Session-Token"]).toBe(TOKEN);
    expect(memberCall.body).toMatchObject({
      person_id: "alice",
      person_name: "Alice",
      person_type: "agent",
      github_account_type: "",
      is_active: true,
      roles: ["product"],
      config_dir: "/workspace/.guildbotics/config",
      env_file_path: "/workspace/.env",
    });

    const createButton = await screen.findByRole("button", { name: t("setup.saveInitial") });
    await user.click(createButton);

    await waitFor(() => expect(server.requestsFor("POST", "/config/init")).toHaveLength(1));
    const initCall = server.requestsFor("POST", "/config/init")[0];
    expect(initCall.url).toBe(`${BASE}/config/init`);
    expect(initCall.headers["X-GuildBotics-Session-Token"]).toBe(TOKEN);
    expect(initCall.body).toMatchObject({
      description: "Demo project",
      llm_api_type: "openai",
      cli_agent: "codex",
      owner: "",
      github_project_url: "",
      provider_api_keys: { openai: "sk-test" },
    });

    // restartBackend -> setWorkspace fires a real POST /workspace.
    await waitFor(() => expect(server.requestsFor("POST", "/workspace")).toHaveLength(1));
    expect(server.lastBody("POST", "/workspace")).toEqual({ workspace_dir: "/workspace" });
    expect(localStorage.getItem("guildbotics.workspace")).toBe("/workspace");
    expect(await screen.findByText(t("setup.initialCreated.title"))).toBeInTheDocument();
    expect(screen.getByText(/\/workspace\/\.guildbotics\/config/)).toBeInTheDocument();
    expect(screen.getByText(/\/workspace\/\.env/)).toBeInTheDocument();
  });
});

describe("Commands integration (real client + mock server)", () => {
  it("sends real GET /commands/options + POST /commands/run and updates history from websocket events", async () => {
    const server = new MockServer()
      .json("GET", "/config/status", configStatus())
      .json("GET", "/team", team())
      .json("GET", "/prompt-trace", promptTrace())
      .json("GET", "/runtime/debug", runtimeDebug())
      .json("PUT", "/runtime/debug", runtimeDebug({ enabled: true }))
      .json("POST", "/commands/run", { trace_id: "req-1", output: "hello output" })
      .on("GET", "/commands/options", () => ({ body: { options: [catalogCommand()] } }));
    const user = userEvent.setup();
    renderApp(server, "/commands");
    await screen.findByRole("heading", { name: t("commands.title") });

    // The real GET /commands/options request is built and sent by the client.
    // The catalog loads before a person is explicitly chosen, so no `person`
    // query param is present, but the session token header is.
    await waitFor(() =>
      expect(server.requestsFor("GET", "/commands/options").length).toBeGreaterThan(0),
    );
    const optionsCall = server.requestsFor("GET", "/commands/options")[0];
    expect(optionsCall.url).toBe(`${BASE}/commands/options`);
    expect(optionsCall.query.has("person")).toBe(false);
    expect(optionsCall.headers["X-GuildBotics-Session-Token"]).toBe(TOKEN);

    await user.type(await screen.findByRole("textbox", { name: "topic *" }), " release ");
    await user.type(screen.getByRole("textbox", { name: "mode" }), "fast");
    await user.click(screen.getByRole("button", { name: t("commands.run") }));

    await waitFor(() => expect(server.requestsFor("POST", "/commands/run")).toHaveLength(1));
    const runCall = server.requestsFor("POST", "/commands/run")[0];
    expect(runCall.url).toBe(`${BASE}/commands/run`);
    expect(runCall.headers["X-GuildBotics-Session-Token"]).toBe(TOKEN);
    expect(runCall.body).toMatchObject({
      command: "workflows/sample",
      args: ["release", "mode=fast"],
      person: "alice",
    });

    // A pushed command lifecycle event over the (real-client) websocket drives
    // the history UI. The client opens /events with the encoded token.
    const socket = FakeWebSocket.find(`/events?token=${TOKEN}`);
    await waitFor(() => expect(socket.onmessage).not.toBeNull());

    socket.emit({
      type: "command.started",
      trace_id: "evt-1",
      payload: { command: "workflows/sample", person: "alice" },
      timestamp: "2026-06-04T01:00:00Z",
    });
    expect(await screen.findByText(t("commands.status.running"))).toBeInTheDocument();

    socket.emit({
      type: "command.finished",
      trace_id: "evt-1",
      payload: { command: "workflows/sample", person: "alice" },
      timestamp: "2026-06-04T01:00:01Z",
    });
    expect(await screen.findByText(t("commands.status.success"))).toBeInTheDocument();
  });
});
