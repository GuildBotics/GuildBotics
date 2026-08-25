import { Alert, Anchor, Button, Group, Text } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router";
import { TriangleAlert } from "lucide-react";

import { getWorkspaceSyncStatus, retryWorkspaceSync } from "../api/client";
import { SYNC_SETTINGS_PATH } from "./SyncIndicator";
import { syncCanRetry, syncIndicatorState, syncNeedsAttention, syncTone } from "./syncState";

/**
 * The synchronization half of the app-wide warning band.
 *
 * Only states the user has to act on appear here; progress belongs to the
 * sidebar indicator, which is always on screen anyway. The band carries a
 * summary and a way in -- the list of what is actually wrong lives on the
 * settings screen, so the warning does not grow with the problem.
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
  const status = useQuery({
    queryKey: ["workspace-sync"],
    queryFn: getWorkspaceSyncStatus,
    refetchInterval: 5000,
  });
  const retry = useMutation({
    mutationFn: retryWorkspaceSync,
    onSuccess: (next) => queryClient.setQueryData(["workspace-sync"], next),
  });
  const state = syncIndicatorState(status.data);
  const rejected = status.data?.rejected_changes.length ?? 0;
  const count = status.data?.unsendable_changes.length ?? 0;
  const liveClientUpdateRequired = status.data?.live_error_code === "live_client_update_required";
  // Both at once is an ordinary combination -- a hub that cannot be reached
  // says nothing about what it already refused -- so neither hides the other.
  return (
    <>
      {syncNeedsAttention(state) ? (
        <Alert
          color={syncTone(state) === "danger" ? "danger" : "warning"}
          icon={<TriangleAlert size={18} />}
          title={t(`sync.state.${state}.label`)}
        >
          <Group align="center" gap="md" wrap="wrap">
            <Text size="sm">{t(`sync.alerts.${state}`, { count })}</Text>
            <Group gap="xs">
              {syncCanRetry(state) ? (
                <Button
                  loading={retry.isPending}
                  onClick={() => retry.mutate()}
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
        </Alert>
      ) : null}
      {rejected > 0 ? <RejectedAlert count={rejected} /> : null}
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
 * Changes of the user's that the hub did not accept, still held here.
 *
 * The count is all the band carries; which ones, and the identifier each is
 * recoverable by, live on the settings screen. The warning stays until the
 * user discards them there, because until then they are still holding content
 * that exists on no other machine.
 */
function RejectedAlert({ count }: { count: number }) {
  const { t } = useTranslation();
  return (
    <Alert color="warning" icon={<TriangleAlert size={18} />} title={t("sync.rejected.alertTitle")}>
      <Group align="center" gap="md" wrap="wrap">
        <Text size="sm">{t("sync.rejected.alert", { count })}</Text>
        <Anchor component={NavLink} size="sm" to={SYNC_SETTINGS_PATH} underline="hover">
          {t("sync.actions.settings")}
        </Anchor>
      </Group>
    </Alert>
  );
}
