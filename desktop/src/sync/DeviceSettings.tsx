import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  CopyButton,
  Group,
  Modal,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Server } from "lucide-react";

import {
  createDeviceSshKey,
  createHub,
  getDeviceSshKey,
  getHubStatus,
  getRegisteredWorkspaces,
  getConfigStatus,
  unregisterWorkspace,
} from "../api/client";

/**
 * Settings that belong to this machine rather than to a workspace: its SSH
 * key and whether it hosts the hub. The machine that hosts the hub is often
 * the one that has no workspace at all, so nothing here waits for one.
 */
export function DeviceSettings() {
  const { t } = useTranslation();
  return (
    <Stack gap="md">
      <Title order={3}>{t("sync.device.title")}</Title>
      <Text c="dimmed" size="sm">
        {t("sync.device.subtitle")}
      </Text>
      <SshKeyCard />
      <HostThisMachineCard />
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
        <Title order={4}>{t("sync.registeredWorkspaces.title")}</Title>
        <Text size="sm" c="dimmed">
          {t("sync.registeredWorkspaces.body")}
        </Text>
        {error ? <Alert color="danger">{error.message}</Alert> : null}
        {locations.data?.length === 0 ? (
          <Text size="sm">{t("sync.registeredWorkspaces.empty")}</Text>
        ) : null}
        {locations.data?.map((path) => (
          <Group key={path} justify="space-between">
            <Code style={{ overflowWrap: "anywhere" }}>{path}</Code>
            {path === config.data?.workspace ? (
              <Badge>{t("sync.registeredWorkspaces.active")}</Badge>
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
              aria-label={t("sync.registeredWorkspaces.remove", { path })}
            >
              {t("sync.registeredWorkspaces.removeLabel")}
            </Button>
          </Group>
        ))}
        <Modal
          opened={removing !== null}
          onClose={() => setRemoving(null)}
          title={t("sync.registeredWorkspaces.removeLabel")}
        >
          <Stack>
            <Text>{t("sync.registeredWorkspaces.confirm", { path: removing })}</Text>
            {remove.error ? <Alert color="danger">{remove.error.message}</Alert> : null}
            <Group justify="flex-end">
              <Button
                variant="default"
                disabled={remove.isPending}
                onClick={() => setRemoving(null)}
              >
                {t("sync.registeredWorkspaces.cancel")}
              </Button>
              <Button
                color="danger"
                loading={remove.isPending}
                onClick={() => removing && remove.mutate(removing)}
              >
                {t("sync.registeredWorkspaces.confirmLabel")}
              </Button>
            </Group>
          </Stack>
        </Modal>
      </Stack>
    </Card>
  );
}

/**
 * This device's public key, to be registered on the hub machine.
 *
 * GuildBotics cannot install it there: adding a key to a machine's
 * `authorized_keys` is exactly the step that proves the person doing it already
 * has access. So the key is shown to copy, and the hub side stays manual.
 */
function SshKeyCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const key = useQuery({ queryKey: ["device-ssh-key"], queryFn: getDeviceSshKey });
  const create = useMutation({
    mutationFn: createDeviceSshKey,
    onSuccess: (next) => queryClient.setQueryData(["device-ssh-key"], next),
  });
  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Title order={4}>{t("sync.sshKey.title")}</Title>
        <Text c="dimmed" size="sm">
          {t("sync.sshKey.body")}
        </Text>
        {key.data?.exists ? (
          <Stack gap="xs">
            <Code block style={{ overflowWrap: "anywhere" }}>
              {key.data.public_key}
            </Code>
            <Group gap="xs">
              <CopyButton value={key.data.public_key}>
                {({ copied, copy }) => (
                  <Button onClick={copy} size="xs" variant="light">
                    {t(copied ? "sync.sshKey.copied" : "sync.sshKey.copy")}
                  </Button>
                )}
              </CopyButton>
              <Text c="dimmed" size="xs">
                {key.data.fingerprint}
              </Text>
            </Group>
          </Stack>
        ) : (
          <Group>
            <Button loading={create.isPending} onClick={() => create.mutate()} size="xs">
              {t("sync.sshKey.create")}
            </Button>
          </Group>
        )}
      </Stack>
    </Card>
  );
}

/** Make this machine the one the others connect to. */
function HostThisMachineCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const hub = useQuery({ queryKey: ["hub"], queryFn: getHubStatus });
  const create = useMutation({
    mutationFn: createHub,
    onSuccess: (next) => queryClient.setQueryData(["hub"], next),
  });
  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Group gap="xs">
          <Server size={18} />
          <Title order={4}>{t("sync.host.title")}</Title>
        </Group>
        <Text c="dimmed" size="sm">
          {t("sync.host.body")}
        </Text>
        {hub.data?.hosted ? (
          <Stack gap="xs">
            <Text size="sm">{t("sync.host.hosted", { count: hub.data.workspace_ids.length })}</Text>
            <Code style={{ overflowWrap: "anywhere" }}>{String(hub.data.hub_root)}</Code>
            <Text c="dimmed" size="xs">
              {t("sync.host.sshdHint")}
            </Text>
          </Stack>
        ) : (
          <Group>
            <Button loading={create.isPending} onClick={() => create.mutate()} size="xs">
              {t("sync.host.create")}
            </Button>
          </Group>
        )}
      </Stack>
    </Card>
  );
}
