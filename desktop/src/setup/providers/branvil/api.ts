import { providerOperation } from "../../../api/client";

/** The owner's Git credential helper configuration for their own checkouts. */
export function getGitConfig(): Promise<{ git_config: string }> {
  return providerOperation("branvil", "git_config", {});
}
