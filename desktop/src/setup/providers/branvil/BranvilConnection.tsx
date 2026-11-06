import { Button, Code, CopyButton, Group, Stack, Text } from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { openExternal } from "../../../openExternal";
import { branvilKey, getConnection, startConnection, type Connection } from "./api";
import { openBranvil } from "./openBranvil";

const POLL_INTERVAL_MS = 2000;

/** GuildBotics' connection to Branvil; with ``state``, the consent it names
 * while the owner decides, which is polled until then. */
export function useConnection(state = "") {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: [...branvilKey("connection"), state],
    queryFn: async () => {
      const connection = await getConnection(state);
      if (state && connection.status !== "pending") {
        // What the connection lists may now be read: read all of it again.
        void queryClient.invalidateQueries({
          queryKey: ["branvil"],
          predicate: (query) => query.queryKey[2] !== state,
        });
      }
      return connection;
    },
    refetchInterval: (query) => (query.state.data?.status === "pending" ? POLL_INTERVAL_MS : false),
  });
}

/** The connection, as the cache holds it, for what is done after a save. */
export function heldConnection(
  queryClient: ReturnType<typeof useQueryClient>,
): Connection | undefined {
  return queryClient.getQueryData([...branvilKey("connection"), ""]);
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Whether GuildBotics is connected to Branvil, connecting it on the owner's
 * consent in the browser, and the way into Branvil's GUI. The connection is
 * the identities': one Branvil for every workspace. */
export function BranvilConnection() {
  const { t } = useTranslation();
  const [consent, setConsent] = useState("");
  const [error, setError] = useState("");
  const connection = useConnection(consent).data;
  const pending = Boolean(consent) && (connection?.status ?? "pending") === "pending";

  const run = (action: () => Promise<void>) => async () => {
    setError("");
    try {
      await action();
    } catch (failure) {
      setError(message(failure));
    }
  };
  const connect = run(async () => {
    const started = await startConnection();
    setConsent(started.state);
    await openExternal(started.url);
  });
  const open = run(openBranvil);
  const connected = connection?.status === "connected";

  return (
    <Stack gap={4}>
      {connection?.base_url && (
        <Text size="sm">
          {t("setup.branvil.baseUrl")}: {connection.base_url}
        </Text>
      )}
      <Group gap="xs">
        {connected ? (
          <>
            <Text size="sm" c="teal">
              {t("setup.branvil.connection.connected", { owner: connection.owner })}
            </Text>
            <Button size="compact-sm" onClick={() => void open()}>
              {t("setup.branvil.connection.open")}
            </Button>
            <Button size="compact-sm" variant="subtle" onClick={() => void connect()}>
              {t("setup.branvil.connection.reconnect")}
            </Button>
          </>
        ) : (
          <Button size="compact-sm" loading={pending} onClick={() => void connect()}>
            {t("setup.branvil.connection.connect")}
          </Button>
        )}
      </Group>
      <Text size="xs" c={error ? "danger" : "dimmed"}>
        {pending
          ? t("setup.branvil.connection.pending")
          : error || (connected ? "" : connection?.message || t("setup.branvil.connection.hint"))}
      </Text>
    </Stack>
  );
}

/** The git configuration that lets the owner's own checkouts on this machine
 * reach Branvil: git asks GuildBotics for this machine's token instead of a
 * name and a password. Shown while connected. */
export function BranvilGitConfig() {
  const { t } = useTranslation();
  const config = useConnection().data?.git_config;
  if (!config) {
    return null;
  }
  return (
    <Stack gap={4}>
      <Text size="sm" fw={500}>
        {t("setup.branvil.git.title")}
      </Text>
      <Text size="xs" c="dimmed">
        {t("setup.branvil.git.hint")}
      </Text>
      <Code block aria-label={t("setup.branvil.git.title")}>
        {config}
      </Code>
      <Group>
        <CopyButton value={config}>
          {({ copied, copy }) => (
            <Button size="compact-sm" variant="light" onClick={copy}>
              {t(copied ? "setup.branvil.git.copied" : "setup.branvil.git.copy")}
            </Button>
          )}
        </CopyButton>
      </Group>
    </Stack>
  );
}
