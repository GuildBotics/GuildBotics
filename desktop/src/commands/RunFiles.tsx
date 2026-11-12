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
  });
  if (!finished) return null;
  if (files.error) return <Alert color="danger">{String(files.error)}</Alert>;
  if (!files.data?.files.length) return null;
  return (
    <Stack gap="xs">
      <Text size="sm">{t("commands.runFiles")}</Text>
      {files.data.files.map((path) => (
        <Anchor key={path} href={runFileUrl(traceId, path)} download={path.split("/").pop()}>
          {path}
        </Anchor>
      ))}
    </Stack>
  );
}
