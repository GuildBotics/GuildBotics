import { Button, Group, Stack, Text } from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { openExternal } from "../../../openExternal";
import {
  ORIGIN,
  branvilKey,
  enterBranvil,
  getConnection,
  startConnection,
  type Connection,
} from "./api";

const POLL_INTERVAL_MS = 2000;

/** GuildBotics' connection to the Branvil at ``baseUrl``; with ``state``, the
 * consent it names while the owner decides, which is polled until then. */
export function useConnection(baseUrl: string, state = "") {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: [...branvilKey("connection", baseUrl), state],
    queryFn: async () => {
      const connection = await getConnection(baseUrl, state);
      if (state && connection.status !== "pending") {
        // What the connection lists may now be read: read all of it again.
        void queryClient.invalidateQueries({
          queryKey: ["branvil"],
          predicate: (query) => query.queryKey[3] !== state,
        });
      }
      return connection;
    },
    enabled: ORIGIN.test(baseUrl),
    refetchInterval: (query) => (query.state.data?.status === "pending" ? POLL_INTERVAL_MS : false),
  });
}

/** The connection, as the cache holds it, for what is done after a save. */
export function heldConnection(
  queryClient: ReturnType<typeof useQueryClient>,
  baseUrl: string,
): Connection | undefined {
  return queryClient.getQueryData([...branvilKey("connection", baseUrl), ""]);
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Whether GuildBotics is connected to Branvil, connecting it on the owner's
 * consent in the browser, and the way into Branvil's GUI. */
export function BranvilConnection({ baseUrl }: { baseUrl: string }) {
  const { t } = useTranslation();
  const [consent, setConsent] = useState("");
  const [error, setError] = useState("");
  const connection = useConnection(baseUrl, consent).data;
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
    const started = await startConnection(baseUrl);
    setConsent(started.state);
    await openExternal(started.url);
  });
  const open = run(async () => openExternal((await enterBranvil(baseUrl)).url));
  const connected = connection?.status === "connected";

  return (
    <Stack gap={4}>
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
          <Button
            size="compact-sm"
            loading={pending}
            disabled={!ORIGIN.test(baseUrl)}
            onClick={() => void connect()}
          >
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
