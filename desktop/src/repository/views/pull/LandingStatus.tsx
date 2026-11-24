import { href } from "../../router/routes";
import { Anchor, Badge, Card, Group, Stack, Text } from "@mantine/core";
import { Check, Cloud, GitBranch, GitMerge, ListChecks, X } from "lucide-react";
import type { PullRequest, Repository } from "../../api/types";
import { useT, type MessageKey } from "../../i18n";
import { useChecks } from "../../query/hooks";

export type CheckState =
  | "checking"
  | "check.missing"
  | "check.running"
  | "check.pathsSkipped"
  | "check.postMergeSkipped"
  | "check.success"
  | "check.failure";
const satisfied = (state: CheckState) =>
  state === "check.success" || state === "check.pathsSkipped" || state === "check.postMergeSkipped";

/** Each required check on this head, as check_landable reads it: real success or a service path selection. */
export function useRequiredChecks(
  pull: PullRequest,
  required: string[],
  load = true,
): { name: string; state: CheckState }[] {
  const query = useChecks(pull.repository, pull.number, load && required.length > 0);
  const current = !query.isError && query.data?.head_sha === pull.head_sha ? query.data : undefined;
  return required.map((name) => {
    const c = current?.checks.find((check) => check.name === name);
    const state: CheckState = !current
      ? "checking"
      : !c
        ? "check.missing"
        : c.status !== "completed"
          ? "check.running"
          : c.conclusion === "success"
            ? "check.success"
            : c.conclusion === "skipped" && c.path_skip
              ? c.path_skip.reason === "post_merge"
                ? "check.postMergeSkipped"
                : "check.pathsSkipped"
              : "check.failure";
    return { name, state };
  });
}

export const useMissingChecks = (pull: PullRequest, required: string[]): string[] =>
  useRequiredChecks(pull, required)
    .filter((c) => !satisfied(c.state))
    .map((c) => c.name);

export type Stop = "conditions" | "host" | "merge" | "base";
export type Mark = "now" | "turn" | "failed";
export function whereabouts(
  pull: PullRequest,
  repo: Repository,
  personNeeded: boolean,
): { stops: Stop[]; at: Stop; mark: Mark } {
  const publishes = repo.upstream && pull.base === repo.default_branch;
  const stops: Stop[] = publishes
    ? ["conditions", "host", "merge", "base"]
    : ["conditions", "merge", "base"];
  const next = pull.landing?.next ?? "conditions";
  const at = next.includes("following")
    ? "base"
    : ["owner_merge_host", "owner_merge", "auto_merge"].includes(next)
      ? "merge"
      : next === "conditions"
        ? "conditions"
        : publishes
          ? "host"
          : "merge";
  return {
    stops,
    at,
    mark:
      next.startsWith("owner_fix") || next === "host_closed"
        ? "failed"
        : next.startsWith("owner_") || personNeeded
          ? "turn"
          : "now",
  };
}
const stopIcons = { conditions: ListChecks, host: Cloud, merge: GitMerge, base: GitBranch };
const markColors = {
  now: "var(--color-brand)",
  turn: "var(--color-warning-text)",
  failed: "var(--color-danger-text)",
};
const blockerName = (code: string) => `landing.blocker.${code}` as MessageKey;

function useBlockerDetails(pull: PullRequest, load = true) {
  const t = useT();
  const blockers = pull.landing?.blockers ?? [];
  const jobs = blockers.find((b) => b.code === "head_checks")?.jobs ?? [];
  const checks = useRequiredChecks(pull, jobs, load);
  return blockers.map((b) => {
    switch (b.code) {
      case "head_checks":
        return checks.length
          ? checks.map(
              (c) =>
                `${c.name} · ${t(c.state === "checking" || satisfied(c.state) ? "landing.check.checking" : `landing.${c.state}`)}`,
            )
          : [t("landing.detail.head_checks")];
      case "held":
        return b.held
          ? [
              t(b.held.assignee ? "landing.held" : "landing.heldUnassigned", {
                number: b.held.number,
                name: b.held.assignee ?? "",
              }),
            ]
          : [];
      case "base_checks_failed":
        return [b.jobs.join(", ")];
      case "required_removal":
        return b.jobs.map((name) => t("landing.requiredRemoval", { name }));
      case "approvals":
        return [t("merge.approvals", { count: b.count!, required: b.required! })];
      case "unanswered":
        return [t("landing.unanswered")];
      case "conflict":
        return [t("landing.detail.conflict", { files: b.files.join(", ") })];
      case "host_failed":
        return [t("landing.detail.host_failed", { jobs: b.jobs.join(", ") })];
      default:
        return [t(`landing.detail.${b.code}` as MessageKey)];
    }
  });
}

