import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Code,
  CopyButton,
  Group,
  List,
  Modal,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  changeWorkspaceSyncHub,
  discardWorkspaceSyncRejection,
  enableWorkspaceSync,
  getWorkspaceDevices,
  getWorkspaceLive,
  getWorkspaceServiceOwner,
  getWorkspaceSyncStatus,
  previewWorkspaceSync,
  renameThisDevice,
  transferWorkspaceServiceOwner,
  type HubConnection,
  type RejectedChange,
  type WorkspaceLiveState,
  type WorkspaceSyncPreview,
} from "../api/client";
import { HubConnector } from "./HubConnector";
import { RequestErrorAlert } from "./RequestErrorAlert";

/**
 * Everything about sharing this workspace with the user's other machines:
 * which hub it uses, which devices joined, and what cannot be sent. What
 * belongs to the machine instead -- its SSH key, hosting the hub -- lives in
 * `DeviceSettings`.
 *
 * Connecting is deliberately a sequence rather than one button. The hub has to
 * be reachable before its workspaces can be listed, its host key has to be
 * confirmed by a person before anything is sent to it, and joining an existing
 * workspace has to be previewed before it happens.
 */
export function SyncSettings() {
  const { t } = useTranslation();
  const status = useQuery({
    queryKey: ["workspace-sync"],
    queryFn: getWorkspaceSyncStatus,
    refetchInterval: 5000,
  });
  return (
    <Stack gap="md">
      <Title order={3}>{t("sync.settings.title")}</Title>
      <Text c="dimmed" size="sm">
        {t("sync.settings.subtitle")}
      </Text>
      {status.data?.enabled ? (
        <ConnectedHubCard hubUrl={status.data.hub_url ?? ""} />
      ) : (
        <ConnectCard />
      )}
      <UnsendableChangesCard />
      <RejectedChangesCard />
      <DevicesCard />
    </Stack>
  );
}

/** How many of a rejection's files a row shows before the rest is asked for. */
const VISIBLE_REJECTED_PATHS = 3;

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
  const [error, setError] = useState<unknown>(null);

  const connect = useMutation({
    mutationFn: ({ endpoint, workspaceId }: { endpoint: string; workspaceId: string }) => {
      const body = { hub: { endpoint }, workspace_id: workspaceId };
      return change ? changeWorkspaceSyncHub(body) : enableWorkspaceSync(body);
    },
    onSuccess: async (next) => {
      setPreview(null);
      setError(null);
      queryClient.setQueryData(["workspace-sync"], next);
      await queryClient.invalidateQueries({ queryKey: ["workspace-devices"] });
    },
    onError: setError,
  });
  // Registering has nothing on the other side to compare against, so only
  // joining is previewed; asking anyway answers 409.
  const startConnect = useMutation({
    mutationFn: async ({ endpoint, workspaceId }: { endpoint: string; workspaceId: string }) =>
      workspaceId ? previewWorkspaceSync({ hub: { endpoint }, workspace_id: workspaceId }) : null,
    onSuccess: (found, variables) => {
      setError(null);
      if (found === null) {
        connect.mutate(variables);
        return;
      }
      setPending(variables);
      setPreview(found);
    },
    onError: setError,
  });

  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Title order={4}>{t(change ? "sync.connect.changeTitle" : "sync.connect.title")}</Title>
        {change ? null : (
          <Text c="dimmed" size="xs">
            {t("sync.connect.cloneHint")}
          </Text>
        )}
        <HubConnector onEndpointChange={() => setError(null)}>
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
        <RequestErrorAlert cause={error} title={t("sync.connect.failed")} />
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

/**
 * The commits the hub did not accept, still held on this machine.
 *
 * Nothing removes these on their own, so this list is what the user is left
 * with, and discarding is what ends it. The content is not shown, compared, or
 * exported here: it lives in one Git ref on this device, and reading it is a
 * manual procedure the README carries. Discarding is the one operation this
 * screen can offer without touching that content at all.
 */
function RejectedChangesCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [discarding, setDiscarding] = useState<RejectedChange | null>(null);
  const status = useQuery({
    queryKey: ["workspace-sync"],
    queryFn: getWorkspaceSyncStatus,
    refetchInterval: 5000,
  });
  const discard = useMutation({
    mutationFn: (rejectionId: string) => discardWorkspaceSyncRejection(rejectionId),
    onSuccess: (next) => {
      setDiscarding(null);
      queryClient.setQueryData(["workspace-sync"], next);
    },
  });
  const changes = status.data?.rejected_changes ?? [];
  if (changes.length === 0) {
    return null;
  }
  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Title order={4}>{t("sync.rejected.title")}</Title>
        <Text c="dimmed" size="sm">
          {t("sync.rejected.body")}
        </Text>
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("sync.rejected.when")}</Table.Th>
              <Table.Th>{t("sync.rejected.paths")}</Table.Th>
              <Table.Th>{t("sync.rejected.id")}</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {changes.map((change) => (
              <Table.Tr key={change.rejection_id}>
                <Table.Td style={{ whiteSpace: "nowrap" }}>
                  {new Date(change.occurred_at).toLocaleString()}
                </Table.Td>
                <Table.Td>
                  <RejectedPaths paths={change.paths} />
                </Table.Td>
                <Table.Td>
                  <Code style={{ overflowWrap: "anywhere" }}>{change.rejection_id}</Code>
                </Table.Td>
                <Table.Td>
                  <Button onClick={() => setDiscarding(change)} size="xs" variant="light">
                    {t("sync.rejected.discard")}
                  </Button>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Stack>
      <Modal
        centered
        onClose={() => setDiscarding(null)}
        opened={discarding !== null}
        title={t("sync.rejected.discardTitle")}
      >
        <Stack gap="sm">
          <Text size="sm">{t("sync.rejected.discardBody")}</Text>
          <Stack gap={2}>
            {(discarding?.paths ?? []).map((path) => (
              <Code key={path} style={{ overflowWrap: "anywhere" }}>
                {path}
              </Code>
            ))}
          </Stack>
          <Text c="dimmed" size="xs">
            {t("sync.rejected.discardHint")}
          </Text>
          <Group justify="flex-end">
            <Button onClick={() => setDiscarding(null)} variant="default">
              {t("sync.rejected.cancel")}
            </Button>
            <Button
              color="danger"
              loading={discard.isPending}
              onClick={() => discard.mutate(discarding?.rejection_id ?? "")}
            >
              {t("sync.rejected.discardConfirm")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Card>
  );
}

/**
 * The files one set-aside change holds back.
 *
 * A rejection can name many of them, and a row whose height follows the list
 * stops being a table the moment there are two rows. The count is what the
 * user reads first anyway; the rest opens on demand.
 */
function RejectedPaths({ paths }: { paths: string[] }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? paths : paths.slice(0, VISIBLE_REJECTED_PATHS);
  const hidden = paths.length - shown.length;
  return (
    <Stack gap={2}>
      {shown.map((path) => (
        <Code key={path} style={{ overflowWrap: "anywhere" }}>
          {path}
        </Code>
      ))}
      {hidden > 0 || expanded ? (
        <Anchor component="button" onClick={() => setExpanded(!expanded)} size="xs" type="button">
          {expanded ? t("sync.rejected.less") : t("sync.rejected.more", { count: hidden })}
        </Anchor>
      ) : null}
    </Stack>
  );
}

function DevicesCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState<string | null>(null);
  const devices = useQuery({ queryKey: ["workspace-devices"], queryFn: getWorkspaceDevices });
  const live = useQuery({
    queryKey: ["workspace-live"],
    queryFn: getWorkspaceLive,
    enabled: devices.data !== undefined && devices.data.devices.length > 0,
    refetchInterval: 1000,
  });
  const owner = useQuery({
    queryKey: ["workspace-service-owner"],
    queryFn: getWorkspaceServiceOwner,
    enabled: devices.data !== undefined && devices.data.devices.length > 0,
    refetchInterval: 5000,
  });
  const rename = useMutation({
    mutationFn: (displayName: string) => renameThisDevice(displayName),
    onSuccess: (next) => {
      queryClient.setQueryData(["workspace-devices"], next);
      setName(null);
    },
  });
  const transfer = useMutation({
    mutationFn: (deviceId: string) => transferWorkspaceServiceOwner(deviceId),
    onSuccess: (next) => {
      queryClient.setQueryData(["workspace-service-owner"], next);
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
          <Stack gap="xs">
            {rows.map((device) => (
              <Card key={device.device_id} p="sm" radius="sm" withBorder>
                <Stack gap="sm">
                  <Group align="flex-start" justify="space-between" wrap="wrap">
                    <Group gap="xs">
                      <Text fw={600}>{device.display_name}</Text>
                      {device.is_self ? <Badge size="xs">{t("sync.devices.self")}</Badge> : null}
                    </Group>
                    <LiveDeviceStatus
                      deviceId={device.device_id}
                      live={live.data ?? []}
                      status={device.live_status ?? "unknown"}
                    />
                  </Group>
                  <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="md">
                    <DeviceDetail label={t("sync.devices.os")}>{device.os}</DeviceDetail>
                    <DeviceDetail label={t("sync.devices.joinedAt")}>
                      {new Date(device.joined_at).toLocaleString()}
                    </DeviceDetail>
                    <DeviceDetail label={t("sync.devices.fingerprint")}>
                      <Code style={{ overflowWrap: "anywhere" }}>
                        {device.ssh_public_key_fingerprint || t("sync.devices.fingerprintMissing")}
                      </Code>
                    </DeviceDetail>
                    <DeviceDetail label={t("sync.devices.id")}>
                      <Group gap="xs" style={{ minWidth: 0 }} wrap="nowrap">
                        <Code style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>
                          {device.device_id}
                        </Code>
                        <CopyButton value={device.device_id}>
                          {({ copied, copy }) => (
                            <Button
                              onClick={copy}
                              size="compact-xs"
                              style={{ flexShrink: 0 }}
                              variant="subtle"
                            >
                              {t(copied ? "sync.devices.copied" : "sync.devices.copy")}
                            </Button>
                          )}
                        </CopyButton>
                      </Group>
                    </DeviceDetail>
                  </SimpleGrid>
                </Stack>
              </Card>
            ))}
          </Stack>
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
        <ServiceOwnerCard
          devices={rows}
          error={owner.error}
          owner={owner.data}
          onTransfer={(deviceId) => transfer.mutate(deviceId)}
          pending={transfer.isPending}
        />
      </Stack>
    </Card>
  );
}

function DeviceDetail({ children, label }: { children: ReactNode; label: string }) {
  return (
    <Stack gap={4} style={{ minWidth: 0 }}>
      <Text c="dimmed" fw={500} size="xs">
        {label}
      </Text>
      <Text component="div" size="sm">
        {children}
      </Text>
    </Stack>
  );
}

function LiveDeviceStatus({
  deviceId,
  live,
  status,
}: {
  deviceId: string;
  live: WorkspaceLiveState[];
  status: string;
}) {
  const { t } = useTranslation();
  const current = live
    .filter((state) => state.device_id === deviceId)
    .sort((left, right) => right.observed_at.localeCompare(left.observed_at))[0];
  const works = current?.works.slice(0, 2) ?? [];
  const workCount = current?.works.length ?? 0;
  const key = ["unknown", "online", "delayed", "expired"].includes(status) ? status : "unknown";
  return (
    <Stack gap={2}>
      <Badge color={key === "online" ? "green" : key === "delayed" ? "yellow" : "gray"}>
        {t(`sync.devices.liveStatus.${key}`)}
      </Badge>
      {workCount > 0 ? (
        <Stack gap={2}>
          <Text c="dimmed" size="xs">
            {t("sync.devices.currentWork", { count: workCount })}
          </Text>
          {works.map((work) => (
            <Text key={work.work_id} size="xs" style={{ overflowWrap: "anywhere" }}>
              {work.workflow_name}
            </Text>
          ))}
        </Stack>
      ) : null}
    </Stack>
  );
}

function ServiceOwnerCard({
  devices,
  error,
  owner,
  onTransfer,
  pending,
}: {
  devices: Array<{
    device_id: string;
    display_name: string;
    is_self: boolean;
    live_status?: string;
  }>;
  error: unknown;
  owner: { owner_device_id?: string | null; is_self: boolean } | undefined;
  onTransfer: (deviceId: string) => void;
  pending: boolean;
}) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);
  const self = devices.find((device) => device.is_self);
  const current = devices.find((device) => device.device_id === owner?.owner_device_id);
  const canTakeOver = Boolean(self && owner && !owner.is_self);
  const currentName = current?.display_name ?? owner?.owner_device_id ?? "—";
  const currentStatus = current?.live_status ?? "unknown";
  return (
    <Card withBorder radius="sm" p="sm">
      <Stack gap="xs">
        <Title order={5}>{t("sync.owner.title")}</Title>
        {error ? <RequestErrorAlert cause={error} title={t("sync.owner.failed")} /> : null}
        <Text size="sm">
          {owner?.owner_device_id
            ? t("sync.owner.current", {
                deviceId: owner.owner_device_id,
                name: currentName,
              })
            : t("sync.owner.none")}
        </Text>
        {owner?.owner_device_id ? (
          <Text c="dimmed" size="xs">
            {t(`sync.owner.connection.${currentStatus}`, { defaultValue: currentStatus })}
          </Text>
        ) : null}
        {owner?.is_self ? <Badge>{t("sync.owner.self")}</Badge> : null}
        <Text c="dimmed" size="xs">
          {t("sync.owner.hint")}
        </Text>
        {canTakeOver ? (
          <Button
            color="red"
            disabled={pending}
            loading={pending}
            onClick={() => setConfirming(true)}
            size="xs"
            variant="light"
          >
            {t("sync.owner.takeOver", { name: self?.display_name })}
          </Button>
        ) : null}
      </Stack>
      <Modal
        centered
        onClose={() => setConfirming(false)}
        opened={confirming}
        title={t("sync.owner.confirmTitle")}
      >
        <Stack gap="sm">
          <Text size="sm">
            {t("sync.owner.confirmBody", {
              current: currentName,
              status: t(`sync.owner.connection.${currentStatus}`, {
                defaultValue: currentStatus,
              }),
            })}
          </Text>
          <Alert color="red" title={t("sync.owner.confirmWarning")}>
            {t("sync.owner.confirmHint")}
          </Alert>
          <Group justify="flex-end">
            <Button onClick={() => setConfirming(false)} variant="default">
              {t("sync.owner.cancel")}
            </Button>
            <Button
              color="red"
              loading={pending}
              onClick={() => {
                if (self) {
                  onTransfer(self.device_id);
                  setConfirming(false);
                }
              }}
            >
              {t("sync.owner.confirmAction")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Card>
  );
}
