import { providerOperation } from "../../../api/client";

/** A query key of what the embedded Branvil answers. */
export const branvilKey = (what: string) => ["branvil", what];

function operation<T>(name: string, body: object = {}): Promise<T> {
  return providerOperation("branvil", name, body);
}

export function listWorkspaces(): Promise<{ workspaces: string[] }> {
  return operation("workspaces");
}

export function createWorkspace(name: string): Promise<{ workspaces: string[] }> {
  return operation("workspaces/create", { name });
}

/** What the owner adds to their git configuration for their own checkouts to
 * authenticate to Branvil. */
export function getGitConfig(): Promise<{ git_config: string }> {
  return operation("git_config");
}
