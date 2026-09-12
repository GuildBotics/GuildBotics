import {
  Badge,
  Button,
  Card,
  Code,
  CopyButton,
  Group,
  Loader,
  ScrollArea,
  Stack,
  Table,
  Text,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Hammer, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { cliToolStatusKey } from "../cliAgent";

import {
  buildAgentEnvironment,
  getAgentEnvironmentStatus,
  type AgentEnvironmentStatusResponse,
  type EnvironmentToolStatus,
  type SnapshotState,
} from "../api/client";

/** Anchor for a system alert to scroll to. */
export const AGENT_ENVIRONMENT_CARD_ID = "agent-environment";
/** Read local state so external login and completed turns update the card. */
const STATUS_REFRESH_MS = 10000;
const BUILDING_REFRESH_MS = 2000;
/** The snapshot states in which pressing "build" does something. */
const BUILDABLE: SnapshotState[] = ["missing", "stale", "failed"];
const STATE_COLORS: Record<SnapshotState, string> = {
  ready: "success",
  building: "info",
  stale: "warning",
  missing: "gray",
  failed: "danger",
};

/**
 * What this device holds of the isolated agent environment: whether it can
 * run one at all, the snapshot the declaration asks for, the resolvers, and
 * each tool's login. Everything here is the device's, so the card does not
 * share the intelligence settings' save button: the one action is a build,
 * and it runs on this device alone.
 */
export function AgentEnvironmentCard({
  focusElement,
}: {
  /** The element id a system alert asked to bring into view. */
  focusElement?: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ["agent-environment-status"],
    queryFn: getAgentEnvironmentStatus,
    refetchInterval: (query) =>
      query.state.data?.snapshot.state === "building" ? BUILDING_REFRESH_MS : STATUS_REFRESH_MS,
  });
  const build = useMutation({
    mutationFn: buildAgentEnvironment,
    onSuccess: (started) => {
      queryClient.setQueryData<AgentEnvironmentStatusResponse>(
        ["agent-environment-status"],
        started,
      );
      queryClient.invalidateQueries({ queryKey: ["system-alerts"] });
    },
  });
  const focused = Boolean(focusElement?.startsWith(AGENT_ENVIRONMENT_CARD_ID));
  const loaded = Boolean(status.data);
  useEffect(() => {
    if (focused && loaded && focusElement) {
      window.requestAnimationFrame(() => {
        document.getElementById(focusElement)?.scrollIntoView?.({ block: "start" });
      });
    }
  }, [focused, loaded, focusElement]);

  const data = status.data;
  const narrow = { whiteSpace: "nowrap" as const, width: 1, verticalAlign: "top" as const };
  return (
    <Card
      id={AGENT_ENVIRONMENT_CARD_ID}
      withBorder
      radius="sm"
      p="md"
      data-testid="agent-environment"
    >
      <Stack gap="sm">
        <div>
          <Text size="sm" fw={700}>
            {t("setup.intelligence.environment.title")}
          </Text>
          <Text size="sm" c="dimmed">
            {t("setup.intelligence.environment.description")}
          </Text>
        </div>
        <Button
          size="compact-xs"
          variant="subtle"
          leftSection={<RefreshCw size={12} />}
          onClick={() => {
            void status.refetch();
            void queryClient.invalidateQueries({ queryKey: ["system-alerts"] });
          }}
        >
          {t("setup.intelligence.environment.refresh")}
        </Button>
        {status.isError ? (
          <Text size="sm" c="danger">
            {t("setup.intelligence.environment.loadError")}
          </Text>
        ) : null}
        {data ? (
          <Table withTableBorder={false} verticalSpacing="xs">
            <Table.Tbody>
              <Table.Tr id={`${AGENT_ENVIRONMENT_CARD_ID}-runtime`}>
                <Table.Th style={narrow}>{t("setup.intelligence.environment.runtime")}</Table.Th>
                <Table.Td>
                  <Group gap="xs">
                    {data.runtime.available ? (
                      <Badge color="success" variant="light" size="sm">
                        {t("setup.intelligence.environment.runtimeAvailable", {
                          version: data.runtime.version,
                        })}
                      </Badge>
                    ) : (
                      <>
                        <Badge color="danger" variant="light" size="sm">
                          {t("setup.intelligence.environment.runtimeUnavailable")}
                        </Badge>
                        <Text size="sm">{data.runtime.reason}</Text>
                      </>
                    )}
                    {data.runtime.home ? (
                      <Text size="xs" c="dimmed" ff="monospace">
                        {data.runtime.home}
                      </Text>
                    ) : null}
                  </Group>
                </Table.Td>
              </Table.Tr>
              <Table.Tr id={`${AGENT_ENVIRONMENT_CARD_ID}-snapshot`}>
                <Table.Th style={narrow}>{t("setup.intelligence.environment.snapshot")}</Table.Th>
                <Table.Td>
                  <Stack gap="xs">
                    <Group gap="xs">
                      <Badge color={STATE_COLORS[data.snapshot.state]} variant="light" size="sm">
                        {t(`setup.intelligence.environment.snapshotStates.${data.snapshot.state}`)}
                      </Badge>
                      {data.snapshot.state === "building" ? <Loader size="xs" /> : null}
                      {data.snapshot.name ? (
                        <Text size="xs" c="dimmed" ff="monospace">
                          {data.snapshot.name}
                        </Text>
                      ) : null}
                      {data.runtime.available && BUILDABLE.includes(data.snapshot.state) ? (
                        <Button
                          size="xs"
                          variant="light"
                          leftSection={<Hammer size={14} />}
                          loading={build.isPending}
                          onClick={() => build.mutate()}
                        >
                          {t("setup.intelligence.environment.build")}
                        </Button>
                      ) : null}
                    </Group>
                    {data.snapshot.detail ? (
                      <Text size="sm" c={data.snapshot.state === "failed" ? "danger" : undefined}>
                        {data.snapshot.detail}
                      </Text>
                    ) : null}
                    {build.isError ? (
                      <Text size="sm" c="danger">
                        {build.error instanceof Error
                          ? build.error.message
                          : t("setup.intelligence.environment.buildError")}
                      </Text>
                    ) : null}
                    {data.snapshot.output.length > 0 &&
                    ["building", "failed"].includes(data.snapshot.state) ? (
                      <ScrollArea.Autosize mah={160} type="auto">
                        <Code block aria-label={t("setup.intelligence.environment.buildOutput")}>
                          {data.snapshot.output.join("\n")}
                        </Code>
                      </ScrollArea.Autosize>
                    ) : null}
                  </Stack>
                </Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Th style={narrow}>{t("setup.intelligence.environment.dns")}</Table.Th>
                <Table.Td>
                  <Text size="sm">
                    {data.dns.declared === "host"
                      ? t("setup.intelligence.environment.dnsHost")
                      : data.dns.declared}
                    {data.dns.nameservers.length > 0 ? (
                      <Text span size="sm" c="dimmed" ff="monospace">
                        {" → "}
                        {data.dns.nameservers.join(", ")}
                      </Text>
                    ) : null}
                  </Text>
                  {data.dns.problem ? (
                    <Text size="sm" c="danger">
                      {data.dns.problem}
                    </Text>
                  ) : null}
                </Table.Td>
              </Table.Tr>
              {data.tools.map((tool) => (
                <Table.Tr key={tool.name} id={`${AGENT_ENVIRONMENT_CARD_ID}-tool-${tool.name}`}>
                  <Table.Th style={narrow}>{tool.label}</Table.Th>
                  <Table.Td>
                    <ToolLogin tool={tool} />
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        ) : null}
      </Stack>
    </Card>
  );
}

/** One tool's login on this device, with the command that performs it. */
function ToolLogin({ tool }: { tool: EnvironmentToolStatus }) {
  const { t } = useTranslation();
  if (!tool.provisioned) {
    return (
      <Badge color="gray" variant="light" size="sm">
        {t("setup.intelligence.environment.toolNotProvisioned")}
      </Badge>
    );
  }
  const command = tool.login_command;
  return (
    <Stack gap={4}>
      <Badge color={tool.authentication_failed ? "danger" : "gray"} variant="light" size="sm">
        {t(`setup.intelligence.environment.${cliToolStatusKey(tool)}`)}
      </Badge>
      <Text size="xs" c="dimmed">
        {t("setup.intelligence.environment.loginHint")}
      </Text>
      <Group gap="xs">
        <Code>{command}</Code>
        <CopyButton value={command}>
          {({ copied, copy }) => (
            <Button
              size="compact-xs"
              variant="subtle"
              leftSection={copied ? <Check size={12} /> : <Copy size={12} />}
              onClick={copy}
            >
              {copied
                ? t("setup.intelligence.environment.copied")
                : t("setup.intelligence.environment.copy")}
            </Button>
          )}
        </CopyButton>
      </Group>
    </Stack>
  );
}
