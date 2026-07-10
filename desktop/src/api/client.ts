let apiBase = import.meta.env.VITE_GUILDBOTICS_API_BASE ?? "http://127.0.0.1:8765";
let sessionToken = import.meta.env.VITE_GUILDBOTICS_API_TOKEN ?? "";

export function configureApi(token: string, baseUrl?: string) {
  sessionToken = token;
  if (baseUrl) {
    apiBase = baseUrl;
  }
}

export function getApiBase(): string {
  return apiBase;
}

export function memberAvatarUrl(personId: string, cacheBust?: number | string): string {
  const params = new URLSearchParams();
  if (sessionToken) {
    params.set("token", sessionToken);
  }
  if (cacheBust !== undefined) {
    params.set("t", String(cacheBust));
  }
  const query = params.toString();
  return `${apiBase}/config/members/${encodeURIComponent(personId)}/avatar${
    query ? `?${query}` : ""
  }`;
}

export type ConfigStatus = {
  cwd: string;
  env_file: string;
  env_file_exists: boolean;
  config_dir: string;
  project_file: string;
  project_file_exists: boolean;
  storage_dir: string;
  machine_state_dir?: string | null;
  workspace_data_dir?: string | null;
};

export type WorkspaceChangeRequest = {
  workspace_dir: string;
};

export type TeamSummary = {
  project: {
    name: string;
    language_code: string;
    language_name: string;
  };
  members: Array<{
    person_id: string;
    name: string;
    person_type?: "human" | "agent" | "";
    is_active: boolean;
    roles: string[];
  }>;
};

export type RuntimeUnitStatus = {
  target: "scheduler" | "events";
  state: "starting" | "running" | "stopping" | "stopped" | "failed";
  running: boolean;
  started_at: string | null;
  stopped_at: string | null;
  error: string | null;
  routine_commands: string[];
  max_consecutive_errors: number | null;
  routine_interval_minutes: number | null;
  active_member_count: number | null;
  worker_count: number | null;
  scheduled_source_enabled: boolean | null;
  routine_source_enabled: boolean | null;
  event_queue_source_enabled: boolean | null;
  subscription_count: number | null;
  listener_count: number | null;
  cycle_count: number | null;
  cycle_failure_count: number | null;
  events_drained_count: number | null;
  events_auth_failed_count: number | null;
  events_auth_failed_persons: string[];
};

export type RuntimeActiveWork = {
  id: string;
  source: "manual" | "scheduled" | "routine" | "event_queue";
  person_id: string;
  command: string;
  started_at: string;
};

export type RuntimeStatus = {
  scheduler: RuntimeUnitStatus;
  events: RuntimeUnitStatus;
  active_works?: RuntimeActiveWork[];
};

export type ChatReceiveResetResponse = {
  members_reset: number;
  channels_reset: number;
};

export type RuntimeSourceSelection = {
  scheduled: boolean;
  routine: boolean;
  event_queue: boolean;
};

export type SchedulerStartRequest = {
  sources?: RuntimeSourceSelection;
  routine_commands?: string[];
  max_consecutive_errors?: number;
  routine_interval_minutes?: number;
};

export type SchedulerStopRequest = {
  force?: boolean;
};

export type PromptTraceEntry = {
  event: string;
  timestamp: string;
  person_id: string;
  brain: string;
  command: string;
  target: string;
  cwd: string;
  description: string;
  transcript: string;
  prompt: string;
  response: string;
  error: string;
  fields: Record<string, unknown>;
};

export type PromptTraceStatus = {
  enabled: boolean;
  env_file: string;
  env_file_exists: boolean;
  trace_file: string;
  output_trace_file: string;
  default_trace_file: string;
  trace_file_exists: boolean;
  event_count: number;
  events: PromptTraceEntry[];
};

export type PromptTraceUpdateRequest = {
  enabled: boolean;
  trace_path?: string;
};

