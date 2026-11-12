import { Anchor, Alert, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { getRunFiles, runFileUrl } from "../api/client";

export function RunFiles({ traceId, finished }: { traceId: string; finished: boolean }) {
  const { t } = useTranslation();
  const files = useQuery({
    queryKey: ["run-files", traceId],
    queryFn: () => getRunFiles(traceId),
    enabled: finished,
    refetchInterval: (query) => (query.state.data?.pending ? 1000 : false),
  });
  if (!finished) return null;
  if (files.error) return <Alert color="danger">{String(files.error)}</Alert>;
  if (!files.data || (!files.data.files.length && !files.data.oversized?.length)) return null;
  return (
    <Stack gap="xs">
      <Text size="sm">{t("commands.runFiles")}</Text>
      {files.data.oversized?.map((path) => (
        <Text key={path} size="sm">
          {path}: {t("commands.runFileTooLarge")}
        </Text>
      ))}
      {files.data.files.map((path) => (
        <Anchor key={path} href={runFileUrl(traceId, path)} download={path.split("/").pop()}>
          {path}
        </Anchor>
      ))}
    </Stack>
  );
}
