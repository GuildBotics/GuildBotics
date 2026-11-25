import type { TraceRecord } from "../api/client";

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
