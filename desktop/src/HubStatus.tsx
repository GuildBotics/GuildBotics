import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Text } from "@mantine/core";
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
  const version = useHubStatus().data?.current;
  return version ? (
    <Text>
      {t("hub.version")}: <HubVersionLabel version={version} t={t} />
    </Text>
  ) : null;
}
