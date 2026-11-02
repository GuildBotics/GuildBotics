// Warnings at the top of every page, as GuildBotics' system alerts: only when
// something needs the owner (the page lost its connection to the service; CI
// cannot run, or waits for the service to stop, also when it runs on another
// machine; an upstream cannot be followed). A dismissed warning stays away
// until what it says changes; the lost connection cannot be dismissed, as it
// goes away by itself once the page is up to date again.

import { Alert, Anchor, Stack, Text } from "@mantine/core";
import { TriangleAlert } from "lucide-react";
import { useState } from "react";
import { useT } from "../i18n";
import { useDiagnostics } from "../query/hooks";
import { Link } from "../router/Router";
import { href } from "../router/routes";

interface Warning {
  id: string;
  text: string;
  repository?: string;
  dismissible: boolean;
}

export function AlertBand({ streamLost }: { streamLost: boolean }) {
  const t = useT();
  const diagnostics = useDiagnostics();
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  const d = diagnostics.data;
  const warnings: Warning[] = [];
  if (streamLost) warnings.push({ id: "stream", text: t("alert.streamLost"), dismissible: false });
  const runner = d?.runner;
  if (runner && (runner.state === "stopping" || (runner.mode !== "off" && runner.state !== "ready"))) {
    const text =
      runner.state === "stopping"
        ? t("alert.stopping")
        : runner.state === "no_runner"
          ? t("alert.noRunner")
          : t("alert.executorUnavailable", { detail: runner.detail });
    warnings.push({ id: `runner:${runner.state}:${runner.detail}`, text, dismissible: true });
  }
  for (const p of d?.upstream?.problems ?? []) {
    warnings.push({
      id: `upstream:${p.repository}:${p.problem}`,
      text: t("alert.upstream", { repository: p.repository, problem: p.problem }),
      repository: p.repository,
      dismissible: true,
    });
  }
  const shown = warnings.filter((w) => !dismissed.has(w.id));
  if (shown.length === 0) return null;
  return (
    <Stack gap="xs" role="region" aria-label={t("alert.region")}>
      {shown.map((w) => (
        <Alert
          key={w.id}
          color="warning"
          icon={<TriangleAlert size={18} />}
          title={t("alert.title")}
          withCloseButton={w.dismissible}
          closeButtonLabel={t("alert.dismiss")}
          onClose={() => setDismissed(new Set([...dismissed, w.id]))}
        >
          <Text size="sm" span>
            {w.text}
          </Text>
          {w.repository && (
            <Anchor component={Link} to={href.settings()} size="sm" ml="md">
              {t("alert.openSettings")}
            </Anchor>
          )}
        </Alert>
      ))}
    </Stack>
  );
}
