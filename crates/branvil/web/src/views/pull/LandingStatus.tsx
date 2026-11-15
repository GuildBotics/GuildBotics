import { Anchor, Badge, Card, Stack, Text } from "@mantine/core";
import type { PullRequest, Repository } from "../../api/types";
import { useT } from "../../i18n";
import { useChecks } from "../../query/hooks";

export type CheckState = "checking" | "check.missing" | "check.running" | "check.pathsSkipped" | "check.postMergeSkipped" | "check.success" | "check.failure";
const satisfied = (state: CheckState) => state === "check.success" || state === "check.pathsSkipped" || state === "check.postMergeSkipped";

/** Each required check on this head, as check_landable reads it: real success or a service path selection. */
export function useRequiredChecks(pull: PullRequest, required: string[], load = true): { name: string; state: CheckState }[] {
  const query = useChecks(pull.repository, pull.number, load && required.length > 0);
  const current = !query.isError && query.data?.head_sha === pull.head_sha ? query.data : undefined;
  return required.map((name) => {
    const c = current?.checks.find((check) => check.name === name);
    const state: CheckState = !current ? "checking" : !c ? "check.missing" : c.status !== "completed" ? "check.running" : c.conclusion === "success" ? "check.success" : c.conclusion === "skipped" && c.path_skip ? (c.path_skip.reason === "post_merge" ? "check.postMergeSkipped" : "check.pathsSkipped") : "check.failure";
    return { name, state };
  });
}

export const useMissingChecks = (pull: PullRequest, required: string[]): string[] =>
  useRequiredChecks(pull, required).filter((c) => !satisfied(c.state)).map((c) => c.name);

export type Stop = "conditions" | "host" | "merge" | "base";
export type Mark = "now" | "turn" | "failed";
export function whereabouts(pull: PullRequest, repo: Repository, personNeeded: boolean): { stops: Stop[]; at: Stop; mark: Mark } {
  const stops: Stop[] = repo.upstream ? ["conditions", "host", "merge", "base"] : ["conditions", "merge", "base"];
  const next = pull.landing?.next ?? "conditions";
  const at = next.includes("following") ? "base" : ["owner_merge_host", "owner_merge", "auto_merge"].includes(next) ? "merge" : next === "conditions" ? "conditions" : "host";
  return { stops, at, mark: next.startsWith("owner_fix") || next === "host_closed" ? "failed" : next.startsWith("owner_") || personNeeded ? "turn" : "now" };
}
export function LandingJourney({ pull, repo }: { pull: PullRequest; repo: Repository }) {
  const t = useT();
  const l = pull.landing;
  const { stops, at, mark } = whereabouts(pull, repo, pull.unanswered.length > 0);
  return <Card p="lg" component="section" aria-label={t("landing.journey", { base: pull.base })}><Stack gap="sm">
    <Text fw={700}>{!l || l.next === "conditions" ? t("landing.missing") : t(`landing.next.${l.next}`, { host: repo.upstream?.host ?? "", base: pull.base })}</Text>
    <ol className="journey">{stops.map(stop => <li key={stop} className="journey-stop" data-stop={stop} data-state={stop === at ? mark : "todo"} aria-current={stop === at ? "step" : undefined}>{stop === "host" ? repo.upstream?.host : stop === "base" ? pull.base : t(`landing.stop.${stop}`)}</li>)}</ol>
    {l?.blockers.map(b => <Text key={b.code} size="sm">{b.message}</Text>)}
    {pull.unanswered.map(u => <Anchor key={u.id} href={`#${encodeURIComponent(u.id)}`} size="sm">{t(`landing.statement.${u.kind}`, { author: u.author })}</Anchor>)}
    {l?.next_detail && <Text size="sm">{l.next_detail}</Text>}
    {l?.host_url && ["owner_merge_host", "host_closed"].includes(l.next) && <Anchor href={l.host_url} target="_blank" rel="noopener noreferrer">{t("landing.action")}</Anchor>}
  </Stack></Card>;
}
export function JourneyBadge({ pull, repo }: { pull: PullRequest; repo: Repository; load: boolean }) {
  const t = useT();
  const l = pull.landing;
  const { at, mark } = whereabouts(pull, repo, false);
  const text = !l || l.next === "conditions" ? (l?.blockers[0]?.message ?? t("landing.missing")) : t(`landing.badge.${l.next}`, { host: repo.upstream?.host ?? "", base: pull.base });
  return <Badge color={mark === "failed" ? "danger" : mark === "turn" ? "warning" : "neutral"} variant="light" size="sm" title={l?.blockers.map(b => b.message).join(" / ")} data-stop={at} data-state={mark}>{pull.base === repo.default_branch ? text : `${pull.base} · ${text}`}</Badge>;
}
