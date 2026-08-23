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
  workspace: string | null;
  config_dir: string | null;
  project_file: string | null;
  project_file_exists: boolean;
  storage_dir: string | null;
  machine_state_dir?: string | null;
  workspace_state_dir?: string | null;
  workspace_local_dir?: string | null;
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
  // Member a command runs as when none is selected: the configured default,
  // else the first active non-human member in person id order. Empty only when
  // no member can execute commands.
  default_person_id: string;
};

export type RuntimeMemberRoutine = {
  person_id: string;
  last_routine_at: string;
  next_routine_at: string;
};

export type RuntimeUnitStatus = {
  target: "scheduler" | "events";
  state: "starting" | "running" | "stopping" | "stopped" | "failed";
  running: boolean;
  started_at: string | null;
  stopped_at: string | null;
  error: string | null;
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
  member_routines: RuntimeMemberRoutine[];
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
  max_consecutive_errors?: number;
  routine_interval_minutes?: number;
};

export type SchedulerStopRequest = {
  force?: boolean;
};

export type TranscriptSettingsStatus = {
  detail: "standard" | "full";
  retention_days: number;
  sessions_dir: string;
  total_size_bytes: number;
  index_size_bytes: number;
  index_rewrite_threshold_bytes: number;
  memory_size_bytes: number;
  memory_max_size_bytes: number;
};

export type TranscriptSettingsUpdateRequest = {
  detail: "standard" | "full";
  retention_days: number;
};

