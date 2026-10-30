import { ActionIcon, Alert, Anchor, Group, Stack, Text, Tooltip } from "@mantine/core";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Download, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { getHandoffFiles, handoffFileUrl } from "../api/client";
import { formatBytes, formatDateTime } from "../App";

/**
 * The files in the server machine's handoff folder, each one downloadable:
 * what a command left there is otherwise out of reach of a browser on
 * another machine.
 *
 * `refreshKey` changes when a run ends, which is when new files appear.
 */
export function HandoffFiles({ refreshKey }: { refreshKey: string }) {
  const { t } = useTranslation();
  const handoff = useQuery({
    queryKey: ["handoff-files", refreshKey],
    queryFn: getHandoffFiles,
    placeholderData: keepPreviousData,
  });
  const files = handoff.data?.files ?? [];

  return (
    <section className="command-handoff" aria-label={t("commands.handoff.title")}>
      <Group gap="xs" justify="space-between" wrap="nowrap">
        <Text fw={600} size="sm">
          {t("commands.handoff.title")}
        </Text>
        <Tooltip label={t("commands.handoff.refresh")}>
          <ActionIcon
            aria-label={t("commands.handoff.refresh")}
            size="sm"
            variant="subtle"
            loading={handoff.isFetching}
            onClick={() => void handoff.refetch()}
          >
            <RefreshCw size={14} />
          </ActionIcon>
        </Tooltip>
      </Group>
      {handoff.data ? (
        <Text size="xs" c="dimmed" ff="monospace" style={{ wordBreak: "break-all" }}>
          {handoff.data.directory}
        </Text>
      ) : null}
      {handoff.error ? (
        <Alert color="danger">{handoff.error.message}</Alert>
      ) : handoff.data && files.length === 0 ? (
        <Text size="xs" c="dimmed">
          {t("commands.handoff.empty")}
        </Text>
      ) : (
        <Stack gap={2} className="command-handoff-files">
          {files.map((file) => (
            <Group key={file.path} gap="xs" justify="space-between" wrap="nowrap">
              <Anchor
                href={handoffFileUrl(file.path)}
                download={file.path.split("/").pop()}
                size="sm"
                style={{ minWidth: 0, wordBreak: "break-all" }}
              >
                <Download size={12} /> {file.path}
              </Anchor>
              <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>
                {formatBytes(file.size)} · {formatDateTime(file.modified_at)}
              </Text>
            </Group>
          ))}
        </Stack>
      )}
    </section>
  );
}