export type RuntimeDebugStatus = {
  enabled: boolean;
  log_level: string;
  agno_debug: boolean;
  env_file: string;
  env_file_exists: boolean;
};

export type RuntimeDebugUpdateRequest = {
  enabled: boolean;
};

export type VerifyCheck = {
  code: string;
  status: "ok" | "warning" | "error";
  message: string;
  target: string;
  context: Record<string, unknown>;
};

export type VerifyResponse = {
  ok: boolean;
  config: ConfigStatus;
  active_members: string[];
  checks: VerifyCheck[];
  warnings: VerifyCheck[];
  errors: VerifyCheck[];
};

export type DiagnosticCheck = {
  section: "config" | "members" | "llm" | "cli_agent" | "github" | "slack" | "git";
  code: string;
  status: "ok" | "warning" | "error";
  message: string;
  target: string;
  person_id: string;
  context: Record<string, unknown>;
};

export type ScenarioDiagnosticsResponse = {
  ok: boolean;
  active_members: string[];
  checks: DiagnosticCheck[];
  warnings: DiagnosticCheck[];
  errors: DiagnosticCheck[];
};

export type CliAgentDetection = {
  name: string;
  label: string;
  executable: string;
  detected: boolean;
  path: string;
};

export type CliAgentDetectionsResponse = {
  agents: CliAgentDetection[];
};

export type Correlation = {
  trace_id: string | null;
  span_id: string | null;
  parent_id: string | null;
  source: string | null;
  person_id: string;
  command: string;
  workflow: string;
  attributes: Record<string, unknown>;
};

export type RuntimeEvent = Correlation & {
  kind: "event";
  type: string;
  payload: Record<string, unknown>;
  timestamp: string;
};

export type CommandRunResponse = {
  trace_id: string;
  output: string;
};

export type TraceSummary = {
  trace_id: string;
  source: string;
  person_id: string;
  command: string;
  workflow: string;
  started_at: string;
  updated_at: string;
  status: string;
  event_count: number;
  log_count: number;
  error_count: number;
  span_count: number;
  attributes: Record<string, unknown>;
};

export type TraceRecord = {
  kind: "event" | "log" | "prompt_trace" | "memory";
  timestamp: string;
  trace_id: string | null;
  span_id: string | null;
  parent_id: string | null;
  call_id: string | null;
  span: string;
  source: string;
  person_id: string;
  command: string;
  workflow: string;
  type: string;
  level: string;
  message: string;
  attributes: Record<string, unknown>;
  payload: Record<string, unknown>;
};

export type TracesResponse = {
  traces: TraceSummary[];
};

export type TraceDetailResponse = {
  trace_id: string;
  summary: TraceSummary | null;
  records: TraceRecord[];
};

export type ActivityHistoryMember = {
  person_id: string;
  name: string;
  person_type: string;
  roles: string[];
};

export type ActivityHistoryLink = {
  kind: "doc" | "issue" | "pull_request" | "commit" | "external";
  label: string;
  url: string;
  timestamp?: string;
};

export type ActivityHistorySession = {
  trace_id: string;
  person_id: string;
  source: string;
  command: string;
  workflow: string;
  title: string;
  mode: "interactive" | "workflow";
  status: string;
  started_at: string;
  ended_at: string;
  duration_seconds: number;
  links: ActivityHistoryLink[];
  rate_limit?: {
    retry_after_at: string;
    retry_after_text: string;
  } | null;
};

export type ActivityHistoryEvent = {
  id: string;
  timestamp: string;
  person_id: string;
  type: "pr_create" | "pr_merge" | "pr_closed" | "push" | "issue_resolve" | "external";
  title: string;
  detail: string;
  url: string;
  links: ActivityHistoryLink[];
};

export type ActivityHistoryResponse = {
  start: string;
  end: string;
  members: ActivityHistoryMember[];
  sessions: ActivityHistorySession[];
  events: ActivityHistoryEvent[];
  unsupported_event_sources: string[];
};

