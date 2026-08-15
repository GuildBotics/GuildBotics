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
  if (!syncNeedsAttention(state)) {
    return null;
  }
  const count = status.data?.unsendable_changes.length ?? 0;
  return (
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
  );
}
