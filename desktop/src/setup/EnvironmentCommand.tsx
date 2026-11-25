import { Button, Code, CopyButton, Group } from "@mantine/core";
import { Check, Copy } from "lucide-react";
import { useTranslation } from "react-i18next";

export function EnvironmentCommand({ command }: { command: string }) {
  const { t } = useTranslation();
  return (
    <Group gap="xs">
      <Code>{command}</Code>
      <CopyButton value={command}>
        {({ copied, copy }) => (
          <Button
            size="compact-xs"
            variant="subtle"
            leftSection={copied ? <Check size={12} /> : <Copy size={12} />}
            onClick={copy}
          >
            {copied
              ? t("setup.intelligence.environment.copied")
              : t("setup.intelligence.environment.copy")}
          </Button>
        )}
      </CopyButton>
    </Group>
  );
}
