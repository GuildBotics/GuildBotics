import type { WorkspaceSyncStatus } from "../api/client";

/**
 * What the sidebar says about synchronization.
 *
 * The backend reports the queue's own state plus counts; this collapses them
 * into the one line a person reads at a glance. Several can be true at once --
 * an unreachable hub while changes wait to be sent, say -- so the order below
 * is the order of precedence, and it puts what the user has to act on first.
 */
export type SyncIndicatorState =
  | "update_required"
  | "invalid_shared_state"
  | "unreachable"
  | "unsendable"
  | "receiving"
  | "sending"
  | "synced"
  | "disabled";

/** States the user is expected to do something about. */
const NEEDS_ATTENTION: ReadonlySet<SyncIndicatorState> = new Set([
  "update_required",
  "invalid_shared_state",
  "unreachable",
  "unsendable",
]);

export function syncIndicatorState(status: WorkspaceSyncStatus | undefined): SyncIndicatorState {
  if (!status?.enabled) {
    return "disabled";
  }
  // A newer build wrote something this one cannot read, so nothing else it
  // reports about the shared content can be trusted.
  if (status.state === "update_required") {
    return "update_required";
  }
  if (status.state === "invalid_shared_state") {
    return "invalid_shared_state";
  }
  if (status.state === "unreachable") {
    return "unreachable";
  }
  // Changes that cannot leave this machine outrank ones merely queued: waiting
  // will not clear them, and only the user can.
  if (status.unsendable_changes.length > 0) {
    return "unsendable";
  }
  if (status.state === "fetching" || status.behind_count > 0) {
    return "receiving";
  }
  if (status.state === "pushing" || status.state === "reconciling" || status.ahead_count > 0) {
    return "sending";
  }
  return "synced";
}

export function syncNeedsAttention(state: SyncIndicatorState): boolean {
  return NEEDS_ATTENTION.has(state);
}

/** Whether the user can usefully ask for another attempt right now. */
export function syncCanRetry(state: SyncIndicatorState): boolean {
  return state === "unreachable" || state === "invalid_shared_state";
}

export type SyncTone = "danger" | "warning" | "info" | "success" | "neutral";

export function syncTone(state: SyncIndicatorState): SyncTone {
  switch (state) {
    case "update_required":
    case "invalid_shared_state":
      return "danger";
    case "unreachable":
    case "unsendable":
      return "warning";
    case "receiving":
    case "sending":
      return "info";
    case "synced":
      return "success";
    case "disabled":
      return "neutral";
  }
}
