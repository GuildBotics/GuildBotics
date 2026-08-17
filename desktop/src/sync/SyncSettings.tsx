import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  CopyButton,
  Group,
  List,
  Modal,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { CircleAlert, Server } from "lucide-react";

import {
  ApiRequestError,
  changeWorkspaceSyncHub,
  createDeviceSshKey,
  createHub,
  enableWorkspaceSync,
  getConfigStatus,
  getDeviceSshKey,
  getHubStatus,
  getWorkspaceDevices,
  getWorkspaceSyncStatus,
  previewWorkspaceSync,
  renameThisDevice,
  type HubConnection,
  type WorkspaceSyncPreview,
} from "../api/client";
import { HubConnector } from "./HubConnector";

/**
 * Everything about sharing this workspace with the user's other machines:
 * which hub it uses, which devices joined, what cannot be sent, and this
 * device's SSH key.
 *
 * Connecting is deliberately a sequence rather than one button. The hub has to
 * be reachable before its workspaces can be listed, its host key has to be
 * confirmed by a person before anything is sent to it, and joining an existing
 * workspace has to be previewed before it happens.
 */
export function SyncSettings() {
  const { t } = useTranslation();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  // Hosting a hub and this device's key are properties of the machine, not of
  // a workspace, and the machine that hosts the hub is often the one that has
  // no workspace at all. Only the sharing itself waits for one.
  const hasWorkspace = Boolean(config.data?.workspace);
  return (
    <Stack gap="md">
      <Title order={3}>{t("sync.settings.title")}</Title>
      <Text c="dimmed" size="sm">
        {t("sync.settings.subtitle")}
      </Text>
      {hasWorkspace ? (
        <WorkspaceSharing />
      ) : (
        <Alert color="neutral" title={t("sync.settings.noWorkspaceTitle")}>
          {t("sync.settings.noWorkspaceBody")}
        </Alert>
      )}
      <SshKeyCard />
      <HostThisMachineCard />
    </Stack>
  );
}

/** The hub this workspace uses, what it cannot send, and who else holds it. */
function WorkspaceSharing() {
  const status = useQuery({
    queryKey: ["workspace-sync"],
    queryFn: getWorkspaceSyncStatus,
    refetchInterval: 5000,
  });
  return (
    <>
      {status.data?.enabled ? (
        <ConnectedHubCard hubUrl={status.data.hub_url ?? ""} />
      ) : (
        <ConnectCard />
      )}
      <UnsendableChangesCard />
      <DevicesCard />
    </>
  );
}

// -- Connecting ---------------------------------------------------------------

/**
 * The steps between "no hub" and "sharing": reach the machine, confirm its host
 * key, pick a workspace, and -- when joining one that already exists -- see
 * what taking it would change before it happens.
 */
function ConnectCard({ change = false }: { change?: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [preview, setPreview] = useState<WorkspaceSyncPreview | null>(null);
  const [pending, setPending] = useState({ endpoint: "", workspaceId: "" });
  const [error, setError] = useState("");

  const report = (cause: unknown) => {
    setError(cause instanceof ApiRequestError ? cause.message : String(cause));
  };
  const connect = useMutation({
    mutationFn: ({ endpoint, workspaceId }: { endpoint: string; workspaceId: string }) => {
      const body = { hub: { endpoint }, workspace_id: workspaceId };
      return change ? changeWorkspaceSyncHub(body) : enableWorkspaceSync(body);
    },
    onSuccess: async (next) => {
      setPreview(null);
      setError("");
      queryClient.setQueryData(["workspace-sync"], next);
      await queryClient.invalidateQueries({ queryKey: ["workspace-devices"] });
    },
    onError: report,
  });
  // Registering has nothing on the other side to compare against, so only
  // joining is previewed; asking anyway answers 409.
  const startConnect = useMutation({
    mutationFn: async ({ endpoint, workspaceId }: { endpoint: string; workspaceId: string }) =>
      workspaceId ? previewWorkspaceSync({ hub: { endpoint }, workspace_id: workspaceId }) : null,
    onSuccess: (found, variables) => {
      setError("");
      if (found === null) {
        connect.mutate(variables);
        return;
      }
      setPending(variables);
      setPreview(found);
    },
    onError: report,
  });

  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Title order={4}>{t(change ? "sync.connect.changeTitle" : "sync.connect.title")}</Title>
        <HubConnector onEndpointChange={() => setError("")}>
          {(connection) => (
            <WorkspaceChoice
              connection={connection}
              onChoose={(workspaceId) =>
                startConnect.mutate({ endpoint: connection.endpoint, workspaceId })
              }
              pending={startConnect.isPending || connect.isPending}
            />
          )}
        </HubConnector>
        {error ? (
          <Alert color="danger" icon={<CircleAlert size={18} />} title={t("sync.connect.failed")}>
            {error}
          </Alert>
        ) : null}
        <JoinPreviewModal
          onCancel={() => setPreview(null)}
          onConfirm={() => {
            setPreview(null);
            connect.mutate(pending);
          }}
          pending={connect.isPending}
          preview={preview}
        />
      </Stack>
    </Card>
  );
}

