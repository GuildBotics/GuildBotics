import { ActionIcon, Tooltip } from "@mantine/core";
import { Copy } from "lucide-react";
import { useTranslation } from "react-i18next";

/** Copy action for a command file path. */
export function CommandPathActions({ path }: { path: string }) {
  const { t } = useTranslation();

  return (
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
  );
}
