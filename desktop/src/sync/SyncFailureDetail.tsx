import { Code, Stack, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";

import type { WorkspaceSyncStatus } from "../api/client";

/**
 * What Git, ssh, or the hub printed when synchronization last failed.
 *
 * Shown as printed rather than summarized: a name that does not resolve, a key
 * the hub has not registered, and a hub whose own `git` cannot run all look the
 * same from here, and only their own words tell the user which one to fix.
 */
export function SyncFailureDetail({ status }: { status: WorkspaceSyncStatus | undefined }) {
  const { t } = useTranslation();
  const detail = status?.last_error_detail;
  if (!detail) {
    return null;
  }
  return (
    <Stack gap={4}>
      <Text fw={600} size="xs">
        {t("sync.failureDetail")}
      </Text>
      <Code
        block
        style={{
          maxHeight: "8rem",
          overflowWrap: "anywhere",
          overflowY: "auto",
          whiteSpace: "pre-wrap",
        }}
      >
        {detail}
      </Code>
    </Stack>
  );
}
