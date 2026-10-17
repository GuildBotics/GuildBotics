import { Alert, Anchor, Button, Group, Stack, Text } from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router";
import { TriangleAlert } from "lucide-react";
import { useEffect, useRef } from "react";

import { getWorkspaceSecrets, type SyncScope } from "../api/client";
import { SECRETS_REFETCH_MS, secretAlert, secretsQueryKey } from "./secretState";
import { SyncFailureDetail } from "./SyncFailureDetail";
import { SYNC_SETTINGS_PATH } from "./SyncIndicator";
import { syncAttentionGroups, syncCanRetry, syncTone, type ScopedSyncStatus } from "./syncState";
import { useSyncRetry, useSyncStatuses } from "./useSyncStatus";

/**
 * The synchronization half of the app-wide warning band.
 *
 * Only states the user has to act on appear here; progress belongs to the
 * sidebar indicator, which is always on screen anyway. The band carries a
 * summary and a way in -- the list of what is actually wrong lives on the
 * settings screen, so the warning does not grow with the problem.
 *
 * The workspace and the members' identities synchronize separately, so each
 * has its warnings, named for whose they are. A state both are in -- a hub that
 * is down -- is one warning about both.
 *
 * A change the hub did not accept is one of those states, not an event that
 * scrolls past. It is rare, and each one means an edit of the user's was set
 * aside; a marker on the activity timeline is findable only by someone already
 * looking for it. What ends the warning is the user saying they are done with
 * the change, since nothing deletes the ref holding it.
 */
export function SyncAlerts() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const scoped = useSyncStatuses();
  const workspace = scoped.find((item) => item.scope === "workspace");
  // A head change includes both adoption and reconciliation. Rejections are
  // also observed because a short-lived local head can fall between polls.
  // Refresh reads, never the editable form: it compares its own revisions.
  const previous = useRef<{ workspace: string | null; revision: string } | null>(null);
  useEffect(() => {
    if (!workspace?.status) return;
    const workspaceId = workspace.status.workspace_id;
    const revision = JSON.stringify(
      scoped.map(({ status }) => [
        status?.local_head ?? null,
        (status?.rejected_changes ?? []).map((change) => change.rejection_id).sort(),
      ]),
    );
    const last = previous.current;
    previous.current = { workspace: workspaceId, revision };
    if (last?.workspace === workspaceId && last.revision !== revision) {
      void queryClient.invalidateQueries({ queryKey: ["team"] });
      void queryClient.invalidateQueries({ queryKey: ["member-config"] });
    }
  }, [queryClient, scoped, workspace?.status]);
  const secrets = useQuery({
    queryKey: secretsQueryKey("workspace"),
    queryFn: () => getWorkspaceSecrets("workspace"),
    refetchInterval: SECRETS_REFETCH_MS,
  });
  const identitySecrets = useQuery({
    queryKey: secretsQueryKey("identities"),
    queryFn: () => getWorkspaceSecrets("identities"),
    refetchInterval: SECRETS_REFETCH_MS,
  });
  const liveClientUpdateRequired =
    workspace?.status?.live_error_code === "live_client_update_required";
  // A state and a set-aside change at once is an ordinary combination -- a
  // hub that cannot be reached says nothing about what it already refused --
  // so neither hides the other.
  return (
    <>
      {syncAttentionGroups(scoped).map((group) => (
        <StateAlert group={group} key={group[0]?.state} />
      ))}
      {scoped.map(({ scope, status }) =>
        (status?.rejected_changes.length ?? 0) > 0 ? (
          <RejectedAlert count={status?.rejected_changes.length ?? 0} key={scope} scope={scope} />
        ) : null,
      )}
      {secretAlert(identitySecrets.data) === "attention" ? (
        <SecretsAlert scope="identities" count={identitySecrets.data?.attention_count ?? 0} />
      ) : null}
      {secretAlert(secrets.data) === "attention" ? (
        <SecretsAlert scope="workspace" count={secrets.data?.attention_count ?? 0} />
      ) : null}
      {liveClientUpdateRequired ? (
        <Alert
          color="warning"
          icon={<TriangleAlert size={18} />}
          title={t("sync.live.updateRequiredTitle")}
        >
          <Text size="sm">{t("sync.live.updateRequired")}</Text>
        </Alert>
      ) : null}
    </>
  );
}

