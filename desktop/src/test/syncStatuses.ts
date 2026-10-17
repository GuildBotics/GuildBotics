import { vi } from "vitest";

import { getWorkspaceSyncStatus, type WorkspaceSyncStatus } from "../api/client";

/**
 * Answer each scope's synchronization status from a mocked client.
 *
 * The identities default to a healthy twin of the workspace -- connected
 * exactly when it is, with nothing waiting or failing -- so a case about the
 * workspace reads as it did before there were two scopes.
 */
export function mockSyncStatuses(
  workspace: WorkspaceSyncStatus,
  identities: WorkspaceSyncStatus = {
    ...workspace,
    state: workspace.enabled ? "idle" : workspace.state,
    ahead_count: 0,
    behind_count: 0,
    unsendable_changes: [],
    rejected_changes: [],
    last_success_at: null,
    last_error_code: null,
    last_error_detail: null,
  },
) {
  vi.mocked(getWorkspaceSyncStatus).mockImplementation(async (scope = "workspace") =>
    scope === "identities" ? identities : workspace,
  );
}
