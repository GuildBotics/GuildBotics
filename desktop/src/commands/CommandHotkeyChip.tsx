import { Button, Popover, Stack, Text } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getHotkeys, updateHotkeys, type HotkeySettings } from "../api/client";
import { formatAccelerator, isMacPlatform } from "../hotkeys/accelerator";
import { HotkeyInput } from "../hotkeys/HotkeyInput";
import { syncHotkeys } from "../hotkeys/hotkeyRuntime";
import { hotkeyErrorMessageKey, useHotkeyRecordingGuard } from "../hotkeys/useHotkeys";

export type CommandHotkeyChipProps = {
  /** Logical command name, or null while no command is selected. */
  command: string | null;
};

/**
 * Hotkey assignment for one command, as a command-bar chip.
 *
 * Assignments live in `hotkeys.yml`, not in the command file, so this control
 * saves on its own the moment a combination is recorded — it deliberately does
 * not take part in the editor's save button. The recording UI matches the
 * global shortcut in Setup (description above the input) so the same control
 * behaves the same way in both places; only the entry point differs.
 */
export function CommandHotkeyChip(props: CommandHotkeyChipProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const guard = useHotkeyRecordingGuard();
  const [opened, setOpened] = useState(false);
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
    <Popover
      opened={opened}
      onChange={setOpened}
      position="bottom-end"
      shadow="md"
      width={300}
      withArrow
      // No focus trap: it would put the input straight into recording mode,
      // which hides the clear button and leaves no way to unassign by mouse.
      trapFocus={false}
    >
      <Popover.Target>
        <Button
          className="command-hotkey-chip"
          size="compact-xs"
          variant={value ? "light" : "subtle"}
          color="gray"
          aria-label={t("hotkey.commandLabel")}
          onClick={() => setOpened((current) => !current)}
        >
          {formatAccelerator(value, isMacPlatform()) || t("hotkey.unset")}
        </Button>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            {t("hotkey.commandDescription")}
          </Text>
          <HotkeyInput
            label={t("hotkey.commandLabel")}
            value={value}
            disabled={hotkeys.isLoading}
            error={error || undefined}
            onRecordingChange={guard}
            onChange={assign}
          />
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