/**
 * One state that needs the user, in every scope it holds for.
 *
 * Named for the identities only when they alone are in it: the workspace's
 * warnings read as they always have, and a warning about both needs no name.
 */
function StateAlert({ group }: { group: ScopedSyncStatus[] }) {
  const { t } = useTranslation();
  const retry = useSyncRetry();
  const state = group[0]?.state ?? "disabled";
  const label = t(`sync.state.${state}.label`);
  const count = group.reduce((sum, item) => sum + (item.status?.unsendable_changes.length ?? 0), 0);
  // Both scopes of a hub that is down failed the same way; their words are
  // shown once.
  const failures = group.filter(
    (item, index) =>
      item.status?.last_error_detail &&
      group.findIndex(
        (other) => other.status?.last_error_detail === item.status?.last_error_detail,
      ) === index,
  );
  return (
    <Alert
      color={syncTone(state) === "danger" ? "danger" : "warning"}
      icon={<TriangleAlert size={18} />}
      title={
        group.length === 1 && group[0]?.scope === "identities"
          ? t("sync.identities.title", { label })
          : label
      }
    >
      <Stack gap="xs">
        <Group align="center" gap="md" wrap="wrap">
          <Text size="sm">{t(`sync.alerts.${state}`, { count })}</Text>
          <Group gap="xs">
            {syncCanRetry(state) ? (
              <Button
                loading={retry.isPending}
                onClick={() => retry.mutate(group.map((item) => item.scope))}
                size="xs"
                variant="light"
              >
                {t("sync.actions.retry")}
              </Button>
            ) : null}
            <Anchor component={NavLink} size="sm" to={SYNC_SETTINGS_PATH} underline="hover">
              {t("sync.actions.settings")}
            </Anchor>
          </Group>
        </Group>
        {failures.map((item) => (
          <SyncFailureDetail key={item.scope} status={item.status} />
        ))}
      </Stack>
    </Alert>
  );
}

/**
 * Credentials this machine is short of, or has not given the hub.
 *
 * The band carries the count and the way in. Which keys, and what to do about
 * each, are on the settings screen -- and so is every transfer, because a value
 * leaving or arriving on a machine is a decision rather than a notification.
 */
function SecretsAlert({ scope, count }: { scope: SyncScope; count: number }) {
  const { t } = useTranslation();
  return (
    <Alert
      color="warning"
      icon={<TriangleAlert size={18} />}
      title={t(`sync.secrets.title.${scope}`)}
    >
      <Group align="center" gap="md" wrap="wrap">
        <Text size="sm">{t("sync.secrets.alert.attention", { count })}</Text>
        <Anchor component={NavLink} size="sm" to={SYNC_SETTINGS_PATH} underline="hover">
          {t("sync.secrets.hint.link")}
        </Anchor>
      </Group>
    </Alert>
  );
}

/**
 * Changes of the user's that the hub did not accept, still held here.
 *
 * The count is all the band carries; which ones, and the identifier each is
 * recoverable by, live on the settings screen. The warning stays until the
 * user discards them there, because until then they are still holding content
 * that exists on no other machine.
 */
function RejectedAlert({ scope, count }: { scope: SyncScope; count: number }) {
  const { t } = useTranslation();
  return (
    <Alert
      color="warning"
      icon={<TriangleAlert size={18} />}
      title={t(`sync.rejected.alertTitle.${scope}`)}
    >
      <Group align="center" gap="md" wrap="wrap">
        <Text size="sm">{t("sync.rejected.alert", { count })}</Text>
        <Anchor component={NavLink} size="sm" to={SYNC_SETTINGS_PATH} underline="hover">
          {t("sync.actions.settings")}
        </Anchor>
      </Group>
    </Alert>
  );
}
