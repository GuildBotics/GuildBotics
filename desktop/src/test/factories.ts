import type {
  ConfigStatus,
  ProjectConfig,
  RuntimeStatus,
  RuntimeUnitStatus,
  TraceRecord,
} from "../api/client";

export function makeTraceRecord(overrides: Partial<TraceRecord> = {}): TraceRecord {
  const record: TraceRecord = {
    kind: "event",
    timestamp: "2026-06-04T01:00:00Z",
    trace_id: "trace-1",
    span_id: null,
    parent_id: null,
    call_id: null,
    span: "",
    source: "manual",
    person_id: "",
    command: "",
    workflow: "",
    type: "",
    level: "",
    message: "",
    attributes: {},
    payload: {},
    presentation: overrides.presentation ?? {
      label_key: "",
      label_fallback: overrides.level || overrides.type || "Event",
      message_key: "",
      message:
        overrides.message ||
        (typeof overrides.payload?.message === "string" ? overrides.payload.message : "") ||
        overrides.type ||
        "event",
      message_params: {},
      effort: "",
      tone: "neutral",
    },
    ...overrides,
  };
  return record;
}

export function configStatus(overrides: Partial<ConfigStatus> = {}): ConfigStatus {
  return {
    cwd: "/workspace",
    workspace: "/workspace",
    config_dir: "/workspace/.guildbotics/config",
    project_file: "/workspace/.guildbotics/config/project.yml",
    project_file_exists: true,
    storage_dir: "/workspace/.guildbotics",
    ...overrides,
  };
}

export function projectConfig(overrides: Partial<ProjectConfig> = {}): ProjectConfig {
  return {
    config_dir: "/workspace/.guildbotics/config",
    revisions: {},
    language: "en",
    description: "Demo project",
    llm_api_type: "openai",
    cli_agent: "codex",
    services: {},
    provider_api_keys: { openai: true, gemini: false, anthropic: false },
    ...overrides,
  };
}

export function runtimeUnit(
  target: "scheduler" | "events",
  overrides: Partial<RuntimeUnitStatus> = {},
): RuntimeUnitStatus {
  return {
    target,
    state: "stopped",
    running: false,
    started_at: null,
    stopped_at: null,
    error: null,
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
    events_auth_failed_count: 0,
    events_auth_failed_persons: [],
    events_missing_credential_count: 0,
    events_missing_credential_persons: [],
    events_chat_provider: "",
    member_routines: [],
    member_pull_requests: {},
    ...overrides,
  };
}

export function runtimeStatus(overrides: Partial<RuntimeStatus> = {}): RuntimeStatus {
  return {
    scheduler: runtimeUnit("scheduler"),
    events: runtimeUnit("events"),
    has_active_work: false,
    ...overrides,
  };
}