export type MemoryEvent = {
  timestamp: string;
  action: string;
  person_id: string;
  scope: string;
  doc_id: string;
  path: string;
  title: string;
  summary: string;
  kind: string;
  trace_id: string | null;
  run_id: string;
  task_run_id: string;
  source: Array<Record<string, unknown>>;
  changed_fields: string[];
  query_keywords: string[];
  result_count: number | null;
  duration_ms: number | null;
  body_preview: string;
};

export type MemoryEventsResponse = {
  event_count: number;
  events: MemoryEvent[];
};

export type CommandArgumentOption = {
  name: string;
  kind: "positional" | "keyword";
  required: boolean;
  default: string;
};

export type CommandRequirement = {
  kind: "github" | "slack" | "cli_agent" | "llm";
  satisfied: boolean;
  message: string;
};

export type CommandOption = {
  command: string;
  label: string;
  description: string;
  category: "workflow" | "function" | "example" | "custom";
  source: "workspace" | "home" | "template";
  path: string;
  arguments: CommandArgumentOption[];
  supports_raw_args: boolean;
  recommended_input: string;
  requirements: CommandRequirement[];
  // False when a routine candidate still needs caller-supplied input and so
  // cannot run on a schedule. Absent on the general /commands/options listing.
  routine_eligible?: boolean;
};

export type CommandOptionsResponse = {
  options: CommandOption[];
};

export type LaneMap = {
  ready: string;
  working: string;
  done: string;
};

export type ProjectStatusOptions = {
  available: boolean;
  statuses: string[];
};

export type AgentFieldOption = {
  name: string;
  description: string;
};

export type AgentFieldState = {
  available: boolean;
  exists: boolean;
  options: AgentFieldOption[];
  missing: AgentFieldOption[];
};

export type ProjectStatusOptionsRequest = {
  owner: string;
  project_id: string;
  github_project_url: string;
};

export type ProjectSetupRequest = {
  config_dir: string;
  env_file_path: string;
  env_file_option: "skip" | "append" | "overwrite";
  language: "en" | "ja";
  description?: string;
  owner?: string;
  project_id?: string;
  github_project_url?: string;
  lane_map?: LaneMap;
  llm_api_type: string;
  cli_agent: string;
  // provider id -> new API key value to write to the .env
  provider_api_keys?: Record<string, string>;
};

export type ProjectConfig = {
  config_dir: string;
  env_file_path: string;
  language: "en" | "ja";
  description: string;
  llm_api_type: string;
  cli_agent: string;
  github_enabled: boolean;
  github_project_url: string;
  lane_map: LaneMap;
  // provider id -> whether its API key is configured in the .env
  provider_api_keys: Record<string, boolean>;
};

export type ProjectConfigUpdateRequest = {
  config_dir: string;
  env_file_path: string;
  language: "en" | "ja";
  description?: string;
  llm_api_type: string;
  cli_agent: string;
  github_enabled: boolean;
  owner?: string;
  project_id?: string;
  github_project_url?: string;
  lane_map?: LaneMap;
  // provider id -> new API key value to write to the .env
  provider_api_keys?: Record<string, string>;
};

export type MemberPersonType = "human" | "agent";
export type MemberGitHubAccountType = "" | "human" | "machine_user" | "github_apps" | "proxy_agent";
export type ChatParticipationPolicy = "strict" | "social" | "muted";

type MemberWriteRequestBase = {
  config_dir: string;
  env_file_path: string;
  append_env_file?: boolean;
  person_type: MemberPersonType;
  github_account_type: MemberGitHubAccountType;
  person_id: string;
  person_name: string;
  is_active: boolean;
  github_username: string;
  git_email: string;
  roles?: string[];
  speaking_style?: string;
  relationships?: string;
  character?: Record<string, unknown>;
  github_installation_id?: number;
  github_app_id?: number;
  github_private_key_path?: string;
  github_access_token?: string;
  slack_user_id?: string;
  slack_bot_token?: string;
  slack_app_token?: string;
  slack_channels?: string[];
  slack_channel_participation?: Record<string, ChatParticipationPolicy>;
  routine_commands?: string[];
  task_schedules?: MemberTaskSchedule[];
};