/** Register this workspace on the hub, or join one the hub already holds. */
function WorkspaceChoice({
  connection,
  onChoose,
  pending,
}: {
  connection: HubConnection;
  onChoose: (workspaceId: string) => void;
  pending: boolean;
}) {
  const { t } = useTranslation();
  return (
    <Stack gap="xs">
      <Text fw={600} size="sm">
        {t("sync.connect.chooseTitle")}
      </Text>
      <Button disabled={pending} onClick={() => onChoose("")} variant="light">
        {t("sync.connect.register")}
      </Button>
      {connection.workspace_ids.length > 0 ? (
        <Stack gap="xs">
          <Text c="dimmed" size="xs">
            {t("sync.connect.joinHint")}
          </Text>
          {connection.workspace_ids.map((workspaceId) => (
            <Group gap="sm" key={workspaceId} wrap="nowrap">
              <Code style={{ overflowWrap: "anywhere" }}>{workspaceId}</Code>
              <Button
                disabled={pending}
                onClick={() => onChoose(workspaceId)}
                size="xs"
                variant="light"
              >
                {t("sync.connect.join")}
              </Button>
            </Group>
          ))}
        </Stack>
      ) : null}
    </Stack>
  );
}

/**
 * What joining would do, shown before it happens.
 *
 * Joining is not an overwrite: this machine's content is committed first, the
 * hub's version of any file both hold wins, and the commit that lost is kept
 * under a rejected ref. Saying so here is the difference between a person
 * expecting a merge and a person expecting a replacement.
 */
function JoinPreviewModal({
  preview,
  onCancel,
  onConfirm,
  pending,
}: {
  preview: WorkspaceSyncPreview | null;
  onCancel: () => void;
  onConfirm: () => void;
  pending: boolean;
}) {
  const { t } = useTranslation();
  return (
    <Modal centered onClose={onCancel} opened={preview !== null} title={t("sync.preview.title")}>
      {preview ? (
        <Stack gap="md">
          <Text size="sm">{t(`sync.preview.mode.${preview.mode}`)}</Text>
          <PathList paths={preview.differing} titleKey="sync.preview.differing" />
          <PathList paths={preview.hub_only} titleKey="sync.preview.hubOnly" />
          <PathList paths={preview.device_only} titleKey="sync.preview.deviceOnly" />
          {preview.unsendable_changes.length > 0 ? (
            <Alert color="warning" title={t("sync.preview.unsendableTitle")}>
              {t("sync.preview.unsendableBody", { count: preview.unsendable_changes.length })}
            </Alert>
          ) : null}
          <Group justify="flex-end">
            <Button onClick={onCancel} variant="default">
              {t("sync.preview.cancel")}
            </Button>
            <Button loading={pending} onClick={onConfirm}>
              {t("sync.preview.confirm")}
            </Button>
          </Group>
        </Stack>
      ) : null}
    </Modal>
  );
}

function PathList({ paths, titleKey }: { paths: string[]; titleKey: string }) {
  const { t } = useTranslation();
  if (paths.length === 0) {
    return null;
  }
  return (
    <Stack gap={4}>
      <Text fw={600} size="sm">
        {t(titleKey, { count: paths.length })}
      </Text>
      <List size="xs" withPadding>
        {paths.map((path) => (
          <List.Item key={path}>
            <Code style={{ overflowWrap: "anywhere" }}>{path}</Code>
          </List.Item>
        ))}
      </List>
    </Stack>
  );
}

// -- Once connected -----------------------------------------------------------

