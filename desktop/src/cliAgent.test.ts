import { describe, expect, it } from "vitest";

import { cliAgentNameFromConfig } from "./cliAgent";
import type { IntelligenceConfig } from "./api/client";

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
