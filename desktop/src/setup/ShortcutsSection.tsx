import { Alert, Card, Stack, Text, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getHotkeys, updateHotkeys, type HotkeySettings } from "../api/client";
import { HotkeyInput } from "../hotkeys/HotkeyInput";
import { hotkeyErrorMessageKey, useHotkeyRecordingGuard } from "../hotkeys/useHotkeys";
import { syncHotkeys } from "../hotkeys/hotkeyRuntime";

export function ShortcutsSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const guard = useHotkeyRecordingGuard();
  const [rejected, setRejected] = useState<string[]>([]);
  const [error, setError] = useState("");

  const hotkeys = useQuery({ queryKey: ["hotkeys"], queryFn: getHotkeys });

  const save = useMutation({
    mutationFn: (settings: HotkeySettings) => updateHotkeys(settings),
    onSuccess: async (saved) => {
      setError("");
      queryClient.setQueryData(["hotkeys"], saved);
      setRejected(await syncHotkeys(saved));
    },
    onError: (cause) => setError(t(hotkeyErrorMessageKey(cause))),
  });

  const settings = hotkeys.data ?? { quick_run: "", commands: {} };

  return (
    <Stack gap="md">
      <Title order={3}>{t("hotkey.settingsTitle")}</Title>
      <Card withBorder radius="md" p="md">
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            {t("hotkey.settingsDescription")}
          </Text>
          <Text size="xs" c="dimmed">
            {t("hotkey.deviceLocal")}
          </Text>
          <HotkeyInput
            label={t("hotkey.label")}
            value={settings.quick_run}
            disabled={hotkeys.isLoading}
            error={error || undefined}
            onRecordingChange={guard}
            onChange={(accelerator) => save.mutate({ ...settings, quick_run: accelerator })}
          />
          {rejected.length > 0 ? (
            <Alert color="warning" title={t("hotkey.rejectedTitle")}>
              {t("hotkey.rejectedBody", { accelerators: rejected.join(", ") })}
            </Alert>
          ) : null}
        </Stack>
      </Card>
    </Stack>
  );
}