function ConnectedHubCard({ hubUrl }: { hubUrl: string }) {
  const { t } = useTranslation();
  const [changing, setChanging] = useState(false);
  return (
    <Stack gap="md">
      <Card withBorder radius="md" p="md">
        <Stack gap="xs">
          <Title order={4}>{t("sync.connected.title")}</Title>
          <Code style={{ overflowWrap: "anywhere" }}>{hubUrl}</Code>
          <Group>
            <Button onClick={() => setChanging((current) => !current)} size="xs" variant="light">
              {t(changing ? "sync.connected.cancelChange" : "sync.connected.change")}
            </Button>
          </Group>
          <Text c="dimmed" size="xs">
            {t("sync.connected.changeHint")}
          </Text>
        </Stack>
      </Card>
      {changing ? <ConnectCard change /> : null}
    </Stack>
  );
}

/**
 * Files held back from the hub because they would not survive the crossing.
 *
 * These are the user's own edits, not a conflict someone else won: nothing is
 * discarded, and nothing moves until the file itself is fixed here.
 */
function UnsendableChangesCard() {
  const { t } = useTranslation();
  const status = useQuery({
    queryKey: ["workspace-sync"],
    queryFn: getWorkspaceSyncStatus,
    refetchInterval: 5000,
  });
  const changes = status.data?.unsendable_changes ?? [];
  if (changes.length === 0) {
    return null;
  }
  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Title order={4}>{t("sync.unsendable.title")}</Title>
        <Text c="dimmed" size="sm">
          {t("sync.unsendable.body")}
        </Text>
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("sync.unsendable.path")}</Table.Th>
              <Table.Th>{t("sync.unsendable.reason")}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {changes.map((change) => (
              <Table.Tr key={change.path}>
                <Table.Td>
                  <Code style={{ overflowWrap: "anywhere" }}>{change.path}</Code>
                </Table.Td>
                <Table.Td>{change.reason}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Stack>
    </Card>
  );
}

function DevicesCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState<string | null>(null);
  const devices = useQuery({ queryKey: ["workspace-devices"], queryFn: getWorkspaceDevices });
  const rename = useMutation({
    mutationFn: (displayName: string) => renameThisDevice(displayName),
    onSuccess: (next) => {
      queryClient.setQueryData(["workspace-devices"], next);
      setName(null);
    },
  });
  const rows = devices.data?.devices ?? [];
  const self = rows.find((device) => device.is_self);
  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Title order={4}>{t("sync.devices.title")}</Title>
        {rows.length === 0 ? (
          <Text c="dimmed" size="sm">
            {t("sync.devices.empty")}
          </Text>
        ) : (
          <Table striped>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("sync.devices.name")}</Table.Th>
                <Table.Th>{t("sync.devices.os")}</Table.Th>
                <Table.Th>{t("sync.devices.joinedAt")}</Table.Th>
                <Table.Th>{t("sync.devices.id")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.map((device) => (
                <Table.Tr key={device.device_id}>
                  <Table.Td>
                    <Group gap="xs">
                      {device.display_name}
                      {device.is_self ? <Badge size="xs">{t("sync.devices.self")}</Badge> : null}
                    </Group>
                  </Table.Td>
                  <Table.Td>{device.os}</Table.Td>
                  <Table.Td>{new Date(device.joined_at).toLocaleString()}</Table.Td>
                  <Table.Td>
                    <Group gap="xs" wrap="nowrap">
                      <Code style={{ overflowWrap: "anywhere" }}>{device.device_id}</Code>
                      <CopyButton value={device.device_id}>
                        {({ copied, copy }) => (
                          <Button onClick={copy} size="compact-xs" variant="subtle">
                            {t(copied ? "sync.devices.copied" : "sync.devices.copy")}
                          </Button>
                        )}
                      </CopyButton>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
        {self ? (
          <Group align="flex-end" gap="sm">
            <TextInput
              description={t("sync.devices.renameHint")}
              label={t("sync.devices.rename")}
              onChange={(event) => setName(event.currentTarget.value)}
              style={{ flex: 1 }}
              value={name ?? self.display_name}
            />
            <Button
              disabled={name === null || name.trim() === ""}
              loading={rename.isPending}
              onClick={() => rename.mutate(name ?? "")}
            >
              {t("sync.devices.renameAction")}
            </Button>
          </Group>
        ) : null}
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
