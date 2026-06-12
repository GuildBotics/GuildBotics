import type { RuntimeEvent, RuntimeLog, TraceRecord } from "../api/client";

const CORRELATION_DEFAULTS = {
  trace_id: null,
  span_id: null,
  parent_id: null,
  source: null,
  person_id: "",
  command: "",
  workflow: "",
  attributes: {},
};

export function makeRuntimeEvent(overrides: Partial<RuntimeEvent> = {}): RuntimeEvent {
  return {
    kind: "event",
    ...CORRELATION_DEFAULTS,
    type: "command.started",
    payload: {},
    timestamp: "2026-06-04T01:00:00Z",
    ...overrides,
  };
}

export function makeRuntimeLog(overrides: Partial<RuntimeLog> = {}): RuntimeLog {
  return {
    kind: "log",
    ...CORRELATION_DEFAULTS,
    level: "INFO",
    message: "",
    timestamp: "2026-06-04T01:00:00Z",
    ...overrides,
  };
}

export function makeTraceRecord(overrides: Partial<TraceRecord> = {}): TraceRecord {
  return {
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
    ...overrides,
  };
}
