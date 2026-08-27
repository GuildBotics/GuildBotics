import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Group,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
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
import { RequestErrorAlert } from "./RequestErrorAlert";
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
    // A transfer changes what the OS secret store holds, and snapshots other
    // sections have already read stay answered without it -- "is this
    // provider's key stored?" would say no after the fetch that stored it.
    // Which screens consult the store is not a list to maintain here, so
    // everything but the secret states this response just delivered is
    // invalidated, the same way joining a workspace invalidates everything.
    void queryClient.invalidateQueries({
      predicate: (query) => query.queryKey[0] !== SECRETS_QUERY_KEY[0],
    });
  };
  // A bulk action names no keys. The lists below are as old as the last poll,
  // and acting on them would let a key that has since been changed on another
  // machine be swept up -- which is the one decision these actions must not
  // make on their own. Sending nothing asks the backend to work out what to
  // move from the state it reads now. A row names its key, because that is a
  // decision the user made about that key.
  // A failed transfer may still have moved the hub's side -- a send whose
  // record could not be written leaves the hub ahead -- so the states are
  // asked for right away rather than left to the next poll, and the screen
  // shows what the failure left behind next to the message saying why.
  const refreshStates = () => void queryClient.invalidateQueries({ queryKey: SECRETS_QUERY_KEY });
  // One mutation for both directions. The alert below shows its error, which
  // a new attempt clears -- two mutations would keep a failed send on screen
  // through every later fetch, since neither ever resets the other.
  const transfer = useMutation({
    mutationFn: ({ kind, keys }: { kind: "fetch" | "send"; keys: string[] }) =>
      kind === "send" ? sendWorkspaceSecrets({ keys }) : fetchWorkspaceSecrets({ keys }),
    onSuccess: applied,
    onError: refreshStates,
  });
  const data = secrets.data;
  if (!data?.enabled) {
    return null;
  }
  const alert = secretAlert(data);
  // Only how many the buttons say, and whether there is anything to press.
  const fetchable = data.fetchable_keys;
  const sendable = data.sendable_keys;
  const busy = transfer.isPending;
  const active = transfer.isPending ? transfer.variables.kind : null;
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
        <RequestErrorAlert cause={transfer.error} title={t("sync.secrets.transferFailed")} />
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
            loading={active === "fetch"}
            onClick={() => transfer.mutate({ kind: "fetch", keys: [] })}
            size="xs"
          >
            {t("sync.secrets.fetchAll", { count: fetchable.length })}
          </Button>
          <Button
            disabled={sendable.length === 0 || busy}
            loading={active === "send"}
            onClick={() => transfer.mutate({ kind: "send", keys: [] })}
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
                  onFetch={() => transfer.mutate({ kind: "fetch", keys: [state.key] })}
                  onSend={() => transfer.mutate({ kind: "send", keys: [state.key] })}
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
  const badge = (
    <Badge color={tone === "ok" ? "success" : tone === "danger" ? "danger" : "warning"}>
      {t(`sync.secrets.state.${state.status}`)}
    </Badge>
  );
  return (
    <Table.Tr>
      <Table.Td>
        <Code style={{ overflowWrap: "anywhere" }}>{state.key}</Code>
      </Table.Td>
      <Table.Td>
        {state.status === "ready" ? (
          badge
        ) : (
          // The label alone says that something is off, not what happened or
          // which action settles it -- "needs checking" reads as nothing at
          // all. The detail says both, and only the in-step state goes
          // without one.
          <Tooltip label={t(`sync.secrets.stateDetail.${state.status}`)} maw={340} multiline>
            {badge}
          </Tooltip>
        )}
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
