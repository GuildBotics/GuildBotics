import { Badge, Tooltip } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Server } from "lucide-react";
import { useTranslation } from "react-i18next";

import { getServerMachineName } from "./api/client";

/** Where the server machine's name is cached; a rename invalidates it. */
export const SERVER_MACHINE_QUERY_KEY = ["server-machine-name"];

/**
 * The name of the machine the server runs on. "This machine" on these screens
 * is that machine, which a browser on another one would otherwise take for
 * its own.
 */
export function useServerMachineName(): string | undefined {
  return useQuery({ queryKey: SERVER_MACHINE_QUERY_KEY, queryFn: getServerMachineName }).data;
}

/** The server machine as a text names it: its name, or a word for it until that is known. */
export function useServerMachineLabel(): string {
  const { t } = useTranslation();
  return useServerMachineName() || t("app.serverMachine.fallback");
}

/** A hint for a path field: the path names a file of the server machine. */
export function useServerPathHint(): string {
  const { t } = useTranslation();
  return t("app.serverMachine.pathHint", { machine: useServerMachineLabel() });
}

/** The server machine's name, beside a heading that says "this machine". */
export function ServerMachineBadge() {
  const { t } = useTranslation();
  const machine = useServerMachineName();
  if (!machine) {
    return null;
  }
  return (
    <Tooltip label={t("app.serverMachine.tooltip", { machine })}>
      <Badge
        color="gray"
        variant="light"
        size="sm"
        tt="none"
        leftSection={<Server size={12} />}
        aria-label={t("app.serverMachine.tooltip", { machine })}
      >
        {machine}
      </Badge>
    </Tooltip>
  );
}
