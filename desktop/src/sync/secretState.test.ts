import { describe, expect, it } from "vitest";

import type { WorkspaceSecrets } from "../api/client";
import { secretAlert, secretNeedsAttention, secretTone } from "./secretState";

function secrets(overrides: Partial<WorkspaceSecrets> = {}): WorkspaceSecrets {
  return {
    enabled: true,
    hub_reachable: true,
    hub_error_code: "",
    secret_store: { available: true, locked: false },
    hub_secret_store: { available: true, locked: false },
    keys: [],
    sendable_keys: [],
    fetchable_keys: [],
    missing_count: 0,
    outdated_count: 0,
    pending_count: 0,
    attention_count: 0,
    ...overrides,
  };
}

describe("secretTone", () => {
  it("keeps the two states that mean a value may be wrong apart from the rest", () => {
    expect(secretTone("ready")).toBe("ok");
    expect(secretTone("missing")).toBe("warning");
    expect(secretTone("outdated")).toBe("warning");
    expect(secretTone("pending_send")).toBe("warning");
    expect(secretTone("conflict")).toBe("danger");
    expect(secretTone("unconfirmed")).toBe("danger");
  });

  it("treats everything but being in step as worth showing", () => {
    expect(secretNeedsAttention("ready")).toBe(false);
    expect(secretNeedsAttention("pending_send")).toBe(true);
  });
});

describe("secretAlert", () => {
  it("says nothing about a workspace that has no hub", () => {
    expect(secretAlert(undefined)).toBeNull();
    expect(secretAlert(secrets({ enabled: false, attention_count: 3 }))).toBeNull();
  });

  it("names a locked store first, because every transfer fails while it lasts", () => {
    expect(
      secretAlert(
        secrets({
          secret_store: { available: false, locked: true },
          hub_reachable: false,
          attention_count: 2,
        }),
      ),
    ).toBe("local_locked");
    expect(secretAlert(secrets({ hub_secret_store: { available: false, locked: true } }))).toBe(
      "hub_locked",
    );
  });

  it("names an unreachable hub before the counts it could not check", () => {
    expect(
      secretAlert(secrets({ hub_reachable: false, hub_secret_store: null, attention_count: 1 })),
    ).toBe("hub_unreachable");
  });

  it("says nothing when every key is in step", () => {
    expect(secretAlert(secrets())).toBeNull();
    expect(secretAlert(secrets({ attention_count: 1 }))).toBe("attention");
  });
});
