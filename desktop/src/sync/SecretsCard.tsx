import { Alert, Badge, Button, Card, Code, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { TriangleAlert } from "lucide-react";

import {
  fetchWorkspaceSecrets,
  getWorkspaceSecrets,
  sendWorkspaceSecrets,
  type SecretTransferResult,
  type WorkspaceSecretState,
  type WorkspaceSecrets,
} from "../api/client";
import { SECRETS_QUERY_KEY, SECRETS_REFETCH_MS, secretAlert, secretTone } from "./secretState";

/** The statuses that mean the value really moved; everything else is shown. */
const MOVED = new Set(["sent", "fetched"]);

/**
 * Which credentials this machine holds, and the two transfers that change that.
 *
 * Every transfer on this screen is one the user asked for. Nothing is fetched
 * in the background: a value arriving on a machine is a decision, so the list
 * says what is missing and waits. The values themselves never appear here --
 * they go from one OS secret store to the other, and what this screen handles
 * is a key name and a generation number.
 */
export function SecretsCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const secrets = useQuery({
    queryKey: SECRETS_QUERY_KEY,
    queryFn: getWorkspaceSecrets,
    refetchInterval: SECRETS_REFETCH_MS,
  });
  // A transfer answers per key, and the ones it could not move are the part
  // worth seeing: the refreshed states say what each key is now, but not that
  // the hub refused this particular attempt and why.
  const [refused, setRefused] = useState<SecretTransferResult[]>([]);
  const applied = (response: { secrets: WorkspaceSecrets; results: SecretTransferResult[] }) => {
    queryClient.setQueryData(SECRETS_QUERY_KEY, response.secrets);
    setRefused(response.results.filter((result) => !MOVED.has(result.status)));
  };
  // A bulk action names no keys. The lists below are as old as the last poll,
  // and acting on them would let a key that has since been changed on another
  // machine be swept up -- which is the one decision these actions must not
  // make on their own. Sending nothing asks the backend to work out what to
  // move from the state it reads now. A row names its key, because that is a
  // decision the user made about that key.
  const fetching = useMutation({
    mutationFn: (keys: string[]) => fetchWorkspaceSecrets({ keys }),
    onSuccess: applied,
  });
  const sending = useMutation({
    mutationFn: (keys: string[]) => sendWorkspaceSecrets({ keys }),
    onSuccess: applied,
  });
  const data = secrets.data;
  if (!data?.enabled) {
    return null;
  }
  const alert = secretAlert(data);
  // Only how many the buttons say, and whether there is anything to press.
  const fetchable = data.fetchable_keys;
  const sendable = data.sendable_keys;
  const busy = fetching.isPending || sending.isPending;
  return (
    <Card withBorder radius="md" p="md">
      <Stack gap="sm">
        <Title order={4}>{t("sync.secrets.title")}</Title>
        <Text c="dimmed" size="sm">
          {t("sync.secrets.body")}
        </Text>
        {alert && alert !== "attention" ? (
          <Alert
            color="warning"
            icon={<TriangleAlert size={18} />}
            title={t(`sync.secrets.alert.${alert}.title`)}
          >
            {t(`sync.secrets.alert.${alert}.body`)}
          </Alert>
        ) : null}
        {refused.length > 0 ? (
          <Alert
            color="warning"
            icon={<TriangleAlert size={18} />}
            title={t("sync.secrets.refused.title", { count: refused.length })}
          >
            <Stack gap={2}>
              {refused.map((result) => (
                <Text key={result.key} size="sm">
                  {t("sync.secrets.refused.entry", {
                    key: result.key,
                    reason: t(`sync.secrets.result.${result.status}`, {
                      defaultValue: result.status,
                    }),
                  })}
                </Text>
              ))}
            </Stack>
          </Alert>
        ) : null}
        <Group gap="xs">
          <Button
            disabled={fetchable.length === 0 || busy}
            loading={fetching.isPending}
            onClick={() => fetching.mutate([])}
            size="xs"
          >
            {t("sync.secrets.fetchAll", { count: fetchable.length })}
          </Button>
          <Button
            disabled={sendable.length === 0 || busy}
            loading={sending.isPending}
            onClick={() => sending.mutate([])}
            size="xs"
            variant="light"
          >
            {t("sync.secrets.sendAll", { count: sendable.length })}
          </Button>
        </Group>
        {data.keys.length === 0 ? (
          <Text c="dimmed" size="sm">
            {t("sync.secrets.empty")}
          </Text>
        ) : (
          <Table striped>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("sync.secrets.key")}</Table.Th>
                <Table.Th>{t("sync.secrets.status")}</Table.Th>
                <Table.Th>{t("sync.secrets.generations")}</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.keys.map((state) => (
                <SecretRow
                  busy={busy}
                  key={state.key}
                  onFetch={() => fetching.mutate([state.key])}
                  onSend={() => sending.mutate([state.key])}
                  state={state}
                />
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Stack>
    </Card>
  );
}

function SecretRow({
  busy,
  onFetch,
  onSend,
  state,
}: {
  busy: boolean;
  onFetch: () => void;
  onSend: () => void;
  state: WorkspaceSecretState;
}) {
  const { t } = useTranslation();
  const tone = secretTone(state.status);
  return (
    <Table.Tr>
      <Table.Td>
        <Code style={{ overflowWrap: "anywhere" }}>{state.key}</Code>
      </Table.Td>
      <Table.Td>
        <Badge color={tone === "ok" ? "success" : tone === "danger" ? "danger" : "warning"}>
          {t(`sync.secrets.state.${state.status}`)}
        </Badge>
      </Table.Td>
      <Table.Td>
        <Text c="dimmed" size="xs">
          {t("sync.secrets.generationDetail", {
            shared: state.shared_generation,
            here: state.local_generation ?? "-",
            hub: state.hub_generation ?? "-",
          })}
        </Text>
      </Table.Td>
      <Table.Td>
        <Group gap="xs" justify="flex-end">
          {state.can_fetch ? (
            <Button disabled={busy} onClick={onFetch} size="compact-xs" variant="light">
              {t("sync.secrets.fetch")}
            </Button>
          ) : null}
          {state.can_send ? (
            <Button disabled={busy} onClick={onSend} size="compact-xs" variant="subtle">
              {t("sync.secrets.send")}
            </Button>
          ) : null}
        </Group>
      </Table.Td>
    </Table.Tr>
  );
}
