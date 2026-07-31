import { ActionIcon, Group, Tooltip } from "@mantine/core";
import { Copy, ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";

export type CommandPathActionsProps = {
  path: string;
  onOpenExternal?: () => void;
};

/** Copy / open-externally actions for a command file path. */
export function CommandPathActions({ path, onOpenExternal }: CommandPathActionsProps) {
  const { t } = useTranslation();

  return (
    <Group gap={4} wrap="nowrap">
      <Tooltip label={t("commands.copyScriptPath")}>
        <ActionIcon
          aria-label={t("commands.copyScriptPath")}
          size="sm"
          variant="subtle"
          disabled={!path}
          onClick={() => void navigator.clipboard?.writeText(path).catch(() => {})}
        >
          <Copy size={14} />
        </ActionIcon>
      </Tooltip>
      {onOpenExternal ? (
        <Tooltip label={t("commands.openScript")}>
          <ActionIcon
            aria-label={t("commands.openScript")}
            size="sm"
            variant="subtle"
            disabled={!path}
            onClick={onOpenExternal}
          >
            <ExternalLink size={14} />
          </ActionIcon>
        </Tooltip>
      ) : null}
    </Group>
  );
}
