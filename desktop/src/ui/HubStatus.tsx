import { Alert, Anchor, Text } from "@mantine/core";
import { Fragment, useState, type ReactNode } from "react";

export interface HubVersion {
  sha: string;
  subject: string;
  url: string | null;
}
export type HubUpdate =
  | {
      kind: "manual";
      sha: string;
      release: string;
      steps: { version: number; ticket: string }[];
      confirmed: boolean;
    }
  | { kind: "failed"; sha: string; stage: string; reason: string; next: string };
export interface HubStatus {
  current: (HubVersion & { tag: string | null }) | null;
  target: HubVersion | null;
  update?: HubUpdate | null;
  operation: null | {
    kind: "update" | "restart" | "stop";
    phase: "preparing" | "draining";
    since: string;
    waiting: { runs: number; ci_jobs: number | null };
  };
}
type HubTranslate = (
  key: `hub.${"versionLabel" | "dismiss" | "title" | "preparing" | "preparingVersions" | "update" | "updateVersions" | "restart" | "stop"}`,
  params?: Record<string, string | number>,
) => string;
function HubVersionLabel({ version, t }: { version: HubVersion; t: HubTranslate }) {
  const sha = version.sha.slice(0, 8);
  const label = t("hub.versionLabel", { sha: "__HUB_SHA__", subject: "__HUB_SUBJECT__" });
  return (
    <>
      {label.split(/(__HUB_SHA__|__HUB_SUBJECT__)/).map((part, index) => (
        <Fragment key={index}>
          {part === "__HUB_SHA__" ? (
            version.url ? (
              <Anchor href={version.url}>{sha}</Anchor>
            ) : (
              sha
            )
          ) : part === "__HUB_SUBJECT__" ? (
            version.subject
          ) : (
            part
          )}
        </Fragment>
      ))}
    </>
  );
}
export function HubStatusBand({ status, t }: { status?: HubStatus | null; t: HubTranslate }) {
  const [dismissed, setDismissed] = useState<string | null>(null);
  const op = status?.operation;
  if (!op) return null;
  const id = `${op.kind}:${op.phase}:${op.since}`;
  if (dismissed === id) return null;
  const version = (v: HubVersion | null | undefined): ReactNode =>
    v ? <HubVersionLabel version={v} t={t} /> : null;
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
