import { Group, Select, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";

import type { CommandFileSummary } from "../api/client";
import { CommandPathActions } from "./CommandPathActions";
import type { SaveStatus } from "./commandEditorState";

export type CommandBarProps = {
  files: CommandFileSummary[];
  selectedFileId: string | null;
  onSelectFile: (fileId: string | null) => void;
  saveStatus: SaveStatus;
  /** Absolute path, used for the tooltip and the copy action. */
  path: string;
  /** Short path shown in the bar, relative to the commands directory. */
  pathLabel: string;
  disabled?: boolean;
};

/**
 * The one strip of chrome above the editor.
 *
 * Command selection, save state, file path and the copy action all
 * live here so the editor frame starts immediately below the page title, the
 * way every other screen hands off to its content surface.
 */
export function CommandBar({
  files,
  selectedFileId,
  onSelectFile,
  saveStatus,
  path,
  pathLabel,
  disabled = false,
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
          {/* `span`: Text renders a <p> by default, which is invalid inside a <span>. */}
          <Text span size="xs" c="dimmed">
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
          // absolute one stays available through the tooltip and the copy action.
          <Text className="command-bar-path" size="xs" c="dimmed" title={path} truncate="end">
            {pathLabel}
          </Text>
        ) : null}
      </Group>
      <CommandPathActions path={path} />
    </Group>
  );
}
