import { Badge, Button, Code, Group, Modal, Stack, Text } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { cloneWorkspaceFromHub, getWorkspaceSyncStatus, type ConfigStatus } from "../api/client";
import { HubConnector } from "./HubConnector";
import { RequestErrorAlert } from "./RequestErrorAlert";

/**
 * Create a workspace on this machine from one a hub already holds.
 *
 * This is how a machine joins that has nothing yet, as opposed to one that
 * already has a workspace and connects it. The copy lands in a directory the
 * caller supplies, and the backend switches to it, so the caller is handed the
 * new configuration state rather than being expected to reload on its own.
 */
export function CloneFromHubButton({
  destination,
  onCloned,
}: {
  /** Where the copy is created; empty disables the action. */
  destination: string;
  onCloned: (status: ConfigStatus) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [opened, setOpened] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Taking a copy of the workspace already open here would leave this machine
  // with two copies of it, so the row that would do that is labeled.
  const sync = useQuery({
    queryKey: ["workspace-sync"],
    queryFn: getWorkspaceSyncStatus,
    enabled: opened,
  });
  const currentWorkspaceId = sync.data?.workspace_id ?? null;

  const clone = useMutation({
    mutationFn: ({ endpoint, workspaceId }: { endpoint: string; workspaceId: string }) =>
      cloneWorkspaceFromHub({
        hub: { endpoint },
        workspace_id: workspaceId,
        workspace_dir: destination.trim(),
      }),
    onSuccess: async (status) => {
      setOpened(false);
      setError(null);
      await queryClient.invalidateQueries();
      onCloned(status);
    },
    onError: setError,
  });

  return (
    <>
      <Button
        disabled={destination.trim() === ""}
        onClick={() => {
          setError(null);
          setOpened(true);
        }}
        size="xs"
        variant="light"
      >
        {t("sync.clone.action")}
      </Button>
      <Modal
        centered
        onClose={() => setOpened(false)}
        opened={opened}
        title={t("sync.clone.title")}
      >
        <Stack gap="sm">
          <Text size="sm">{t("sync.clone.body")}</Text>
          <Group gap="xs">
            <Text size="sm">{t("sync.clone.destination")}</Text>
            <Code style={{ overflowWrap: "anywhere" }}>{destination}</Code>
          </Group>
          <HubConnector onEndpointChange={() => setError(null)}>
            {(connection) =>
              connection.workspace_ids.length === 0 ? (
                <Text c="dimmed" size="sm">
                  {t("sync.clone.empty")}
                </Text>
              ) : (
                <Stack gap="xs">
                  <Text fw={600} size="sm">
                    {t("sync.clone.chooseTitle")}
                  </Text>
                  {connection.workspace_ids.map((workspaceId) => (
                    <Group gap="sm" key={workspaceId} wrap="nowrap">
                      <Code style={{ overflowWrap: "anywhere" }}>{workspaceId}</Code>
                      {workspaceId === currentWorkspaceId ? (
                        <Badge size="xs">{t("sync.clone.current")}</Badge>
                      ) : null}
                      <Button
                        disabled={clone.isPending}
                        onClick={() => clone.mutate({ endpoint: connection.endpoint, workspaceId })}
                        size="xs"
                        variant="light"
                      >
                        {t("sync.clone.take")}
                      </Button>
                    </Group>
                  ))}
                </Stack>
              )
            }
          </HubConnector>
          <RequestErrorAlert cause={error} title={t("sync.clone.failed")} />
        </Stack>
      </Modal>
    </>
  );
}