export type RuntimeDebugStatus = {
  enabled: boolean;
  log_level: string;
  agno_debug: boolean;
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

export type SystemAlert = {
  id: string;
  code:
    | "credential_github"
    | "credential_slack"
    | "credential_cli_agent"
    | "credential_llm"
    | "command_failed"
    | "rate_limited"
    | "scheduler_failed"
    | "worker_stopped";
  severity: "critical" | "warning";
  opened_at: string;
  updated_at: string;
  occurrence_count: number;
  person_id: string;
  command: string;
  trace_id: string;
  actions: Array<"diagnostics" | "setup" | "trace" | "service">;
};

export type SystemAlertsResponse = {
  alerts: SystemAlert[];
};

export type CliAgentDetection = {
  name: string;
  label: string;
  executable: string;
  config_reference: string;
  detected: boolean;
  path: string;
};

export type CliAgentDetectionsResponse = {
  agents: CliAgentDetection[];
};

export type CliAgentUsageWindow = {
  window: string;
  // null for providers that report only the window's reset time.
  used_percent: number | null;
  resets_at: string;
  window_minutes: number | null;
  // Human-readable qualifier beyond the duration (e.g. a per-model budget's
  // model name).
  label: string;
  // Supplementary window: counts toward the limit state but is shown only in
  // the expanded usage detail, not as its own meter.
  detail: boolean;
};

export type CliAgentUsage = {
  agent: string;
  windows: CliAgentUsageWindow[];
  limit_reached: boolean;
  checked_at: string;
};

export type CliAgentUsagesResponse = {
  usages: CliAgentUsage[];
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

export type HotkeySettings = {
  quick_run: string;
  /** Logical command name to accelerator. */
  commands: Record<string, string>;
};

export type CommandRunResponse = {
  trace_id: string;
  output: string;
};

export type CommandInputFileResponse = {
  path: string;
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
  kind: "event" | "log" | "io" | "memory";
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
  presentation: TracePresentation;
};

export type TracePresentation = {
  label_key: string;
  label_fallback: string;
  message_key: string;
  message: string;
  message_params: Record<string, unknown>;
  tone: string;
  // Resolved effort level of this record, "" when it stated none.
  effort: string;
};

export type TracesResponse = {
  traces: TraceSummary[];
};

export type TraceDetailResponse = {
  trace_id: string;
  summary: TraceSummary | null;
  records: TraceRecord[];
  transcript_available?: boolean;
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

/**
 * Enough to find a rejected change's stashed commit again. The stashed content
 * is deliberately absent: recovery is a manual procedure on the source device.
 */
export type ActivityHistoryRejection = {
  rejection_id: string;
  paths: string[];
  source_device_id: string;
};

export type ActivityHistoryEvent = {
  id: string;
  timestamp: string;
  person_id: string;
  type:
    | "pr_create"
    | "pr_merge"
    | "pr_closed"
    | "push"
    | "issue_create"
    | "issue_resolve"
    | "sync_rejected"
    | "external";
  title: string;
  detail: string;
  url: string;
  links: ActivityHistoryLink[];
  /** Present only on `sync_rejected`. */
  rejection?: ActivityHistoryRejection | null;
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

export type CommandInputs = {
  defined_args: "auto" | "hidden";
  extra_args: "hidden" | "optional";
  message: "hidden" | "optional" | "required";
};

export type CommandOption = {
  command: string;
  label: string;
  description: string;
  category: "workflow" | "function" | "example" | "custom";
  source: "workspace" | "template";
  path: string;
  arguments: CommandArgumentOption[];
  inputs: CommandInputs;
  requirements: CommandRequirement[];
  // False when a routine candidate still needs caller-supplied input and so
  // cannot run on a schedule. Absent on the general /commands/options listing.
  routine_eligible?: boolean;
};

export type CommandOptionsResponse = {
  options: CommandOption[];
};

export type CommandFileFormat = "markdown" | "python" | "shell" | "yaml";

export type CommandFileSummary = {
  id: string;
  command: string;
  label: string;
  description: string;
  relative_path: string;
  format: CommandFileFormat;
};

export type CommandFileDetail = CommandFileSummary & {
  content: string;
  revision: string;
  arguments: CommandArgumentOption[];
  inputs: CommandInputs;
};

export type CommandFilesResponse = {
  files: CommandFileSummary[];
};

export type AssistantTurnRequest = {
  conversation_id: string;
  message: string;
  person?: string;
};

export type AssistantTurnResponse = {
  trace_id: string;
  message: string;
};

export type CommandAuthoringResponse = AssistantTurnResponse & {
  action: "answer" | "propose_changes";
  changes: CommandAuthoringChange[];
};

export type CommandAuthoringChange = {
  operation: "create" | "update";
  command: string;
  format: CommandFileFormat;
  relative_path: string;
  content: string;
  file_id: string;
  expected_revision: string;
};

export type CommandAuthoringRequest = AssistantTurnRequest & {
  mode: "create" | "edit";
  command?: string;
  format?: CommandFileFormat;
  content?: string;
  file_id?: string;
  revision?: string;
};

export type CommandAuthoringApplyResponse = {
  files: CommandFileDetail[];
};

export type TroubleshootingFocus = {
  view: "trace" | "global" | "memory";
  trace_id?: string;
  source?: string;
  person_id?: string;
  query?: string;
  /** Set when the user asked about one specific record of the execution. */
  span_id?: string;
  call_id?: string;
  record_timestamp?: string;
  record_type?: string;
  record_message?: string;
};

export type TroubleshootingRequest = AssistantTurnRequest & {
  focus?: TroubleshootingFocus;
};

export type TroubleshootingResponse = AssistantTurnResponse & {
  trace_ids: string[];
};

export type CommandFileBlockingCode =
  | "command_file_changed"
  | "command_file_invalid_source"
  | "command_file_shadowed"
  | "command_requirement_missing"
  | "person_not_found";

export type CommandFileExecutionStatus = {
  matches_selected_file: boolean;
  requirements: CommandRequirement[];
  blocking_code: CommandFileBlockingCode | null;
  blocking_context: Record<string, string>;
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
  language: "en" | "ja";
  description?: string;
  owner?: string;
  project_id?: string;
  github_project_url?: string;
  lane_map?: LaneMap;
  llm_api_type: string;
  cli_agent: string;
  provider_api_keys?: Record<string, string>;
};

export type ProjectConfig = {
  config_dir: string;
  /** Config-relative path -> revision, sent back with the save. */
  revisions: ConfigRevisions;
  language: "en" | "ja";
  description: string;
  llm_api_type: string;
  cli_agent: string;
  github_enabled: boolean;
  github_project_url: string;
  lane_map: LaneMap;
  provider_api_keys: Record<string, boolean>;
};

export type ProjectConfigUpdateRequest = {
  config_dir: string;
  expected_revisions?: ConfigRevisions;
  language: "en" | "ja";
  description?: string;
  llm_api_type: string;
  cli_agent: string;
  github_enabled: boolean;
  owner?: string;
  project_id?: string;
  github_project_url?: string;
  lane_map?: LaneMap;
  provider_api_keys?: Record<string, string>;
};

export type MemberPersonType = "human" | "agent";
export type MemberGitHubAccountType = "" | "human" | "machine_user" | "github_apps" | "proxy_agent";
export type ChatParticipationPolicy = "strict" | "social" | "muted";

type MemberWriteRequestBase = {
  config_dir: string;
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

export type GitHubAppRegistrationStartRequest = {
  app_name: string;
  organization?: string;
};

export type GitHubAppRegistrationStatus = {
  state: string;
  status: "pending" | "converted" | "installed";
  app_name: string;
  start_url: string;
  slug: string;
  app_id: number | null;
  html_url: string;
  github_username: string;
  git_email: string;
  private_key_path: string;
  installation_id: number | null;
  installation_page_url: string;
  installation_check_error: string;
};

export type SlackAppRegistrationStartRequest = {
  app_name: string;
};

export type SlackAppRegistrationStatus = {
  app_name: string;
  registration_url: string;
  app_directory_url: string;
};

export type SlackTokenVerifyRequest = {
  bot_token: string;
  app_token: string;
  person_id?: string;
  channels?: string[];
};

export type SlackChannelVerification = {
  channel: string;
  ok: boolean;
  error: string;
};

// Which token was actually checked: the one typed in, the one already saved
// for the member (an empty field keeps it), or neither.
export type SlackTokenSource = "input" | "stored" | "none";

export type SlackTokenVerifyResponse = {
  bot_ok: boolean;
  bot_user_id: string;
  bot_display_name: string;
  workspace: string;
  bot_error: string;
  bot_source: SlackTokenSource;
  scopes_ok: boolean;
  scope_error: string;
  scope_needed: string;
  app_token_ok: boolean;
  app_token_error: string;
  app_token_source: SlackTokenSource;
  channels: SlackChannelVerification[];
};

export type MemberConfig = {
  revisions: ConfigRevisions;
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
  has_github_installation_id: boolean;
  has_github_app_id: boolean;
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
  expected_revisions?: ConfigRevisions;
};

export type MemberDeleteRequest = {
  config_dir: string;
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

// Effort level -> provider-specific settings. Values are provider-shaped
// (string, number, or nested object), so they are kept as-is rather than
// flattened to strings.
export type EffortOverlay = Record<string, Record<string, unknown>>;

// One editable setting a provider accepts inside an effort level. The editor
// renders a control from `type` alone and holds no provider vocabulary itself.
export type EffortFieldSpec = {
  key: string;
  type: "enum" | "integer" | "boolean" | "string" | "model_id";
  values: string[];
  minimum: number | null;
  maximum: number | null;
};

export type ModelDefinition = {
  path: string;
  provider: string;
  model_class: string;
  // Settings that always apply, whatever effort was asked for. `id` names the
  // model, so it is one of these rather than a field of its own.
  parameters: Record<string, unknown>;
  effort: EffortOverlay;
  // What applies while `effort` is empty; shown read-only, never written back.
  // Server-supplied display metadata, absent on a slot built client-side.
  inherited_effort?: EffortOverlay;
  effort_fields?: EffortFieldSpec[];
};

export type CliAgentDefinition = {
  path: string;
  name: string;
  detected: boolean;
  detected_path: string;
  // Settings that always apply, whatever effort was asked for.
  parameters?: Record<string, unknown>;
  effort: EffortOverlay;
  inherited_effort?: EffortOverlay;
  effort_fields?: EffortFieldSpec[];
  // False when the tool's protocol has nowhere to put these settings, so the
  // editor explains rather than collecting configuration that would be dropped.
  effort_supported?: boolean;
};

export type BrainAssignment = {
  name: string;
  brain_class: string;
  engine: "llm" | "cli";
  target: string;
};

export type NativeAgentFilesystemAccess = "workspace" | "host";

// One entry per native adapter; the backend rejects unknown keys.
export type NativeAgentPolicySettings = {
  codex: { filesystem_access: NativeAgentFilesystemAccess };
  grok: { filesystem_access: NativeAgentFilesystemAccess };
  copilot: { filesystem_access: NativeAgentFilesystemAccess };
};

export const NATIVE_POLICY_ADAPTERS = ["codex", "grok", "copilot"] as const;

export type NativeAgentPolicyAdapter = (typeof NATIVE_POLICY_ADAPTERS)[number];

// A selectable LLM provider, discovered server-side from
// `models/<provider>/default.yml`. Single source of truth for the catalog.
export type LlmProviderInfo = {
  provider: string;
  label: string;
  order: number;
  api_key_env: string;
  model_class: string;
  model_id: string;
  // The provider's default effort overlay, seeded when a slot switches to it.
  effort: EffortOverlay;
  // The settings this provider accepts, so a brand-new slot still gets typed
  // controls instead of falling back to raw JSON.
  effort_fields: EffortFieldSpec[];
};

export type IntelligenceConfig = {
  config_dir: string;
  revisions: ConfigRevisions;
  person_id: string | null;
  inherited: boolean;
  model_mapping: Record<string, string>;
  models: ModelDefinition[];
  cli_agent_mapping: Record<string, string>;
  cli_agents: CliAgentDefinition[];
  brain_mapping: BrainAssignment[];
  native_agent_policy: NativeAgentPolicySettings;
  // Team-owned slot/feature names. A member may override their value but cannot
  // delete or rename them. Empty for the team scope.
  inherited_model_slots?: string[];
  inherited_cli_slots?: string[];
  inherited_brain_features?: string[];
};

export type IntelligenceConfigUpdateRequest = {
  config_dir: string;
  expected_revisions?: ConfigRevisions;
  person_id?: string | null;
  inherit_team_defaults?: boolean;
  model_mapping?: Record<string, string>;
  models?: ModelDefinition[];
  cli_agent_mapping?: Record<string, string>;
  cli_agents?: CliAgentDefinition[];
  brain_mapping?: BrainAssignment[];
  native_agent_policy?: NativeAgentPolicySettings;
};

export type ConfigWriteResponse = {
  /** Where the written files now stand, for the next save from this screen. */
  revisions: ConfigRevisions;
  project: { files: Array<{ path: string; action: string }> } | null;
  member: { files: Array<{ path: string; action: string }> } | null;
  intelligence?: { files: Array<{ path: string; action: string }> } | null;
};

export type ApiErrorPayload = {
  code: string;
  message: string;
  context: Record<string, unknown>;
};

/** Config-relative path -> the revision the screen was composed against. */
export type ConfigRevisions = Record<string, string>;

/**
 * The seven states the sidebar shows, ordered so that the one needing the user
 * comes first when several are true at once.
 */
export type SyncState =
  | "disabled"
  | "idle"
  | "fetching"
  | "reconciling"
  | "pushing"
  | "unreachable"
  | "invalid_shared_state"
  | "update_required";

export type UnsendableChange = {
  path: string;
  reason: string;
};

/**
 * A local change the hub did not accept, still held on this machine.
 *
 * The content is not here and is not fetchable: recovery is a manual procedure
 * on this device, so what travels is what makes the stashed commit findable.
 */
export type RejectedChange = {
  rejection_id: string;
  occurred_at: string;
  paths: string[];
};

export type WorkspaceSyncStatus = {
  enabled: boolean;
  workspace_id: string | null;
  device_id: string | null;
  hub_url: string | null;
  state: SyncState;
  local_head: string | null;
  remote_head: string | null;
  ahead_count: number;
  behind_count: number;
  unsendable_changes: UnsendableChange[];
  rejected_changes: RejectedChange[];
  last_success_at: string | null;
  last_error_code: string | null;
};

/** Empty `endpoint` means this machine, which needs no network and no host key. */
export type HubTarget = {
  endpoint: string;
};

export type HubStatus = {
  hosted: boolean;
  hub_root: string;
  hub_id: string | null;
  created_at: string | null;
  ssh_endpoint: string | null;
  workspace_ids: string[];
};

export type HubConnection = {
  endpoint: string;
  is_local: boolean;
  host_key_fingerprints: string[];
  host_key_trusted: boolean;
  /** A key is stored for this host, and the host is offering a different one. */
  host_key_changed: boolean;
  workspace_ids: string[];
};

export type HubTrustRequest = {
  endpoint: string;
  /** The fingerprint the user actually looked at. Trusting without one is refused. */
  fingerprint: string;
};

export type DeviceSshKey = {
  exists: boolean;
  path: string | null;
  public_key: string;
  fingerprint: string;
};

export type WorkspaceSyncEnableRequest = {
  hub: HubTarget;
  /** Names the hub workspace to join; empty registers this one on the hub. */
  workspace_id?: string;
};

export type WorkspaceSyncPreview = {
  hub_workspace_id: string | null;
  workspace_id: string;
  mode: "join" | "reconnect";
  hub_only: string[];
  device_only: string[];
  differing: string[];
  unsendable_changes: UnsendableChange[];
};

export type WorkspaceSyncCloneRequest = {
  hub: HubTarget;
  workspace_id: string;
  workspace_dir: string;
};

export type WorkspaceDevice = {
  device_id: string;
  display_name: string;
  os: string;
  joined_at: string;
  status: string;
  ssh_public_key_fingerprint: string;
  is_self: boolean;
};

export type WorkspaceDevices = {
  devices: WorkspaceDevice[];
  device_id: string;
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

export async function getWorkspaceSyncStatus(): Promise<WorkspaceSyncStatus> {
  return request("/workspace/sync");
}

export async function previewWorkspaceSync(
  body: WorkspaceSyncEnableRequest,
): Promise<WorkspaceSyncPreview> {
  return request("/workspace/sync/preview", { method: "POST", body });
}

export async function enableWorkspaceSync(
  body: WorkspaceSyncEnableRequest,
): Promise<WorkspaceSyncStatus> {
  return request("/workspace/sync/enable", { method: "POST", body });
}

export async function changeWorkspaceSyncHub(
  body: WorkspaceSyncEnableRequest,
): Promise<WorkspaceSyncStatus> {
  return request("/workspace/sync/hub", { method: "POST", body });
}

export async function retryWorkspaceSync(): Promise<WorkspaceSyncStatus> {
  return request("/workspace/sync/retry", { method: "POST" });
}

export async function cloneWorkspaceFromHub(
  body: WorkspaceSyncCloneRequest,
): Promise<ConfigStatus> {
  return request("/workspace/sync/clone", { method: "POST", body });
}

export async function getHubStatus(): Promise<HubStatus> {
  return request("/hub");
}

export async function createHub(): Promise<HubStatus> {
  return request("/hub", { method: "POST" });
}

export async function inspectHub(body: HubTarget): Promise<HubConnection> {
  return request("/hub/inspect", { method: "POST", body });
}

export async function trustHub(body: HubTrustRequest): Promise<HubConnection> {
  return request("/hub/trust", { method: "POST", body });
}

export async function getDeviceSshKey(): Promise<DeviceSshKey> {
  return request("/hub/ssh-key");
}

export async function createDeviceSshKey(): Promise<DeviceSshKey> {
  return request("/hub/ssh-key", { method: "POST" });
}

export async function getWorkspaceDevices(): Promise<WorkspaceDevices> {
  return request("/workspace/devices");
}

export async function renameThisDevice(displayName: string): Promise<WorkspaceDevices> {
  return request("/workspace/devices/self", {
    method: "POST",
    body: { display_name: displayName },
  });
}

export async function getConfigStatus(): Promise<ConfigStatus> {
  return request("/config/status");
}

export async function setWorkspace(body: WorkspaceChangeRequest): Promise<ConfigStatus> {
  return request("/workspace", { method: "POST", body });
}

export async function discardWorkspaceSyncRejection(
  rejectionId: string,
): Promise<WorkspaceSyncStatus> {
  return request(`/workspace/sync/rejections/${encodeURIComponent(rejectionId)}/discard`, {
    method: "POST",
  });
}

export async function getHotkeys(): Promise<HotkeySettings> {
  return request("/hotkeys");
}

export async function updateHotkeys(body: HotkeySettings): Promise<HotkeySettings> {
  return request("/hotkeys", { method: "PUT", body });
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

export async function getTranscriptSettings(): Promise<TranscriptSettingsStatus> {
  return request("/transcripts/settings");
}

export async function updateTranscriptSettings(
  body: TranscriptSettingsUpdateRequest,
): Promise<TranscriptSettingsStatus> {
  return request("/transcripts/settings", {
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

export async function getSystemAlerts(): Promise<SystemAlertsResponse> {
  return request("/system-alerts");
}

export async function dismissSystemAlert(alertId: string): Promise<SystemAlertsResponse> {
  return request("/system-alerts/dismiss", {
    method: "POST",
    body: { alert_id: alertId },
  });
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

export async function getCliAgentUsage(): Promise<CliAgentUsagesResponse> {
  return request("/intelligences/cli-agents/usage");
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
  expected_command_file_id?: string;
  expected_command_file_revision?: string;
}): Promise<CommandRunResponse> {
  return request("/commands/run", { method: "POST", body });
}

export async function uploadCommandInputFile(file: File): Promise<CommandInputFileResponse> {
  return uploadFile("/commands/input-files", file);
}

export async function authorCommand(
  body: CommandAuthoringRequest,
): Promise<CommandAuthoringResponse> {
  return request("/commands/author", { method: "POST", body });
}

export async function applyCommandAuthoring(
  changes: CommandAuthoringChange[],
): Promise<CommandAuthoringApplyResponse> {
  return request("/commands/author/apply", {
    method: "POST",
    body: { changes },
  });
}

export async function troubleshoot(body: TroubleshootingRequest): Promise<TroubleshootingResponse> {
  return request("/diagnostics/troubleshoot", { method: "POST", body });
}

export async function getCommandOptions(person?: string): Promise<CommandOptionsResponse> {
  const query = person ? `?person=${encodeURIComponent(person)}` : "";
  return request(`/commands/options${query}`);
}

export async function listCommandFiles(): Promise<CommandFilesResponse> {
  return request("/commands/files");
}

export async function getCommandFile(fileId: string): Promise<CommandFileDetail> {
  return request(`/commands/files/${encodeURIComponent(fileId)}`);
}

export async function createCommandFile(body: {
  command: string;
  format: CommandFileFormat;
  content?: string;
}): Promise<CommandFileDetail> {
  return request("/commands/files", { method: "POST", body });
}

export async function updateCommandFile(
  fileId: string,
  body: { content: string; expected_revision: string },
): Promise<CommandFileDetail> {
  return request(`/commands/files/${encodeURIComponent(fileId)}`, { method: "PUT", body });
}

export async function deleteCommandFile(
  fileId: string,
  expectedRevision: string,
): Promise<CommandFilesResponse> {
  const query = `?expected_revision=${encodeURIComponent(expectedRevision)}`;
  return request(`/commands/files/${encodeURIComponent(fileId)}${query}`, {
    method: "DELETE",
  });
}

export async function getCommandFileExecutionStatus(
  fileId: string,
  params: { person?: string; expected_revision: string },
): Promise<CommandFileExecutionStatus> {
  const query = new URLSearchParams({ expected_revision: params.expected_revision });
  if (params.person) {
    query.set("person", params.person);
  }
  return request(`/commands/files/${encodeURIComponent(fileId)}/execution-status?${query}`);
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

export async function updateDefaultPerson(personId: string): Promise<ConfigWriteResponse> {
  return request("/config/project/default-person", {
    method: "PUT",
    body: { person_id: personId },
  });
}

export async function addMemberConfig(body: MemberSetupRequest): Promise<ConfigWriteResponse> {
  return request("/config/members", { method: "POST", body });
}

export async function resolveMemberIdentity(
  body: MemberResolveRequest,
): Promise<MemberResolveResponse> {
  return request("/config/members/resolve", { method: "POST", body });
}

export async function startGitHubAppRegistration(
  body: GitHubAppRegistrationStartRequest,
): Promise<GitHubAppRegistrationStatus> {
  return request("/config/members/github-app/registrations", {
    method: "POST",
    body: { ...body, callback_base_url: apiBase },
  });
}

export async function getGitHubAppRegistration(
  state: string,
): Promise<GitHubAppRegistrationStatus> {
  return request(`/config/members/github-app/registrations/${encodeURIComponent(state)}`);
}

export async function startSlackAppRegistration(
  body: SlackAppRegistrationStartRequest,
): Promise<SlackAppRegistrationStatus> {
  return request("/config/members/slack-app/registrations", { method: "POST", body });
}

export async function verifySlackTokens(
  body: SlackTokenVerifyRequest,
): Promise<SlackTokenVerifyResponse> {
  return request("/config/members/slack-app/verify", { method: "POST", body });
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
  return uploadFile(`/config/members/${encodeURIComponent(personId)}/avatar`, file);
}

async function uploadFile<T>(path: string, file: File): Promise<T> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${apiBase}${path}`, {
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
