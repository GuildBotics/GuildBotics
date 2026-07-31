import { Group, Select, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";

import type { CommandFileSummary } from "../api/client";
import { CommandHotkeyChip } from "./CommandHotkeyChip";
import { CommandPathActions } from "./CommandPathActions";
import type { SaveStatus } from "./commandEditorState";

export type CommandBarProps = {
  files: CommandFileSummary[];
  selectedFileId: string | null;
  onSelectFile: (fileId: string | null) => void;
  /** Logical command name of the open file, or null while none is loaded. */
  command: string | null;
  saveStatus: SaveStatus;
  /** Absolute path, used for the tooltip, copy and open-externally actions. */
  path: string;
  /** Short path shown in the bar, relative to the commands directory. */
  pathLabel: string;
  disabled?: boolean;
  onOpenExternal?: () => void;
};

/**
 * The one strip of chrome above the editor.
 *
 * Command selection, save state, file path, hotkey and the file actions all
 * live here so the editor frame starts immediately below the page title, the
 * way every other screen hands off to its content surface.
 */
export function CommandBar({
  files,
  selectedFileId,
  onSelectFile,
  command,
  saveStatus,
  path,
  pathLabel,
  disabled = false,
  onOpenExternal,
}: CommandBarProps) {
  const { t } = useTranslation();

  return (
    <Group className="command-bar" gap="xs" justify="space-between" wrap="nowrap">
      <Group className="command-bar-identity" gap="xs" wrap="nowrap">
        <span
          className="command-bar-status"
          data-status={saveStatus}
          title={t(`commands.saveState.${saveStatus}`)}
        >
          <span className="command-bar-status-dot" aria-hidden="true" />
          <Text size="xs" c="dimmed">
            {t(`commands.saveState.${saveStatus}`)}
          </Text>
        </span>
        <Select
          className="command-bar-select"
          aria-label={t("commands.editSelectLabel")}
          searchable
          variant="unstyled"
          allowDeselect={false}
          nothingFoundMessage={t("commands.noCommandOptions")}
          value={selectedFileId}
          disabled={disabled}
          onChange={onSelectFile}
          data={files.map((file) => ({
            value: file.id,
            label: `${file.label} (${file.command})`,
          }))}
        />
        {pathLabel ? (
          // The bar shows the path relative to the commands directory; the
          // absolute one stays available through the tooltip and the actions.
          <Text className="command-bar-path" size="xs" c="dimmed" title={path} truncate="end">
            {pathLabel}
          </Text>
        ) : null}
      </Group>
      <Group gap={4} wrap="nowrap">
        <CommandHotkeyChip command={command} />
        <CommandPathActions path={path} onOpenExternal={onOpenExternal} />
      </Group>
    </Group>
  );
}
