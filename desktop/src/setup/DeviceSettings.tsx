import { Alert, Badge, Button, Card, Code, Group, Modal, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { getConfigStatus, getRegisteredWorkspaces, unregisterWorkspace } from "../api/client";
import { ServerMachineBadge } from "../serverMachine";

/**
 * Settings that belong to this machine rather than to a workspace, so nothing
 * here waits for one to be selected.
 */
export function DeviceSettings() {
  const { t } = useTranslation();
  return (
    <Stack gap="md">
      <Group gap="xs">
        <Title order={3}>{t("setup.device.title")}</Title>
        <ServerMachineBadge />
      </Group>
      <Text c="dimmed" size="sm">
        {t("setup.device.subtitle")}
      </Text>
      <RegisteredWorkspacesCard />
    </Stack>
  );
}

function RegisteredWorkspacesCard() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const [removing, setRemoving] = useState<string | null>(null);
  const locations = useQuery({
    queryKey: ["registered-workspaces"],
    queryFn: getRegisteredWorkspaces,
  });
  const remove = useMutation({
    mutationFn: unregisterWorkspace,
    onSuccess: (paths) => {
      client.setQueryData(["registered-workspaces"], paths);
      setRemoving(null);
      void client.invalidateQueries({ queryKey: ["agent-environment-status"] });
    },
  });
  const error = locations.error;
  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Title order={4}>{t("setup.device.registeredWorkspaces.title")}</Title>
        <Text size="sm" c="dimmed">
          {t("setup.device.registeredWorkspaces.body")}
        </Text>
        {error ? <Alert color="danger">{error.message}</Alert> : null}
        {locations.data?.length === 0 ? (
          <Text size="sm">{t("setup.device.registeredWorkspaces.empty")}</Text>
        ) : null}
        {locations.data?.map((path) => (
          <Group key={path} justify="space-between">
            <Code style={{ overflowWrap: "anywhere" }}>{path}</Code>
            {path === config.data?.workspace ? (
              <Badge>{t("setup.device.registeredWorkspaces.active")}</Badge>
            ) : null}
            <Button
              size="xs"
              variant="light"
              color="danger"
              loading={remove.isPending && remove.variables === path}
              disabled={
                remove.isPending ||
                config.isPending ||
                config.isError ||
                path === config.data?.workspace
              }
              onClick={() => {
                remove.reset();
                setRemoving(path);
              }}
              aria-label={t("setup.device.registeredWorkspaces.remove", { path })}
            >
              {t("setup.device.registeredWorkspaces.removeLabel")}
            </Button>
          </Group>
        ))}
        <Modal
          opened={removing !== null}
          onClose={() => !remove.isPending && setRemoving(null)}
          closeOnEscape={!remove.isPending}
          closeOnClickOutside={!remove.isPending}
          title={t("setup.device.registeredWorkspaces.removeLabel")}
        >
          <Stack>
            <Text>{t("setup.device.registeredWorkspaces.confirm", { path: removing })}</Text>
            {remove.error ? <Alert color="danger">{remove.error.message}</Alert> : null}
            <Group justify="flex-end">
              <Button
                variant="default"
                disabled={remove.isPending}
                onClick={() => setRemoving(null)}
              >
                {t("setup.device.registeredWorkspaces.cancel")}
              </Button>
              <Button
                color="danger"
                loading={remove.isPending}
                onClick={() => removing && remove.mutate(removing)}
              >
                {t("setup.device.registeredWorkspaces.confirmLabel")}
              </Button>
            </Group>
          </Stack>
        </Modal>
      </Stack>
    </Card>
  );
}
