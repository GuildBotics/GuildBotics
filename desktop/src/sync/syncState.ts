import type { SyncScope, WorkspaceSyncStatus } from "../api/client";

/**
 * What the sidebar says about synchronization, in order of precedence.
 *
 * The backend reports the queue's own state plus counts; this collapses them
 * into the one line a person reads at a glance. Several can be true at once --
 * an unreachable hub while changes wait to be sent, say -- so the order here is
 * the order of precedence, and it puts what the user has to act on first. The
 * same order decides which of the two scopes' states the sidebar shows.
 */
const SYNC_INDICATOR_STATES = [
  "update_required",
  "invalid_shared_state",
  "not_connected",
  "unreachable",
  "local_error",
  "unsendable",
  "receiving",
  "sending",
  "synced",
  "disabled",
] as const;

export type SyncIndicatorState = (typeof SYNC_INDICATOR_STATES)[number];

/** The scopes in the order the screens list them: identities first, as the secrets do. */
export const SYNC_SCOPES: readonly SyncScope[] = ["identities", "workspace"];

/**
 * The one query every screen reads one scope's synchronization state from, so
 * the band, the sidebar, and the settings stay on one answer and one request.
 */
export const syncQueryKey = (scope: SyncScope) => ["workspace-sync", scope];

export const SYNC_REFETCH_MS = 5000;

/** States the user is expected to do something about. */
const NEEDS_ATTENTION: ReadonlySet<SyncIndicatorState> = new Set([
  "update_required",
  "invalid_shared_state",
  "not_connected",
  "unreachable",
  "local_error",
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
  // A cycle that failed, whether at the hub or on a file of this machine's,
  // sent nothing at all, so it outranks what was merely held back. So does a
  // hub the identities are not on, which nothing of theirs reaches.
  if (
    status.state === "not_connected" ||
    status.state === "unreachable" ||
    status.state === "local_error"
  ) {
    return status.state;
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

/** The state that comes first among several, by the precedence above. */
export function mostUrgentSyncState(states: SyncIndicatorState[]): SyncIndicatorState {
  return SYNC_INDICATOR_STATES.find((state) => states.includes(state)) ?? "disabled";
}

export function syncNeedsAttention(state: SyncIndicatorState): boolean {
  return NEEDS_ATTENTION.has(state);
}

/** Whether the user can usefully ask for another attempt right now. */
export function syncCanRetry(state: SyncIndicatorState): boolean {
  return (
    state === "unreachable" ||
    state === "local_error" ||
    state === "invalid_shared_state" ||
    state === "not_connected"
  );
}

export type SyncTone = "danger" | "warning" | "info" | "success" | "neutral";

export function syncTone(state: SyncIndicatorState): SyncTone {
  switch (state) {
    case "update_required":
    case "invalid_shared_state":
      return "danger";
    case "not_connected":
    case "unreachable":
    case "local_error":
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

/** One scope's status and what it comes to on screen. */
export type ScopedSyncStatus = {
  scope: SyncScope;
  status: WorkspaceSyncStatus | undefined;
  state: SyncIndicatorState;
};

/**
 * The warnings the band shows: one per state that needs the user, carrying
 * every scope in it. A hub that is down puts both scopes in the same state,
 * and that is one problem to read about, not two.
 */
export function syncAttentionGroups(scoped: ScopedSyncStatus[]): ScopedSyncStatus[][] {
  const groups = new Map<SyncIndicatorState, ScopedSyncStatus[]>();
  for (const item of scoped) {
    if (syncNeedsAttention(item.state)) {
      groups.set(item.state, [...(groups.get(item.state) ?? []), item]);
    }
  }
  return [...groups.values()];
}
