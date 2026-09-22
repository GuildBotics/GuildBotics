import { useQuery } from "@tanstack/react-query";

import {
  type CliAgentUsageResponse,
  type EnvironmentToolStatus,
  type IntelligenceConfig,
  getAgentEnvironmentStatus,
  getCliAgentUsage,
  getIntelligenceConfig,
} from "./api/client";

const CLI_PATH_PARTS = 3;
const CLI_AGENT_ROOT = "cli_agents";

// Resolve the catalog name of a member's AI CLI tool from an effective
// intelligence config (member override already falls back to the team default
// server-side).
export function cliAgentNameFromConfig(config: IntelligenceConfig | undefined): string | null {
  const mapping = config?.cli_agent_mapping ?? {};
  const file = mapping["default"] ?? Object.values(mapping)[0];
  if (!file) {
    return null;
  }
  // A definition path is `cli_agents/<tool>/<slot>.yml`, so the tool is the
  // directory -- the same rule the backend applies, including the root check,
  // so a path outside `cli_agents/` never reads as a tool name.
  const parts = file.split("/");
  return parts.length >= CLI_PATH_PARTS && parts[0] === CLI_AGENT_ROOT ? parts[1] : null;
}

// Resolve the human-friendly AI CLI tool label. Labels come from the backend
// AI CLI tool catalog, as the agent environment status lists it.
export function cliAgentLabelFromConfig(
  config: IntelligenceConfig | undefined,
  tools: Pick<EnvironmentToolStatus, "name" | "label">[],
): string | null {
  const value = cliAgentNameFromConfig(config);
  if (!value) {
    return null;
  }
  return tools.find((tool) => tool.name === value)?.label ?? value;
}

function useMemberIntelligenceConfig(personId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["intelligence-config", personId],
    queryFn: () => getIntelligenceConfig(personId),
    enabled,
  });
}

function useEnvironmentTools(): EnvironmentToolStatus[] {
  const environment = useQuery({
    queryKey: ["agent-environment-status"],
    queryFn: getAgentEnvironmentStatus,
  });
  return environment.data?.tools ?? [];
}

// Shared by the setup members list and the activity history member column so
// both resolve a member's AI CLI tool label the same way. Callers pass
// `enabled: false` for human members, whose config carries no AI CLI tool.
export function useMemberCliAgentLabel(personId: string, enabled: boolean): string | null {
  const config = useMemberIntelligenceConfig(personId, enabled);
  return cliAgentLabelFromConfig(config.data, useEnvironmentTools());
}

const USAGE_REFRESH_MS = 5 * 60 * 1000;
// The backend retries a failed probe after 30 seconds, as its cause is often
// passing (the host was asleep); asking at the same pace lets the reading
// recover without waiting out the full refresh period.
const USAGE_RETRY_MS = 30 * 1000;
// While the backend refreshes behind the reading it returned, ask again soon
// so the new reading replaces it once the probe completes.
const USAGE_REFRESHING_POLL_MS = 3 * 1000;

/** How long until the activity view asks for a tool's usage again. */
export function usageRefetchMs(usage: CliAgentUsageResponse | undefined): number {
  if (usage?.refreshing) return USAGE_REFRESHING_POLL_MS;
  return usage?.check?.status === "failed" ? USAGE_RETRY_MS : USAGE_REFRESH_MS;
}

// Account usage of the member's AI CLI tool, or null while the first reading
// loads and for tools without a structured usage interface. Usage belongs to
// the tool's account on this device, so members on the same tool share one
// query, and each tool is read on its own: a slow tool never delays another.
export function useMemberCliAgentUsage(
  personId: string,
  enabled: boolean,
): CliAgentUsageResponse | null {
  const config = useMemberIntelligenceConfig(personId, enabled);
  const agentName = cliAgentNameFromConfig(config.data);
  const supported = useEnvironmentTools().some(
    (tool) => tool.name === agentName && tool.usage_supported,
  );
  const usage = useQuery({
    queryKey: ["cli-agent-usage", agentName],
    queryFn: () => getCliAgentUsage(agentName ?? ""),
    enabled: enabled && supported,
    staleTime: USAGE_REFRESH_MS,
    refetchInterval: (query) => usageRefetchMs(query.state.data),
  });
  return usage.data ?? null;
}

/** Display the device's core facts without claiming credentials are valid. */
export function cliToolStatusKey(tool: EnvironmentToolStatus): string {
  if (!tool.provisioned) return "toolNotProvisioned";
  if (!tool.credentials_saved) return "toolCredentialsMissing";
  return tool.authentication_failed ? "toolAuthenticationFailed" : "toolCredentialsSaved";
}

/** Saved credentials are neutral; only observed problems need warning colors. */
export function cliToolStatusColor(tool: EnvironmentToolStatus): string {
  const colors: Record<string, string> = {
    toolNotProvisioned: "gray",
    toolCredentialsMissing: "warning",
    toolAuthenticationFailed: "danger",
    toolCredentialsSaved: "gray",
  };
  return colors[cliToolStatusKey(tool)];
}
