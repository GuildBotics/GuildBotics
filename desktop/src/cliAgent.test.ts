import { describe, expect, it } from "vitest";

import { cliAgentNameFromConfig, cliToolStatusColor, cliToolStatusKey } from "./cliAgent";
import type { IntelligenceConfig, EnvironmentToolStatus } from "./api/client";

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
      login_command: "guildbotics environment login codex",
      problem: "",
    };
    expect(cliToolStatusKey(tool)).toBe(key);
    expect(cliToolStatusColor(tool)).toBe(color);
  },
);
