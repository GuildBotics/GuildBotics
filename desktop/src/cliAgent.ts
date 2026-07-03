import { useQuery } from "@tanstack/react-query";

import {
  type CliAgentDetection,
  type IntelligenceConfig,
  getCliAgentDetections,
  getIntelligenceConfig,
} from "./api/client";

// Resolve the human-friendly CLI agent label from an effective intelligence
// config (member override already falls back to the team default server-side).
// Labels come from the backend CLI agent catalog (the detection endpoint).
export function cliAgentLabelFromConfig(
  config: IntelligenceConfig | undefined,
  detections: CliAgentDetection[],
): string | null {
  const mapping = config?.cli_agent_mapping ?? {};
  const file = mapping["default"] ?? Object.values(mapping)[0];
  if (!file) {
    return null;
  }
  const value = file.replace(/\.yml$/, "").replace(/-cli$/, "");
  return detections.find((agent) => agent.name === value)?.label ?? value;
}

// Shared by the setup members list and the activity history member column so
// both resolve a member's CLI agent label the same way. Callers pass
// `enabled: false` for human members, whose config carries no CLI agent.
export function useMemberCliAgentLabel(personId: string, enabled: boolean): string | null {
  const config = useQuery({
    queryKey: ["intelligence-config", personId],
    queryFn: () => getIntelligenceConfig(personId),
    enabled,
  });
  const detections = useQuery({
    queryKey: ["cli-agent-detections"],
    queryFn: getCliAgentDetections,
  });
  return cliAgentLabelFromConfig(config.data, detections.data?.agents ?? []);
}
