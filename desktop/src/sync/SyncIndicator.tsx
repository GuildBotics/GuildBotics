import { Anchor, Button, Group, Popover, Stack, Text } from "@mantine/core";
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

import type { WorkspaceSyncStatus } from "../api/client";
import { SyncFailureDetail } from "./SyncFailureDetail";
import {
  mostUrgentSyncState,
  syncCanRetry,
  syncTone,
  type ScopedSyncStatus,
  type SyncIndicatorState,
} from "./syncState";
import { useSyncRetry, useSyncStatuses } from "./useSyncStatus";

/** Where the settings for hubs, devices, and unsendable changes live. */
export const SYNC_SETTINGS_PATH = "/setup?section=sync";

const ICONS: Record<SyncIndicatorState, typeof CircleCheck> = {
  update_required: TriangleAlert,
  invalid_shared_state: TriangleAlert,
  not_connected: CircleAlert,
  unreachable: CircleAlert,
  local_error: CircleAlert,
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
 * The workspace and the members' identities synchronize separately, and the
 * line is the more urgent of the two: a member's rename that does not reach the
 * other machines is the same question as a workspace edit that does not. The
 * detail says which is which.
 *
 * Nothing synchronizing shows nothing at all: synchronization is not a
 * feature that is switched off, it is one that was never set up, and an
 * indicator saying so on every screen would be noise.
 */
export function SyncIndicator() {
  const { t } = useTranslation();
  const [opened, setOpened] = useState(false);
  const scoped = useSyncStatuses();
  const retry = useSyncRetry();
  const state = mostUrgentSyncState(scoped.map((item) => item.state));
  if (state === "disabled") {
    return null;
  }
  const Icon = ICONS[state];
  const label = t(`sync.state.${state}.label`);
  const shown = scoped.filter((item) => item.state !== "disabled");
  const retryable = shown.filter((item) => syncCanRetry(item.state));
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
          {shown.map((item) => (
            <ScopeDetail item={item} key={item.scope} />
          ))}
          <Group gap="xs">
            {retryable.length > 0 ? (
              <Button
                loading={retry.isPending}
                onClick={() => retry.mutate(retryable.map((item) => item.scope))}
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

/** One scope's state, what it last printed, and what it is waiting on. */
function ScopeDetail({ item }: { item: ScopedSyncStatus }) {
  const { t } = useTranslation();
  return (
    <Stack gap={2}>
      <Text fw={600} size="xs">
        {t(`sync.indicator.scope.${item.scope}`, {
          state: t(`sync.state.${item.state}.label`),
        })}
      </Text>
      <SyncFailureDetail status={item.status} />
      <SyncCounts status={item.status} />
    </Stack>
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
