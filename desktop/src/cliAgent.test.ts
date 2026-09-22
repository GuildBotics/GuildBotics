import { describe, expect, it } from "vitest";

import {
  cliAgentNameFromConfig,
  cliToolStatusColor,
  cliToolStatusKey,
  usageRefetchMs,
} from "./cliAgent";
import type {
  CliAgentUsageResponse,
  IntelligenceConfig,
  EnvironmentToolStatus,
} from "./api/client";

describe("usageRefetchMs", () => {
  const usage = (overrides: Partial<CliAgentUsageResponse>): CliAgentUsageResponse => ({
    agent: "codex",
    usage: null,
    check: { status: "succeeded", checked_at: "2026-09-22T03:00:00Z", trace_id: "" },
    refreshing: false,
    ...overrides,
  });
  const failed = { status: "failed" as const, checked_at: "2026-09-22T03:00:00Z", trace_id: "" };

  it("polls quickly while a newer reading is on its way", () => {
    expect(usageRefetchMs(usage({ refreshing: true, check: failed }))).toBe(3_000);
  });

  it("retries a failed reading sooner than a successful one", () => {
    expect(usageRefetchMs(usage({ check: failed }))).toBe(30_000);
    expect(usageRefetchMs(usage({}))).toBe(300_000);
    expect(usageRefetchMs(undefined)).toBe(300_000);
  });
});

function configWith(mapping: Record<string, string>): IntelligenceConfig {
  return { cli_agent_mapping: mapping } as IntelligenceConfig;
}

describe("cliAgentNameFromConfig", () => {
  it("reads the tool from the definition path's directory", () => {
    const config = configWith({ default: "cli_agents/codex/default.yml" });
    expect(cliAgentNameFromConfig(config)).toBe("codex");
  });

  it("falls back to the first slot when no default slot exists", () => {
    const config = configWith({ reviewer: "cli_agents/claude/default.yml" });
    expect(cliAgentNameFromConfig(config)).toBe("claude");
  });

  it("rejects a three-part path outside the cli_agents root", () => {
    // A stale or corrupted mapping value must not be mislabeled as a tool.
    const config = configWith({ default: "models/openai/default.yml" });
    expect(cliAgentNameFromConfig(config)).toBeNull();
  });

  it("rejects a pre-restructure mapping value", () => {
    const config = configWith({ default: "codex" });
    expect(cliAgentNameFromConfig(config)).toBeNull();
  });

  it("returns null when the config has no mapping", () => {
    expect(cliAgentNameFromConfig(undefined)).toBeNull();
    expect(cliAgentNameFromConfig(configWith({}))).toBeNull();
  });
});

it.each([
  [false, false, false, "toolNotProvisioned", "gray"],
  [true, false, false, "toolCredentialsMissing", "warning"],
  [true, true, false, "toolCredentialsSaved", "gray"],
  [true, true, true, "toolAuthenticationFailed", "danger"],
])(
  "uses one presentation for tool state %s/%s/%s",
  (provisioned, credentials_saved, authentication_failed, key, color) => {
    const tool: EnvironmentToolStatus = {
      name: "codex",
      label: "Codex",
      config_reference: "cli_agents/codex/default.yml",
      provisioned: Boolean(provisioned),
      credentials_saved: Boolean(credentials_saved),
      authentication_failed: Boolean(authentication_failed),
      usage_supported: false,
      usage_check: null,
      login_command: "guildbotics environment login codex",
      problem: "",
    };
    expect(cliToolStatusKey(tool)).toBe(key);
    expect(cliToolStatusColor(tool)).toBe(color);
  },
);
