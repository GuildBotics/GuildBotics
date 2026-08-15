import { ApiRequestError } from "../api/client";

/**
 * Whether a save was refused because the screen had been composed against
 * content that is no longer current.
 *
 * With synchronization on, another machine's edit can land in the workspace
 * while a settings screen sits open. Saving would then send the whole form
 * back, replacing that edit with values read before it arrived -- and since
 * that is an ordinary local write, nothing records it as a conflict. The
 * backend refuses the save instead, and the screen reloads rather than retries:
 * resending the same input is exactly what must not happen.
 */
export function isStaleConfigSave(error: unknown): boolean {
  return error instanceof ApiRequestError && error.code === "config_changed";
}
