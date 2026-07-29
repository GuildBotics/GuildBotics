import { ActionIcon, Group, Text, Tooltip } from "@mantine/core";
import { Copy, ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";

export type CommandFilePathBarProps = {
  path: string;
  onOpenExternal?: () => void;
};

export function CommandFilePathBar({ path, onOpenExternal }: CommandFilePathBarProps) {
  const { t } = useTranslation();

  return (
    <Group className="command-editor-path" gap="xs" justify="space-between" wrap="nowrap">
      <Text className="command-editor-path-text" size="sm" title={path} truncate="end">
        {path}
      </Text>
      <Group gap={4} wrap="nowrap">
        <Tooltip label={t("commands.copyScriptPath")}>
          <ActionIcon
            aria-label={t("commands.copyScriptPath")}
            size="sm"
            variant="subtle"
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
              onClick={onOpenExternal}
            >
              <ExternalLink size={14} />
            </ActionIcon>
          </Tooltip>
        ) : null}
      </Group>
    </Group>
  );
}
