import { Button, Card, Group, PasswordInput, Stack, Text } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import * as api from "../../api/endpoints";
import type { Webhook } from "../../api/types";
import { ErrorMessage, Loading, MASKED_SECRET, StateBadge } from "../../components/common";
import { PanelHeader } from "../../components/PanelHeader";
import { useT } from "../../i18n";
import { useWebhook } from "../../query/hooks";
import { keys } from "../../query/keys";

/** The webhook told of automatic publishes. Its URL is a secret: entered here, never shown again (only its host). */
export function NotificationsSettings() {
  const t = useT();
  const client = useQueryClient();
  const webhook = useWebhook();
  const [url, setUrl] = useState("");
  const shown = (w: Webhook) => client.setQueryData(keys.webhook(), w);
  const save = useMutation({
    mutationFn: () => api.admin.setWebhook(url.trim()),
    onSuccess: (w) => {
      shown(w);
      setUrl("");
    },
  });
  const remove = useMutation({ mutationFn: () => api.admin.deleteWebhook(), onSuccess: shown });
  return (
    <Card p="lg">
      <PanelHeader title={t("settings.notifications")} description={t("notifications.help")} />
      {webhook.isPending ? (
        <Loading />
      ) : webhook.isError ? (
        <ErrorMessage error={webhook.error} />
      ) : (
        <Stack gap="md">
          <Group gap="xs">
            <StateBadge tone={webhook.data.set ? "success" : "neutral"}>
              {webhook.data.set ? t("notifications.set", { host: webhook.data.host ?? "" }) : t("notifications.unset")}
            </StateBadge>
            {webhook.data.set && (
              <Button size="xs" variant="default" onClick={() => remove.mutate()} disabled={remove.isPending}>
                {t("notifications.remove")}
              </Button>
            )}
          </Group>
          {webhook.data.problem && (
            <Text size="sm" c="var(--color-warning-text)">
              {t("notifications.problem", { reason: webhook.data.problem })}
            </Text>
          )}
          <form
            aria-label={t("notifications.url")}
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <Group align="flex-end">
              <PasswordInput
                label={t("notifications.url")}
                description={t("notifications.urlHelp")}
                placeholder={webhook.data.set ? MASKED_SECRET : undefined}
                value={url}
                onChange={(e) => setUrl(e.currentTarget.value)}
                style={{ flex: 1 }}
              />
              <Button type="submit" disabled={save.isPending || !url.trim()}>
                {t("common.save")}
              </Button>
            </Group>
          </form>
          {(save.error ?? remove.error) && <ErrorMessage error={save.error ?? remove.error} />}
        </Stack>
      )}
    </Card>
  );
}
