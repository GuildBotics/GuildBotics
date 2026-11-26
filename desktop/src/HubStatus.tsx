import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Alert, Anchor, Button, List, Stack, Text } from "@mantine/core";
import { HubVersionLabel, type HubStatus, type HubUpdate } from "./ui/HubStatus";
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
  if (!version && !status?.update) return null;
  return (
    <Stack gap={2}>
      {version && (
        <Text>
          {t("hub.version")}: <HubVersionLabel version={version} t={t} />
          {releases?.running && ` · ${releases.running}`}
        </Text>
      )}
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
      {status?.update && <HubUpdateNotice key={status.update.sha} update={status.update} />}
    </Stack>
  );
}

export function HubUpdateNotice({ update }: { update: HubUpdate }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const confirm = useMutation({
    mutationFn: () => request("/hub-status/confirm", { method: "POST", body: { sha: update.sha } }),
    onSettled: () => client.invalidateQueries({ queryKey: ["hub-status"] }),
  });
  if (update.kind === "failed") {
    return (
      <Alert color="danger" title={t("hub.failed")}>
        <Text size="sm">{t("hub.failureStage", { stage: update.stage })}</Text>
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
