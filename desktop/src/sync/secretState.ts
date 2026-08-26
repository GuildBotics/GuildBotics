import type { SecretStatus, WorkspaceSecrets } from "../api/client";

/**
 * How the Secret list is presented.
 *
 * Which transfer a key can take is decided by the backend and arrives on each
 * key (`can_send` / `can_fetch`) and on the payload (`sendable_keys` /
 * `fetchable_keys`), so this file maps states onto the screen and settles
 * nothing. Working the same answer out here as well is how a button comes to
 * offer a transfer the transfer itself refuses.
 */

/**
 * The one query every screen reads the Secret states from. Sharing the key is
 * what keeps the provider hints, the summary band, and the list on one answer
 * -- and to one request, however many of them are on screen.
 */
export const SECRETS_QUERY_KEY = ["workspace-secrets"];

/**
 * How often that answer is renewed.
 *
 * Reading it asks the hub what it holds, which on another machine is an SSH
 * connection of its own. The states change when a person types a credential or
 * a transfer runs, and both of those update this query directly, so the poll is
 * only there to notice what another machine did -- which arrives through
 * synchronization on its own schedule anyway.
 */
export const SECRETS_REFETCH_MS = 60000;

/** How a state is coloured: nothing to do, act when convenient, act now. */
export type SecretTone = "ok" | "warning" | "danger";

const TONES: Record<SecretStatus, SecretTone> = {
  ready: "ok",
  missing: "warning",
  outdated: "warning",
  pending_send: "warning",
  conflict: "danger",
  unconfirmed: "danger",
  hub_behind: "warning",
};

export function secretTone(status: SecretStatus): SecretTone {
  return TONES[status] ?? "warning";
}

/** True when the key is not simply in step with the other machines. */
export function secretNeedsAttention(status: SecretStatus): boolean {
  return status !== "ready";
}

/**
 * The one thing wrong that a summary should name, or null when nothing is.
 *
 * A locked secret store comes first because every transfer fails while it
 * lasts, and an unreachable hub next because no transfer can even start.
 */
export function secretAlert(
  secrets: WorkspaceSecrets | undefined,
): "local_locked" | "hub_locked" | "hub_unreachable" | "attention" | null {
  if (!secrets?.enabled) return null;
  if (secrets.secret_store.locked) return "local_locked";
  if (secrets.hub_secret_store?.locked) return "hub_locked";
  if (!secrets.hub_reachable) return "hub_unreachable";
  return secrets.attention_count > 0 ? "attention" : null;
}