export type MemberSetupRequest = MemberWriteRequestBase;

export type MemberResolveRequest = {
  person_type: "human" | "machine_user" | "github_apps" | "proxy_agent";
  identity: string;
};

export type MemberResolveResponse = {
  person_id: string;
  github_username: string;
  github_user_id: number;
  git_email: string;
};

export type MemberConfig = {
  person_id: string;
  person_name: string;
  person_type: MemberPersonType | "";
  github_account_type: MemberGitHubAccountType;
  is_active: boolean;
  github_username: string;
  git_email: string;
  roles: string[];
  speaking_style: string;
  relationships: string;
  character: Record<string, unknown>;
  github_installation_id: number | null;
  github_app_id: number | null;
  github_private_key_path: string;
  has_github_installation_id: boolean;
  has_github_app_id: boolean;
  has_github_private_key_path: boolean;
  has_github_private_key: boolean;
  has_github_access_token: boolean;
  slack_user_id?: string;
  has_slack_bot_token: boolean;
  has_slack_app_token: boolean;
  slack_channels: string[];
  slack_channel_participation: Record<string, ChatParticipationPolicy>;
  routine_commands: string[];
  task_schedules: MemberTaskSchedule[];
  avatar_timestamp?: number;
};

export type MemberTaskSchedule = {
  command: string;
  schedules: string[];
};

export type MemberConfigUpdateRequest = MemberWriteRequestBase & {
  original_person_id: string;
};

export type MemberDeleteRequest = {
  config_dir: string;
  env_file_path: string;
};

export type RuntimeLog = Correlation & {
  kind: "log";
  level: string;
  message: string;
  timestamp: string;
};

export type StreamStatus = "connecting" | "connected" | "disconnected" | "error";

export type RoutineCommandOptionsResponse = {
  options: CommandOption[];
  // Command to seed / pre-select for a new member. Empty when no candidate.
  default_command?: string;
};

export type RoleOption = {
  role_id: string;
  summary: string;
  description: string;
};

export type RoleOptionsResponse = {
  roles: RoleOption[];
};

export type ModelDefinition = {
  path: string;
  provider: string;
  model_class: string;
  model_id: string;
};

export type CliAgentDefinition = {
  path: string;
  name: string;
  env: Record<string, unknown>;
  script: string;
  detected: boolean;
  detected_path: string;
};

export type BrainAssignment = {
  name: string;
  brain_class: string;
  engine: "llm" | "cli";
  target: string;
};

// A selectable LLM provider, discovered server-side from
// `models/<provider>/default.yml`. Single source of truth for the catalog.
export type LlmProviderInfo = {
  provider: string;
  label: string;
  order: number;
  api_key_env: string;
  model_class: string;
  model_id: string;
};

export type IntelligenceConfig = {
  config_dir: string;
  person_id: string | null;
  inherited: boolean;
  model_mapping: Record<string, string>;
  models: ModelDefinition[];
  cli_agent_mapping: Record<string, string>;
  cli_agents: CliAgentDefinition[];
  brain_mapping: BrainAssignment[];
};

export type IntelligenceConfigUpdateRequest = {
  config_dir: string;
  person_id?: string | null;
  inherit_team_defaults?: boolean;
  model_mapping?: Record<string, string>;
  models?: ModelDefinition[];
  cli_agent_mapping?: Record<string, string>;
  cli_agents?: CliAgentDefinition[];
  brain_mapping?: BrainAssignment[];
};

export type ConfigWriteResponse = {
  project: { files: Array<{ path: string; action: string }> } | null;
  member: { files: Array<{ path: string; action: string }> } | null;
  intelligence?: { files: Array<{ path: string; action: string }> } | null;
};

