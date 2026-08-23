import { describe, expect, it } from "vitest";

import type { WorkspaceSyncStatus } from "../api/client";
import { syncCanRetry, syncIndicatorState, syncNeedsAttention, syncTone } from "./syncState";

function status(overrides: Partial<WorkspaceSyncStatus> = {}): WorkspaceSyncStatus {
  return {
    enabled: true,
    workspace_id: "1f0a0000-0000-7000-8000-000000000001",
    device_id: "1f0a0000-0000-7000-8000-0000000000d1",
    hub_url: "user@hub:.guildbotics/hub/workspaces/w/repository.git",
    state: "idle",
    local_head: "abc",
    remote_head: "abc",
    ahead_count: 0,
    behind_count: 0,
    unsendable_changes: [],
    rejected_changes: [],
    last_success_at: "2026-07-01T00:00:00+00:00",
    last_error_code: null,
    ...overrides,
  };
}

describe("syncIndicatorState", () => {
  it("reports a workspace with no hub as disabled", () => {
    expect(syncIndicatorState(status({ enabled: false }))).toBe("disabled");
  });

  it("reports a workspace whose status has not loaded as disabled", () => {
    expect(syncIndicatorState(undefined)).toBe("disabled");
  });

  it("reports a settled workspace as synced", () => {
    expect(syncIndicatorState(status())).toBe("synced");
  });

  it("reports queued local changes as sending", () => {
    expect(syncIndicatorState(status({ ahead_count: 2 }))).toBe("sending");
    expect(syncIndicatorState(status({ state: "pushing" }))).toBe("sending");
  });

  it("reports hub content not yet taken as receiving", () => {
    expect(syncIndicatorState(status({ behind_count: 1 }))).toBe("receiving");
    expect(syncIndicatorState(status({ state: "fetching" }))).toBe("receiving");
  });

  it("reports an unreachable hub", () => {
    expect(syncIndicatorState(status({ state: "unreachable" }))).toBe("unreachable");
  });

  it("reports changes that cannot be sent", () => {
    expect(
      syncIndicatorState(
        status({ unsendable_changes: [{ path: "config/team/project.yml", reason: "too large" }] }),
      ),
    ).toBe("unsendable");
  });
});

describe("precedence when several states are true at once", () => {
  it("puts an unreachable hub ahead of changes waiting to be sent", () => {
    expect(syncIndicatorState(status({ state: "unreachable", ahead_count: 3 }))).toBe(
      "unreachable",
    );
  });

  it("puts changes that cannot be sent ahead of ones merely queued", () => {
    // Waiting clears the queue but never clears these, so saying "sending"
    // would promise something that will not happen.
    expect(
      syncIndicatorState(
        status({
          ahead_count: 5,
          unsendable_changes: [{ path: "config/team/project.yml", reason: "too large" }],
        }),
      ),
    ).toBe("unsendable");
  });

  it("puts a build too old to read the shared content ahead of everything", () => {
    expect(
      syncIndicatorState(
        status({
          state: "update_required",
          behind_count: 4,
          unsendable_changes: [{ path: "config/team/project.yml", reason: "too large" }],
        }),
      ),
    ).toBe("update_required");
  });

  it("puts damaged shared data ahead of an unreachable hub", () => {
    expect(syncIndicatorState(status({ state: "invalid_shared_state" }))).toBe(
      "invalid_shared_state",
    );
  });
});

describe("what the user is asked to do", () => {
  it("asks for attention only where waiting does not help", () => {
    expect(syncNeedsAttention("unreachable")).toBe(true);
    expect(syncNeedsAttention("unsendable")).toBe(true);
    expect(syncNeedsAttention("invalid_shared_state")).toBe(true);
    expect(syncNeedsAttention("update_required")).toBe(true);
    expect(syncNeedsAttention("sending")).toBe(false);
    expect(syncNeedsAttention("receiving")).toBe(false);
    expect(syncNeedsAttention("synced")).toBe(false);
    expect(syncNeedsAttention("disabled")).toBe(false);
  });

  it("offers a retry only where another attempt can succeed", () => {
    expect(syncCanRetry("unreachable")).toBe(true);
    expect(syncCanRetry("invalid_shared_state")).toBe(true);
    // Retrying sends the same rejected bytes, and updating is not a retry.
    expect(syncCanRetry("unsendable")).toBe(false);
    expect(syncCanRetry("update_required")).toBe(false);
    expect(syncCanRetry("synced")).toBe(false);
  });

  it("colours a settled workspace differently from one needing attention", () => {
    expect(syncTone("synced")).toBe("success");
    expect(syncTone("sending")).toBe("info");
    expect(syncTone("unreachable")).toBe("warning");
    expect(syncTone("invalid_shared_state")).toBe("danger");
    expect(syncTone("disabled")).toBe("neutral");
  });
});
