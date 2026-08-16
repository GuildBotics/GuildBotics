import { Anchor, Button, Group, Popover, Stack, Text } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router";
import {
  CircleAlert,
  CircleCheck,
  CircleSlash,
  Download,
  TriangleAlert,
  Upload,
} from "lucide-react";

import {
  getWorkspaceSyncStatus,
  retryWorkspaceSync,
  type WorkspaceSyncStatus,
} from "../api/client";
import { syncCanRetry, syncIndicatorState, syncTone, type SyncIndicatorState } from "./syncState";

/** Where the settings for hubs, devices, and unsendable changes live. */
export const SYNC_SETTINGS_PATH = "/setup?section=sync";

const ICONS: Record<SyncIndicatorState, typeof CircleCheck> = {
  update_required: TriangleAlert,
  invalid_shared_state: TriangleAlert,
  unreachable: CircleAlert,
  unsendable: CircleAlert,
  receiving: Download,
  sending: Upload,
  synced: CircleCheck,
  disabled: CircleSlash,
};

/**
 * A small, always-visible line above the navigation saying whether this
 * machine and the hub hold the same thing. Selecting it opens the detail.
 *
 * A workspace with no hub shows nothing at all: synchronization is not a
 * feature that is switched off, it is one that was never set up, and an
 * indicator saying so on every screen would be noise.
 */
export function SyncIndicator() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [opened, setOpened] = useState(false);
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
  if (state === "disabled") {
    return null;
  }
  const Icon = ICONS[state];
  const label = t(`sync.state.${state}.label`);
  return (
    <Popover
      opened={opened}
      onChange={setOpened}
      position="right-start"
      shadow="md"
      width={320}
      withArrow
    >
      <Popover.Target>
        <button
          aria-expanded={opened}
          aria-label={t("sync.indicator.aria", { state: label })}
          className={`sync-indicator ${syncTone(state)}`}
          onClick={() => setOpened((current) => !current)}
          type="button"
        >
          <Icon size={14} />
          <span className="sync-indicator-label">{label}</span>
        </button>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Text fw={600} size="sm">
            {label}
          </Text>
          <Text c="dimmed" size="xs">
            {t(`sync.state.${state}.detail`)}
          </Text>
          <SyncCounts status={status.data} />
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
            <Anchor
              component={NavLink}
              onClick={() => setOpened(false)}
              size="xs"
              to={SYNC_SETTINGS_PATH}
            >
              {t("sync.actions.settings")}
            </Anchor>
          </Group>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}

function SyncCounts({ status }: { status: WorkspaceSyncStatus | undefined }) {
  const { t } = useTranslation();
  if (!status) {
    return null;
  }
  const lines = [
    status.ahead_count > 0 ? t("sync.counts.ahead", { count: status.ahead_count }) : "",
    status.behind_count > 0 ? t("sync.counts.behind", { count: status.behind_count }) : "",
    status.unsendable_changes.length > 0
      ? t("sync.counts.unsendable", { count: status.unsendable_changes.length })
      : "",
    status.last_success_at
      ? t("sync.counts.lastSuccess", {
          time: new Date(status.last_success_at).toLocaleString(),
        })
      : "",
  ].filter(Boolean);
  if (lines.length === 0) {
    return null;
  }
  return (
    <Stack gap={2}>
      {lines.map((line) => (
        <Text key={line} size="xs">
          {line}
        </Text>
      ))}
    </Stack>
  );
}