export type ApiErrorPayload = {
  code: string;
  message: string;
  context: Record<string, unknown>;
};

export class ApiRequestError extends Error {
  code: string;
  context: Record<string, unknown>;

  constructor(payload: ApiErrorPayload) {
    super(payload.message);
    this.name = "ApiRequestError";
    this.code = payload.code;
    this.context = payload.context;
  }
}

export async function getConfigStatus(): Promise<ConfigStatus> {
  return request("/config/status");
}

export async function setWorkspace(body: WorkspaceChangeRequest): Promise<ConfigStatus> {
  return request("/workspace", { method: "POST", body });
}

export async function getTeam(): Promise<TeamSummary> {
  return request("/team");
}

export async function getSchedulerStatus(): Promise<RuntimeStatus> {
  return request("/scheduler/status");
}

export async function getRoleOptions(language: "en" | "ja"): Promise<RoleOptionsResponse> {
  return request(`/config/roles?language=${encodeURIComponent(language)}`);
}

export async function startScheduler(body: SchedulerStartRequest): Promise<RuntimeStatus> {
  return request("/scheduler/start", { method: "POST", body });
}

export async function stopScheduler(body: SchedulerStopRequest = {}): Promise<RuntimeStatus> {
  return request("/scheduler/stop", {
    method: "POST",
    body: body.force ? body : undefined,
  });
}

export async function resetChatReceiveState(): Promise<ChatReceiveResetResponse> {
  return request("/chat/receive-state/reset", { method: "POST" });
}

export async function getPromptTrace(limit = 20, path?: string): Promise<PromptTraceStatus> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (path) {
    params.set("path", path);
  }
  return request(`/prompt-trace?${params.toString()}`);
}

export async function updatePromptTrace(
  body: PromptTraceUpdateRequest,
  limit = 20,
): Promise<PromptTraceStatus> {
  return request(`/prompt-trace?limit=${encodeURIComponent(String(limit))}`, {
    method: "PUT",
    body,
  });
}

export async function getRuntimeDebug(): Promise<RuntimeDebugStatus> {
  return request("/runtime/debug");
}

export async function updateRuntimeDebug(
  body: RuntimeDebugUpdateRequest,
): Promise<RuntimeDebugStatus> {
  return request("/runtime/debug", { method: "PUT", body });
}

export async function verify(): Promise<VerifyResponse> {
  return request("/verify", { method: "POST" });
}

export async function runScenarioDiagnostics(
  personId?: string,
): Promise<ScenarioDiagnosticsResponse> {
  const query = personId ? `?person_id=${encodeURIComponent(personId)}` : "";
  return request(`/diagnostics/scenario${query}`, { method: "POST" });
}

export async function getTraces(params?: {
  source?: string;
  personId?: string;
  query?: string;
  attrKey?: string;
  attrValue?: string;
  limit?: number;
}): Promise<TracesResponse> {
  const search = new URLSearchParams();
  if (params?.source) {
    search.set("source", params.source);
  }
  if (params?.personId) {
    search.set("person_id", params.personId);
  }
  if (params?.query) {
    search.set("q", params.query);
  }
  if (params?.attrKey && params?.attrValue) {
    search.set("attr_key", params.attrKey);
    search.set("attr_value", params.attrValue);
  }
  if (params?.limit) {
    search.set("limit", String(params.limit));
  }
  const suffix = search.toString();
  return request(`/diagnostics/traces${suffix ? `?${suffix}` : ""}`);
}

export async function getTraceDetail(traceId: string): Promise<TraceDetailResponse> {
  return request(`/diagnostics/traces/${encodeURIComponent(traceId)}`);
}

export async function getGlobalRecords(limit = 200): Promise<TraceDetailResponse> {
  return request(`/diagnostics/global?limit=${encodeURIComponent(String(limit))}`);
}

