import { Button, Code, CopyButton, Group, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { SERVICE_KINDS, type ServiceKind } from "../../../api/client";
import type { ServiceUi } from "../types";
import { branvilKey, getGitConfig } from "./api";

/** The git configuration that lets the owner's own checkouts on this machine
 * reach Branvil: git asks GuildBotics for this machine's token instead of a
 * name and a password. */
function GitConfig() {
  const { t } = useTranslation();
  const config = useQuery({ queryKey: branvilKey("git_config"), queryFn: getGitConfig }).data
    ?.git_config;
  if (!config) {
    return null;
  }
  return (
    <Stack gap={4}>
      <Text size="sm" fw={500}>
        {t("setup.branvil.git.title")}
      </Text>
      <Text size="xs" c="dimmed">
        {t("setup.branvil.git.hint")}
      </Text>
      <Code block aria-label={t("setup.branvil.git.title")}>
        {config}
      </Code>
      <Group>
        <CopyButton value={config}>
          {({ copied, copy }) => (
            <Button size="compact-sm" variant="light" onClick={copy}>
              {t(copied ? "setup.branvil.git.copied" : "setup.branvil.git.copy")}
            </Button>
          )}
        </CopyButton>
      </Group>
    </Stack>
  );
}

function service(kind: ServiceKind): ServiceUi {
  const Fields: ServiceUi["Fields"] = ({ services }) =>
    SERVICE_KINDS.find((other) => services[other]?.name === "branvil") === kind ? (
      <GitConfig />
    ) : null;

  return { initial: () => ({}), errors: () => ({}), Fields };
}

export const branvilCodeHosting = service("code_hosting_service");
export const branvilTickets = service("ticket_service");
