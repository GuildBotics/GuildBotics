import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Anchor, Stack, Text } from "@mantine/core";
import { HubVersionLabel, type HubStatus } from "./ui/HubStatus";
import { request } from "./api/client";
export const useHubStatus = () =>
  useQuery({
    queryKey: ["hub-status"],
    queryFn: () => request<HubStatus>("/hub-status"),
    refetchInterval: (query) => (query.state.data?.operation ? 2000 : 30_000),
  });
export function HubVersion() {
  const { t } = useTranslation();
  const status = useHubStatus().data;
  const version = status?.current;
  const releases = status?.releases;
  if (!version) return null;
  return (
    <Stack gap={2}>
      <Text>
        {t("hub.version")}: <HubVersionLabel version={version} t={t} />
        {releases?.running && ` · ${releases.running}`}
      </Text>
      {releases && (
        <Text size="sm" c="dimmed">
          {releases.pending.length > 0
            ? t("hub.pendingReleases", {
                names: releases.pending.map((r) => r.name).join(t("hub.separator")),
              })
            : t("hub.noPendingReleases")}{" "}
          <Anchor href={releases.url} size="sm">
            {t("hub.releasesPage")}
          </Anchor>
        </Text>
      )}
    </Stack>
  );
}