export async function getActivityHistory(params: {
  start: string;
  end: string;
  limit?: number;
  refresh?: boolean;
  syncStart?: string;
  syncEnd?: string;
}): Promise<ActivityHistoryResponse> {
  const search = new URLSearchParams({ start: params.start, end: params.end });
  if (params.limit) {
    search.set("limit", String(params.limit));
  }
  if (params.refresh) {
    search.set("refresh", "true");
  }
  if (params.syncStart && params.syncEnd) {
    search.set("sync_start", params.syncStart);
    search.set("sync_end", params.syncEnd);
  }
  return request(`/activity/history?${search.toString()}`);
}

export async function getMemoryEvents(params?: {
  personId?: string;
  docId?: string;
  action?: string;
  traceId?: string;
  source?: string;
  query?: string;
  since?: string;
  until?: string;
  limit?: number;
}): Promise<MemoryEventsResponse> {
  const search = new URLSearchParams();
  if (params?.personId) {
    search.set("person_id", params.personId);
  }
  if (params?.docId) {
    search.set("doc_id", params.docId);
  }
  if (params?.action) {
    search.set("action", params.action);
  }
  if (params?.traceId) {
    search.set("trace_id", params.traceId);
  }
  if (params?.source) {
    search.set("source", params.source);
  }
  if (params?.query) {
    search.set("q", params.query);
  }
  if (params?.since) {
    search.set("since", params.since);
  }
  if (params?.until) {
    search.set("until", params.until);
  }
  if (params?.limit) {
    search.set("limit", String(params.limit));
  }
  const suffix = search.toString();
  return request(`/diagnostics/memory-events${suffix ? `?${suffix}` : ""}`);
}

export async function getCliAgentDetections(): Promise<CliAgentDetectionsResponse> {
  return request("/intelligences/cli-agents/detection");
}

export async function getLlmProviders(): Promise<LlmProviderInfo[]> {
  const response = await request<{ providers: LlmProviderInfo[] }>(
    "/intelligences/model-providers",
  );
  return response.providers;
}

export async function getIntelligenceConfig(personId?: string): Promise<IntelligenceConfig> {
  const query = personId ? `?person_id=${encodeURIComponent(personId)}` : "";
  return request(`/config/intelligences${query}`);
}

export async function updateIntelligenceConfig(
  body: IntelligenceConfigUpdateRequest,
): Promise<ConfigWriteResponse> {
  return request("/config/intelligences", { method: "PUT", body });
}

export async function runCommand(body: {
  command: string;
  args?: string[];
  person?: string;
  message?: string;
  cwd?: string;
}): Promise<CommandRunResponse> {
  return request("/commands/run", { method: "POST", body });
}

export async function getCommandOptions(person?: string): Promise<CommandOptionsResponse> {
  const query = person ? `?person=${encodeURIComponent(person)}` : "";
  return request(`/commands/options${query}`);
}

export async function getRoutineCommandOptions(
  person?: string,
): Promise<RoutineCommandOptionsResponse> {
  const query = person ? `?person=${encodeURIComponent(person)}` : "";
  return request(`/commands/routine-options${query}`);
}

export async function initConfig(body: ProjectSetupRequest): Promise<ConfigWriteResponse> {
  return request("/config/init", { method: "POST", body });
}

export async function getProjectConfig(): Promise<ProjectConfig> {
  return request("/config/project");
}

export async function getProjectStatusOptions(
  body: ProjectStatusOptionsRequest,
): Promise<ProjectStatusOptions> {
  return request("/config/project/status-options", { method: "POST", body });
}

export async function getAgentFieldState(
  body: ProjectStatusOptionsRequest,
): Promise<AgentFieldState> {
  return request("/config/project/agent-field", { method: "POST", body });
}

export async function ensureAgentField(
  body: ProjectStatusOptionsRequest,
): Promise<AgentFieldState> {
  return request("/config/project/agent-field/ensure", { method: "POST", body });
}

export async function updateProjectConfig(
  body: ProjectConfigUpdateRequest,
): Promise<ConfigWriteResponse> {
  return request("/config/project", { method: "PUT", body });
}

