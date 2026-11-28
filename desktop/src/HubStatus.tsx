import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Alert, Anchor, Button, List, Stack, Text } from "@mantine/core";
import type { HubStatus, HubUpdate } from "./ui/HubStatus";
import { request } from "./api/client";
export const useHubStatus = () =>
  useQuery({
    queryKey: ["hub-status"],
    queryFn: () => request<HubStatus>("/hub-status"),
    refetchInterval: (query) => (query.state.data?.operation ? 2000 : 30_000),
  });

export function HubUpdateNotice({ update }: { update: HubUpdate }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const confirm = useMutation({
    mutationFn: () => request("/hub-status/confirm", { method: "POST", body: { sha: update.sha } }),
    onSettled: () => client.invalidateQueries({ queryKey: ["hub-status"] }),
  });
  if (update.kind === "failed") {
    const stage = t(`hub.stage.${update.stage}`, { defaultValue: t("hub.stage.unknown") });
    return (
      <Alert color="danger" title={t("hub.failed")}>
        <Text size="sm">{t("hub.failureStage", { stage })}</Text>
        <Text size="sm" fw={500}>
          {t("hub.failureDetails")}
        </Text>
        <Text size="sm">{update.reason}</Text>
        <Text size="sm">{t("hub.failureNext", { next: update.next })}</Text>
      </Alert>
    );
  }
  return (
    <Alert color="warning" title={t("hub.manualRequired", { release: update.release })}>
      <Stack gap="xs" align="flex-start">
        <List size="sm">
          {update.steps.map((step) => (
            <List.Item key={step.version}>
              <Anchor href={step.ticket}>{step.ticket}</Anchor>
            </List.Item>
          ))}
        </List>
        {update.confirmed || confirm.isSuccess ? (
          <Text size="sm" role="status">
            {t("hub.manualConfirmed")}
          </Text>
        ) : (
          <Button size="xs" loading={confirm.isPending} onClick={() => confirm.mutate()}>
            {t("hub.confirmManual")}
          </Button>
        )}
        {confirm.error && (
          <Text size="sm" role="alert">
            {confirm.error.message}
          </Text>
        )}
      </Stack>
    </Alert>
  );
}
