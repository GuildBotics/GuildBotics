import { Alert, Anchor, Text } from "@mantine/core";
import { Fragment, useState, type ReactNode } from "react";

export interface HubVersion {
  sha: string;
  subject: string;
  url: string | null;
}
export interface HubStatus {
  current: HubVersion | null;
  target: HubVersion | null;
  operation: null | {
    kind: "update" | "restart" | "stop";
    phase: "preparing" | "draining";
    since: string;
    waiting: { runs: number; ci_jobs: number | null };
  };
}
export function HubVersionLabel({ version }: { version: HubVersion }) {
  const sha = version.sha.slice(0, 8);
  return (
    <>
      {version.url ? <Anchor href={version.url}>{sha}</Anchor> : sha} {version.subject}
    </>
  );
}
export function HubStatusBand({
  status,
  t,
}: {
  status?: HubStatus | null;
  t: (
    key: `hub.${"dismiss" | "title" | "preparing" | "preparingVersions" | "update" | "updateVersions" | "restart" | "stop"}`,
    params?: Record<string, string | number>,
  ) => string;
}) {
  const [dismissed, setDismissed] = useState<string | null>(null);
  const op = status?.operation;
  if (!op) return null;
  const id = `${op.kind}:${op.phase}:${op.since}`;
  if (dismissed === id) return null;
  const version = (v: HubVersion | null | undefined): ReactNode =>
    v ? <HubVersionLabel version={v} /> : null;
  const hasVersions = Boolean(status?.current && status?.target);
  const phaseKey = op.phase === "preparing" ? "preparing" : op.kind;
  const key =
    hasVersions && (phaseKey === "preparing" || phaseKey === "update")
      ? (`${phaseKey}Versions` as const)
      : phaseKey;
  const message = t(`hub.${key}`, {
    runs: op.waiting.runs,
    ci_jobs: op.waiting.ci_jobs ?? "?",
    current: "__HUB_CURRENT_VERSION__",
    target: "__HUB_TARGET_VERSION__",
  });
  return (
    <Alert
      color="warning"
      withCloseButton
      closeButtonLabel={t("hub.dismiss")}
      onClose={() => setDismissed(id)}
      title={t("hub.title")}
    >
      <Text span>
        {message.split(/(__HUB_CURRENT_VERSION__|__HUB_TARGET_VERSION__)/).map((part, index) => (
          <Fragment key={index}>
            {part === "__HUB_CURRENT_VERSION__"
              ? version(status?.current)
              : part === "__HUB_TARGET_VERSION__"
                ? version(status?.target)
                : part}
          </Fragment>
        ))}
      </Text>
      {!hasVersions && (status?.current || status?.target) ? (
        <Text component="div">
          {version(status.current)}
          {status.current && status.target ? " → " : null}
          {version(status.target)}
        </Text>
      ) : null}
    </Alert>
  );
}
