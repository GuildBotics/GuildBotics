import { Alert, Button, Code, Group, Stack, Text, TextInput } from "@mantine/core";
import { useMutation } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { CircleAlert, TriangleAlert } from "lucide-react";

import { ApiRequestError, inspectHub, trustHub, type HubConnection } from "../api/client";

/**
 * Reaching a hub, and confirming it is the machine the user meant.
 *
 * Both the connect flow and taking a copy need the same three steps before they
 * differ, so they share them here: address, host key, then the hub's workspace
 * list. What to do with a chosen workspace is the caller's business.
 */
export function HubConnector({
  children,
  onEndpointChange,
}: {
  /** Rendered once the hub is reachable and trusted. */
  children: (connection: HubConnection, endpoint: string) => ReactNode;
  onEndpointChange?: () => void;
}) {
  const { t } = useTranslation();
  const [endpoint, setEndpoint] = useState("");
  const [connection, setConnection] = useState<HubConnection | null>(null);
  const [error, setError] = useState("");

  const report = (cause: unknown) => {
    setError(cause instanceof ApiRequestError ? cause.message : String(cause));
  };
  const accept = (found: HubConnection) => {
    setError("");
    setConnection(found);
  };
  const inspect = useMutation({
    mutationFn: () => inspectHub({ endpoint: endpoint.trim() }),
    onSuccess: accept,
    onError: report,
  });
  const trust = useMutation({
    mutationFn: (fingerprint: string) => trustHub({ endpoint: endpoint.trim(), fingerprint }),
    onSuccess: accept,
    onError: report,
  });

  return (
    <Stack gap="sm">
      <Text c="dimmed" size="sm">
        {t("sync.connect.endpointHint")}
      </Text>
      <Group align="flex-end" gap="sm">
        <TextInput
          label={t("sync.connect.endpoint")}
          onChange={(event) => {
            setEndpoint(event.currentTarget.value);
            setConnection(null);
            setError("");
            onEndpointChange?.();
          }}
          placeholder="user@hub.local"
          style={{ flex: 1 }}
          value={endpoint}
        />
        <Button loading={inspect.isPending} onClick={() => inspect.mutate()}>
          {t("sync.connect.inspect")}
        </Button>
      </Group>
      {error ? (
        <Alert color="danger" icon={<CircleAlert size={18} />} title={t("sync.connect.failed")}>
          {error}
        </Alert>
      ) : null}
      {connection !== null && !connection.host_key_trusted ? (
        <HostKeyConfirmation
          changed={connection.host_key_changed}
          fingerprints={connection.host_key_fingerprints}
          onConfirm={(fingerprint) => trust.mutate(fingerprint)}
          pending={trust.isPending}
        />
      ) : null}
      {connection !== null && connection.host_key_trusted
        ? children(connection, endpoint.trim())
        : null}
    </Stack>
  );
}

/**
 * The host key round trip.
 *
 * Synchronization runs OpenSSH with `BatchMode=yes`, so its own first-contact
 * prompt never appears. This is the only moment a person can compare the key,
 * which is why the fingerprint the user selected is what gets sent back rather
 * than a bare "I confirmed" flag: the machine must not be able to answer with a
 * different key than the one that was read.
 *
 * A key that replaces one this device already stored is the same round trip
 * with a different thing to say: a rebuilt hub and an impostor look identical
 * from here, so the screen says which of the two situations it is in and leaves
 * the judgement to the person comparing the fingerprints.
 */
function HostKeyConfirmation({
  changed,
  fingerprints,
  onConfirm,
  pending,
}: {
  changed: boolean;
  fingerprints: string[];
  onConfirm: (fingerprint: string) => void;
  pending: boolean;
}) {
  const { t } = useTranslation();
  return (
    <Alert
      color="warning"
      icon={<TriangleAlert size={18} />}
      title={changed ? t("sync.hostKey.changedTitle") : t("sync.hostKey.title")}
    >
      <Stack gap="xs">
        <Text size="sm">{changed ? t("sync.hostKey.changedBody") : t("sync.hostKey.body")}</Text>
        <Stack gap="xs">
          {fingerprints.map((fingerprint) => (
            <Group gap="sm" key={fingerprint} wrap="nowrap">
              <Code style={{ overflowWrap: "anywhere" }}>{fingerprint}</Code>
              <Button
                disabled={pending}
                onClick={() => onConfirm(fingerprint)}
                size="xs"
                variant="light"
              >
                {t("sync.hostKey.confirm")}
              </Button>
            </Group>
          ))}
        </Stack>
        <Text c="dimmed" size="xs">
          {t("sync.hostKey.compareHint")}
        </Text>
      </Stack>
    </Alert>
  );
}