export function LandingJourney({ pull, repo }: { pull: PullRequest; repo: Repository }) {
  const t = useT();
  const l = pull.landing;
  const details = useBlockerDetails(pull);
  const { stops, at, mark } = whereabouts(pull, repo, pull.unanswered.length > 0);
  const HeadingIcon = mark === "failed" ? X : stopIcons[at];
  return (
    <Card p="lg" component="section" aria-label={t("landing.journey", { base: pull.base })}>
      <Stack gap="sm">
        <Group gap="xs">
          <HeadingIcon size={18} color={markColors[mark]} aria-hidden="true" />
          <Text fw={700} c={mark === "now" ? undefined : markColors[mark]}>
            {!l || l.next === "conditions"
              ? t("landing.missing")
              : t(`landing.next.${l.next}`, { host: repo.upstream?.host ?? "", base: pull.base })}
          </Text>
        </Group>
        <ol className="journey">
          {stops.map((stop, index) => {
            const state = index < stops.indexOf(at) ? "done" : stop === at ? mark : "todo";
            const Icon = state === "done" ? Check : state === "failed" ? X : stopIcons[stop];
            return (
              <li
                key={stop}
                className="journey-stop"
                data-stop={stop}
                data-state={state}
                aria-current={stop === at ? "step" : undefined}
              >
                <span className="journey-dot" aria-hidden="true">
                  <Icon size={16} />
                </span>
                {stop === "host"
                  ? repo.upstream?.host
                  : stop === "base"
                    ? pull.base
                    : t(`landing.stop.${stop}`)}
              </li>
            );
          })}
        </ol>
        {l?.blockers.map((b, i) => (
          <Stack key={b.code} gap={2}>
            <Text size="sm" fw={700}>
              {t(blockerName(b.code))}
            </Text>
            {details[i]?.map((text, j) => (
              <Text key={j} size="sm">
                {text}
              </Text>
            ))}
            {b.held && (
              <>
                <Anchor href={href.ticket(pull.repository, String(b.held.number))} size="sm">
                  {t("landing.repairLink", { number: b.held.number })}
                </Anchor>
                {b.code === "held" && l.host_url && (
                  <Text size="sm">
                    {t("landing.heldPublished", { host: repo.upstream?.host ?? "" })}
                  </Text>
                )}
              </>
            )}
          </Stack>
        ))}
        {pull.unanswered.map((u) => (
          <Anchor key={u.id} href={`#${encodeURIComponent(u.id)}`} size="sm">
            {t(`landing.statement.${u.kind}`, { author: u.author })}
          </Anchor>
        ))}
        {l?.next_detail && <Text size="sm">{l.next_detail}</Text>}
        {l?.host_url && ["owner_merge_host", "host_closed"].includes(l.next) && (
          <Anchor href={l.host_url} target="_blank" rel="noopener noreferrer">
            {t("landing.action")}
          </Anchor>
        )}
      </Stack>
    </Card>
  );
}
export function JourneyBadge({
  pull,
  repo,
  load,
}: {
  pull: PullRequest;
  repo: Repository;
  load: boolean;
}) {
  const t = useT();
  const l = pull.landing;
  const details = useBlockerDetails(pull, load);
  const { at, mark } = whereabouts(pull, repo, false);
  const text =
    !l || l.next === "conditions"
      ? l?.blockers.length
        ? `${t(blockerName(l.blockers[0]!.code))}${l.blockers.length > 1 ? ` +${l.blockers.length - 1}` : ""}`
        : t("landing.missing")
      : t(`landing.badge.${l.next}`, { host: repo.upstream?.host ?? "", base: pull.base });
  const color =
    l?.next === "conditions" && l.blockers[0]?.code === "held"
      ? "warning"
      : l?.next === "conditions"
        ? l.blockers.some((b) => b.code === "conflict" || b.code === "host_failed")
          ? "danger"
          : "neutral"
        : l && ["owner_publish", "owner_fix_auto", "owner_fix_following"].includes(l.next)
          ? "danger"
          : l && ["owner_merge_host", "owner_merge"].includes(l.next)
            ? "warning"
            : "neutral";
  return (
    <Badge
      color={color}
      variant="light"
      size="sm"
      title={details.flat().join(" / ")}
      data-stop={at}
      data-state={mark}
    >
      {pull.base === repo.default_branch ? text : `${pull.base} · ${text}`}
    </Badge>
  );
}
