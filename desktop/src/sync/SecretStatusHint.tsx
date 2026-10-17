import { Anchor, Group, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router";

import { getWorkspaceSecrets, type SyncScope } from "../api/client";
import { SECRETS_REFETCH_MS, secretNeedsAttention, secretsQueryKey } from "./secretState";
import { SYNC_SETTINGS_PATH } from "./SyncIndicator";

/**
 * What the other machines know about one credential, shown where it is typed.
 *
 * Only the state and the way to the sync screen belong here. Sending and
 * fetching stay on that screen: a value leaving this machine is a decision of
 * its own, and scattering the buttons across every form that happens to hold a
 * credential would make it an incidental one.
 *
 * Nothing is rendered when the scope has no hub, when the key is unknown,
 * or when this machine is simply in step -- the field's own "configured" text
 * already says that, and repeating it would bury the cases that matter.
 *
 * A member's credential is the identities' (``scope`` "identities"), which
 * every workspace shares; an API key is the workspace's.
 */
export function SecretStatusHint({
  envKey,
  scope = "workspace",
}: {
  envKey: string | undefined;
  scope?: SyncScope;
}) {
  const { t } = useTranslation();
  const secrets = useQuery({
    queryKey: secretsQueryKey(scope),
    queryFn: () => getWorkspaceSecrets(scope),
    refetchInterval: SECRETS_REFETCH_MS,
  });
  if (!envKey || !secrets.data?.enabled) {
    return null;
  }
  const state = secrets.data.keys.find((entry) => entry.key === envKey);
  if (!state || !secretNeedsAttention(state.status)) {
    return null;
  }
  return (
    <Group gap="xs" wrap="wrap">
      <Text c="dimmed" size="xs">
        {t(`sync.secrets.hint.${state.status}`)}
      </Text>
      <Anchor component={NavLink} size="xs" to={SYNC_SETTINGS_PATH} underline="hover">
        {t("sync.secrets.hint.link")}
      </Anchor>
    </Group>
  );
}
