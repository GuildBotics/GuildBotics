// Shared trace/session status vocabulary for the diagnostics executions view
// (App.tsx) and the Activity History view (activity/ActivityHistory.tsx), so
// both screens color and terminalize `retry_scheduled` / `abandoned` /
// `incomplete` / `awaiting_checks` the same way instead of re-deriving the
// mapping twice.

export function traceStatusColor(status: string): string {
  if (status === "success") {
    return "success";
  }
  if (status === "failed" || status === "abandoned" || status === "incomplete") {
    return "danger";
  }
  if (status === "retry_scheduled") {
    return "warning";
  }
  if (status === "running" || status === "awaiting_checks") {
    return "info";
  }
  return "neutral";
}

const TERMINAL_TRACE_STATUSES = new Set([
  "success",
  "failed",
  "retry_scheduled",
  "abandoned",
  "incomplete",
  "awaiting_checks",
]);

export function isTerminalTraceStatus(status: string): boolean {
  return TERMINAL_TRACE_STATUSES.has(status);
}
