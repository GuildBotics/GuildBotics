import { Text } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getHotkeys, updateHotkeys, type HotkeySettings } from "../api/client";
import { HotkeyInput } from "../hotkeys/HotkeyInput";
import { syncHotkeys } from "../hotkeys/hotkeyRuntime";
import { hotkeyErrorMessageKey, useHotkeyRecordingGuard } from "../hotkeys/useHotkeys";

export type CommandHotkeyFieldProps = {
  /** Logical command name, or null while no command is selected. */
  command: string | null;
};

/**
 * Hotkey assignment for one command.
 *
 * Assignments live in `hotkeys.yml`, not in the command file, so this field
 * saves on its own the moment a combination is recorded — it deliberately does
 * not take part in the editor's save button.
 */
export function CommandHotkeyField(props: CommandHotkeyFieldProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const guard = useHotkeyRecordingGuard();
  const [error, setError] = useState("");

  const hotkeys = useQuery({ queryKey: ["hotkeys"], queryFn: getHotkeys });
  const save = useMutation({
    mutationFn: (settings: HotkeySettings) => updateHotkeys(settings),
    onSuccess: async (saved) => {
      setError("");
      queryClient.setQueryData(["hotkeys"], saved);
      await syncHotkeys(saved);
    },
    onError: (cause) => setError(t(hotkeyErrorMessageKey(cause))),
  });

  if (!props.command) {
    return null;
  }
  const settings = hotkeys.data ?? { quick_run: "", commands: {} };
  const value = settings.commands[props.command] ?? "";

  const assign = (accelerator: string) => {
    const commands = { ...settings.commands };
    if (accelerator) {
      commands[props.command as string] = accelerator;
    } else {
      delete commands[props.command as string];
    }
    save.mutate({ ...settings, commands });
  };

  return (
    <div className="command-hotkey-field">
      <HotkeyInput
        label={t("hotkey.commandLabel")}
        value={value}
        disabled={hotkeys.isLoading}
        error={error || undefined}
        onRecordingChange={guard}
        onChange={assign}
      />
      <Text size="xs" c="dimmed" className="command-hotkey-help">
        {t("hotkey.commandDescription")}
      </Text>
    </div>
  );
}
