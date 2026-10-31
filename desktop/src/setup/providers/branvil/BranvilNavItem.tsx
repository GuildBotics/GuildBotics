import { Text, UnstyledButton } from "@mantine/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";

import { getProjectConfig } from "../../../api/client";
import { openBranvil } from "./openBranvil";

/** The browser entry for workspaces whose saved code host or tickets use Branvil. */
export function BranvilNavItem() {
  const { t } = useTranslation();
  const config = useQuery({
    queryKey: ["project-config"],
    queryFn: getProjectConfig,
    retry: false,
  });
  const entry = useMutation({ mutationFn: openBranvil });
  const services = config.data?.services;
  if (
    services?.code_hosting_service?.name !== "branvil" &&
    services?.ticket_service?.name !== "branvil"
  ) {
    return null;
  }
  return (
    <>
      <UnstyledButton
        className="nav-item"
        title={t("setup.branvil.nav.openInBrowser")}
        disabled={entry.isPending}
        onClick={() => entry.mutate()}
      >
        <ExternalLink size={18} aria-hidden="true" />
        {t("setup.branvil.nav.label")}
      </UnstyledButton>
      {entry.isError ? (
        <Text size="xs" c="danger" role="alert">
          {entry.error.message}
        </Text>
      ) : null}
    </>
  );
}
