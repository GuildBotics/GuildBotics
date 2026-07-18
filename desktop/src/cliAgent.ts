import { useQuery } from "@tanstack/react-query";

import {
  type CliAgentDetection,
  type CliAgentUsage,
  type IntelligenceConfig,
  getCliAgentDetections,
  getCliAgentUsage,
  getIntelligenceConfig,
} from "./api/client";

// Resolve the catalog name of a member's AI CLI tool from an effective
// intelligence config (member override already falls back to the team default
// server-side).
export function cliAgentNameFromConfig(config: IntelligenceConfig | undefined): string | null {
  const mapping = config?.cli_agent_mapping ?? {};
  const file = mapping["default"] ?? Object.values(mapping)[0];
  if (!file) {
    return null;
  }
  return file.replace(/\.yml$/, "").replace(/-cli$/, "");
}

// Resolve the human-friendly AI CLI tool label. Labels come from the backend
// AI CLI tool catalog (the detection endpoint).
export function cliAgentLabelFromConfig(
  config: IntelligenceConfig | undefined,
  detections: CliAgentDetection[],
): string | null {
  const value = cliAgentNameFromConfig(config);
  if (!value) {
    return null;
  }
  return detections.find((agent) => agent.name === value)?.label ?? value;
}

function useMemberIntelligenceConfig(personId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["intelligence-config", personId],
    queryFn: () => getIntelligenceConfig(personId),
    enabled,
  });
}

// Shared by the setup members list and the activity history member column so
// both resolve a member's AI CLI tool label the same way. Callers pass
// `enabled: false` for human members, whose config carries no AI CLI tool.
export function useMemberCliAgentLabel(personId: string, enabled: boolean): string | null {
  const config = useMemberIntelligenceConfig(personId, enabled);
  const detections = useQuery({
    queryKey: ["cli-agent-detections"],
    queryFn: getCliAgentDetections,
  });
  return cliAgentLabelFromConfig(config.data, detections.data?.agents ?? []);
}

const USAGE_REFRESH_MS = 5 * 60 * 1000;

// Current account usage of the member's AI CLI tool, or null while loading and
// for tools without a structured usage interface (only Codex exposes one).
// The usage endpoint reports machine-wide usage per tool, so all members on
// the same tool share one query.
export function useMemberCliAgentUsage(personId: string, enabled: boolean): CliAgentUsage | null {
  const config = useMemberIntelligenceConfig(personId, enabled);
  const agentName = cliAgentNameFromConfig(config.data);
  const usage = useQuery({
    queryKey: ["cli-agent-usage"],
    queryFn: getCliAgentUsage,
    enabled: enabled && agentName !== null,
    staleTime: USAGE_REFRESH_MS,
    refetchInterval: USAGE_REFRESH_MS,
  });
  return usage.data?.usages.find((item) => item.agent === agentName) ?? null;
}