export async function addMemberConfig(body: MemberSetupRequest): Promise<ConfigWriteResponse> {
  return request("/config/members", { method: "POST", body });
}

export async function resolveMemberIdentity(
  body: MemberResolveRequest,
): Promise<MemberResolveResponse> {
  return request("/config/members/resolve", { method: "POST", body });
}

export async function getMemberConfig(personId: string): Promise<MemberConfig> {
  return request(`/config/members/${encodeURIComponent(personId)}`);
}

export async function updateMemberConfig(
  personId: string,
  body: MemberConfigUpdateRequest,
): Promise<ConfigWriteResponse> {
  return request(`/config/members/${encodeURIComponent(personId)}`, {
    method: "PUT",
    body,
  });
}

export async function deleteMemberConfig(
  personId: string,
  body: MemberDeleteRequest,
): Promise<ConfigWriteResponse> {
  return request(`/config/members/${encodeURIComponent(personId)}`, {
    method: "DELETE",
    body,
  });
}

export type AvatarMutationResponse = {
  avatar_timestamp: number;
};

export async function uploadMemberAvatar(
  personId: string,
  file: File,
): Promise<AvatarMutationResponse> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${apiBase}/config/members/${encodeURIComponent(personId)}/avatar`, {
    method: "POST",
    headers: {
      "X-GuildBotics-Session-Token": sessionToken,
    },
    body: formData,
  });
  if (!response.ok) {
    throw new ApiRequestError(await readError(response));
  }
  return response.json();
}

export async function importAvatarFromGithub(personId: string): Promise<AvatarMutationResponse> {
  return request(`/config/members/${encodeURIComponent(personId)}/avatar/github`, {
    method: "POST",
  });
}

export async function importAvatarFromSlack(personId: string): Promise<AvatarMutationResponse> {
  return request(`/config/members/${encodeURIComponent(personId)}/avatar/slack`, {
    method: "POST",
  });
}

export function subscribeEvents(
  onEvent: (event: RuntimeEvent) => void,
  onStatus?: (status: StreamStatus) => void,
): () => void {
  const socket = new WebSocket(
    `${websocketBase()}/events?token=${encodeURIComponent(sessionToken)}`,
  );
  onStatus?.("connecting");
  socket.onopen = () => onStatus?.("connected");
  socket.onmessage = (message) => {
    onEvent(JSON.parse(message.data) as RuntimeEvent);
  };
  socket.onerror = () => onStatus?.("error");
  socket.onclose = () => onStatus?.("disconnected");
  return () => socket.close();
}

export function subscribeLogs(
  onLog: (log: RuntimeLog) => void,
  onStatus?: (status: StreamStatus) => void,
): () => void {
  const socket = new WebSocket(`${websocketBase()}/logs?token=${encodeURIComponent(sessionToken)}`);
  onStatus?.("connecting");
  socket.onopen = () => onStatus?.("connected");
  socket.onmessage = (message) => {
    onLog(JSON.parse(message.data) as RuntimeLog);
  };
  socket.onerror = () => onStatus?.("error");
  socket.onclose = () => onStatus?.("disconnected");
  return () => socket.close();
}

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "Content-Type": "application/json",
      "X-GuildBotics-Session-Token": sessionToken,
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (!response.ok) {
    throw new ApiRequestError(await readError(response));
  }
  return response.json();
}

async function readError(response: Response): Promise<ApiErrorPayload> {
  try {
    const payload = (await response.json()) as Partial<ApiErrorPayload>;
    if (payload.code && payload.message) {
      return {
        code: payload.code,
        message: payload.message,
        context: payload.context ?? {},
      };
    }
  } catch {
    // Fall through to the stable fallback below.
  }
  return {
    code: "http_error",
    message: `HTTP ${response.status}`,
    context: {},
  };
}

function websocketBase(): string {
  const url = new URL(apiBase);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString().replace(/\/$/, "");
}
