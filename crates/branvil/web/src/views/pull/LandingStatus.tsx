import { Anchor, Badge, Card, Group, Progress, Stack, Text } from "@mantine/core";
import { Check, Cloud, GitBranch, GitMerge, Hourglass, ListChecks, ShieldCheck, X, type LucideIcon } from "lucide-react";
import type { Landing, LandingFirst, LandingNext, PullRequest, Repository, Unanswered } from "../../api/types";
import { useT, type MessageKey, type Translate } from "../../i18n";
import { useChecks, useLanding } from "../../query/hooks";
import { RunTime } from "./Checks";

const humanTurn = (first: LandingFirst) => first.next.startsWith("owner_") || first.next === "host_closed";
/** A departure the head or its author clears (a move, a draft, a close, an answer) sends the pull request back to collecting what it needs. */
const cleared = (code?: string) => !code || ["head_moved", "base_moved", "closed", "draft", "unanswered"].includes(code);

const REASONS = ["merged", "closed", "draft", "conflict", "gate_failed", "host_failed", "publish_stopped", "already_in_base", "head_moved", "base_moved", "empty_head", "publish_requirements", "head_checks", "approvals", "unanswered"] as const;
const reasonKey = (code: string): MessageKey => REASONS.includes(code as typeof REASONS[number]) ? `landing.reason.${code}` as MessageKey : "landing.reason.unknown";

/** What the departure left behind and how to rejoin, under its reason. */
function ReasonDetail({ landing }: { landing: Landing }) {
  const t = useT();
  const reason = landing.reason;
  if (!reason) return null;
  const recovery = reason.code === "gate_failed" ? "landing.retryGate" : reason.code === "host_failed" ? "landing.retryHost" : reason.code === "publish_stopped" ? "landing.publishAgain" : ["already_in_base", "empty_head"].includes(reason.code) ? "landing.closePull" : "landing.rejoin";
  return (
    <>
      {reason.code === "publish_stopped" && <Text size="sm">{reason.message}</Text>}
      {reason.jobs.length > 0 && <Text size="sm">{t("landing.failedJobs", { jobs: reason.jobs.join(", ") })}</Text>}
      {reason.files.length > 0 && <Text size="sm">{t("landing.conflicts", { files: reason.files.join(", ") })}</Text>}
      <Text size="sm" c="dimmed">{t(recovery)}</Text>
    </>
  );
}

/** The statements still waiting on the author, each a link to it on the page. */
function UnansweredStatements({ items }: { items: Unanswered[] }) {
  const t = useT();
  return (
    <>
      <Text size="sm" c="dimmed">{t("landing.unansweredHint")}</Text>
      <Stack gap={2} component="ul" m={0} pl="md">
        {items.map((u) => (
          <li key={u.id}>
            <Anchor href={`#${encodeURIComponent(u.id)}`} size="sm">{t(`landing.statement.${u.kind}`, { author: u.author })}</Anchor>
          </li>
        ))}
      </Stack>
    </>
  );
}

/** Required gate jobs completed on the pull request's head. */
function useGateDone(pull: PullRequest, required: string[], enabled: boolean): number {
  const checks = useChecks(pull.repository, pull.number, enabled);
  return required.filter((name) => checks.data?.head_sha === pull.head_sha && checks.data.checks.some((c) => c.name === `gate/${name}` && c.status === "completed")).length;
}

export type CheckState = "checking" | "check.missing" | "check.running" | "check.pathsSkipped" | "check.success" | "check.failure";
const satisfied = (state: CheckState) => state === "check.success" || state === "check.pathsSkipped";

/** Each required check on this head, as check_landable reads it: real success or a service path selection. */
export function useRequiredChecks(pull: PullRequest, required: string[], load = true): { name: string; state: CheckState }[] {
  const query = useChecks(pull.repository, pull.number, load && required.length > 0);
  const current = !query.isError && query.data?.head_sha === pull.head_sha ? query.data : undefined;
  return required.map((name) => {
    const c = current?.checks.find((check) => check.name === name);
    const state: CheckState = !current ? "checking" : !c ? "check.missing" : c.status !== "completed" ? "check.running" : c.conclusion === "success" ? "check.success" : c.conclusion === "skipped" && c.path_skip ? "check.pathsSkipped" : "check.failure";
    return { name, state };
  });
}

export const useMissingChecks = (pull: PullRequest, required: string[]): string[] =>
  useRequiredChecks(pull, required).filter((c) => !satisfied(c.state)).map((c) => c.name);

/** The stops on a pull request's way into its base; the host's only with an upstream. */
export type Stop = "conditions" | "queue" | "gate" | "host" | "merge" | "base";
/** At the current stop: Branvil (or the host) is working, a person has a turn, or the pull request left the queue there. */
export type Mark = "now" | "turn" | "failed";

const NEXT_STOP: Record<LandingNext, Stop> = {
  gate: "gate", publishing: "host", owner_publish: "host", host_checks: "host", host_closed: "host", owner_fix_auto: "host",
  owner_merge_host: "merge", auto_merge: "merge", owner_merge: "merge", following: "base", owner_fix_following: "base",
};
const REASON_STOP: Partial<Record<string, Stop>> = { conflict: "gate", gate_failed: "gate", already_in_base: "gate", host_failed: "host", publish_stopped: "host" };

/**
 * Where the pull request is on its way into the base. A queue entry for an
 * older head, or one that left for a reason the head itself clears, means the
 * pull request is collecting what it needs to join again.
 */
export function whereabouts(pull: PullRequest, repo: Repository, personNeeded: boolean): { stops: Stop[]; at: Stop; mark: Mark; landing: Landing | null } {
  const stops: Stop[] = repo.upstream ? ["conditions", "queue", "gate", "host", "merge", "base"] : ["conditions", "queue", "gate", "merge", "base"];
  const l = pull.landing?.head_sha === pull.head_sha ? pull.landing : null;
  if (!l || (l.state === "left" && cleared(l.reason?.code))) return { stops, at: "conditions", mark: personNeeded ? "turn" : "now", landing: null };
  if (l.state === "left") return { stops, at: REASON_STOP[l.reason?.code ?? ""] ?? "conditions", mark: "failed", landing: l };
  if (l.position !== 1 || !l.first) return { stops, at: "queue", mark: "now", landing: l };
  const at = NEXT_STOP[l.first.next];
  return { stops, at: at === "host" && !repo.upstream ? "merge" : at, mark: humanTurn(l.first) ? "turn" : "now", landing: l };
}

/**
 * The pull request's whereabouts with what the page and the list say about
 * them. Without `load` (a list row past its budget) the checks are not read,
 * so only the draft state and approvals can be missing.
 */
function useJourney(pull: PullRequest, repo: Repository, load = true) {
  const t = useT();
  const checks = useRequiredChecks(pull, repo.required_checks, load);
  // What the head still lacks to join; a person must act on all but a check CI is still producing.
  const unmet = [
    ...(pull.draft ? [{ text: t("pull.draft"), person: true }] : []),
    ...(load ? checks : []).filter((c) => !satisfied(c.state)).map((c) => ({ text: `${c.name} · ${t(`landing.${c.state}`)}`, person: c.state === "check.failure" })),
    ...(pull.approvals < repo.required_approvals ? [{ text: t("merge.approvals", { count: pull.approvals, required: repo.required_approvals }), person: true }] : []),
    ...(pull.unanswered.length ? [{ text: t("landing.unanswered"), person: true }] : []),
  ];
  const where = whereabouts(pull, repo, unmet.some((u) => u.person));
  const first = where.landing?.position === 1 ? where.landing.first : null;
  const host = repo.upstream?.host ?? "";
  const name = (stop: Stop) => stop === "host" ? host : stop === "base" ? pull.base : stop === "gate" ? t("landing.checkpoint") : t(`landing.stop.${stop}`);
  return { ...where, unmet, first, host, name };
}

type Journey = ReturnType<typeof useJourney>;

/** The page headline: the current state in one sentence. */
const headline = (t: Translate, j: Journey, pull: PullRequest) =>
  j.mark === "failed" ? t("landing.leftWith", { reason: t(reasonKey(j.landing?.reason?.code ?? "")) })
  : j.at === "conditions" ? t(j.unmet.length ? "landing.missing" : "landing.notInQueue")
  : j.first ? t(`landing.next.${j.first.next}`, { host: j.host, base: pull.base })
  : t("landing.waitingAt", { position: j.landing?.position ?? "—" });

const ICONS: Record<Stop, LucideIcon> = { conditions: ListChecks, queue: Hourglass, gate: ShieldCheck, host: Cloud, merge: GitMerge, base: GitBranch };
const MARK_COLOR: Record<Mark, string> = { now: "var(--color-brand)", turn: "var(--color-warning-text)", failed: "var(--color-danger-text)" };

/** The pull request page's answer to "where is this pull request, and whose turn is it". */
export function LandingJourney({ pull, repo }: { pull: PullRequest; repo: Repository }) {
  const t = useT();
  const queue = useLanding(pull.repository);
  const j = useJourney(pull, repo);
  const { stops, at, mark, landing, first, unmet } = j;
  const gateDone = useGateDone(pull, repo.required_checks, first?.next === "gate");
  const behind = first ? (queue.data?.items.filter((e) => e.base === pull.base && e.state !== "left" && e.number !== pull.number).length ?? 0) : 0;
  const reached = stops.indexOf(at);
  const Icon = mark === "failed" ? X : ICONS[at];
  const required = repo.required_checks.length;
  return (
    <Card p="lg" component="section" aria-label={t("landing.journey", { base: pull.base })}>
      <Stack gap="md">
        <Group gap="xs" wrap="nowrap">
          <Icon size={18} color={MARK_COLOR[mark]} aria-hidden="true" />
          <Text fw={700} c={mark === "now" ? undefined : MARK_COLOR[mark]}>{headline(t, j, pull)}</Text>
        </Group>
        <ol className="journey">
          {stops.map((stop, i) => {
            const state = i < reached ? "done" : i === reached ? mark : "todo";
            const StopIcon = state === "done" ? Check : state === "failed" ? X : ICONS[stop];
            return (
              <li key={stop} className="journey-stop" data-stop={stop} data-state={state} aria-current={i === reached ? "step" : undefined}>
                <span className="journey-dot" aria-hidden="true">{stop === "queue" && i === reached ? landing?.position : <StopIcon size={14} />}</span>
                <span>{j.name(stop)}</span>
              </li>
            );
          })}
        </ol>
        <Stack gap={6} style={{ overflowWrap: "anywhere" }}>
          {at === "conditions" && unmet.length > 0 && (
            <Group gap={6}>
              {unmet.map((u) => (
                <Badge key={u.text} color={u.person ? "warning" : "neutral"} variant="light" size="sm" h="auto" styles={{ label: { whiteSpace: "normal", overflowWrap: "anywhere" } }}>
                  {u.text}
                </Badge>
              ))}
            </Group>
          )}
          {pull.unanswered.length > 0 && <UnansweredStatements items={pull.unanswered} />}
          {mark === "failed" && landing ? (
            <ReasonDetail landing={landing} />
          ) : at === "conditions" ? (
            <Text size="sm" c="dimmed">{t(unmet.length ? "landing.notQueued" : "landing.headReady")}</Text>
          ) : !first ? (
            landing?.first && <Text size="sm" c="dimmed">{t("landing.waitFor", { first: landing.first.number })}</Text>
          ) : (
            <>
              {first.next === "gate" && (
                <Group gap="xs" wrap="nowrap">
                  <Progress value={required ? (gateDone / required) * 100 : 0} size="sm" w={120} aria-hidden="true" />
                  <Text size="sm">{gateDone}/{required}</Text>
                </Group>
              )}
              {first.next_detail && <Text size="sm">{first.next_detail}</Text>}
              {["owner_merge_host", "host_closed"].includes(first.next) && first.host_url && (
                <Anchor href={first.host_url} target="_blank" rel="noopener noreferrer" size="sm">{t("landing.action")}</Anchor>
              )}
              {behind > 0 && <Text size="sm" c="dimmed">{t("landing.behind", { count: behind })}</Text>}
              <Text size="xs" c="dimmed">
                {t("landing.elapsed")}{" "}
                <RunTime startedAt={first.state_at} />
              </Text>
            </>
          )}
        </Stack>
      </Stack>
    </Card>
  );
}

const MARK_TONE: Record<Mark, string> = { now: "neutral", turn: "warning", failed: "danger" };

/**
 * The pull request list's word for the same whereabouts; its title says the
 * rest (every missing requirement, the departure reason, who goes first).
 */
export function JourneyBadge({ pull, repo, load }: { pull: PullRequest; repo: Repository; load: boolean }) {
  const t = useT();
  const j = useJourney(pull, repo, load);
  const { at, mark, landing, first, unmet } = j;
  const [text, title] =
    mark === "failed" ? [t("landing.leftAt", { stop: j.name(at) }), t(reasonKey(landing?.reason?.code ?? ""))]
    : at === "conditions" ? [unmet[0] ? t("landing.blockedBy", { what: unmet.length > 1 ? `${unmet[0].text} +${unmet.length - 1}` : unmet[0].text }) : t("landing.notInQueue"), unmet.map((u) => u.text).join(" / ") || t("landing.headReady")]
    : first ? [t(`landing.badge.${first.next}`, { host: j.host, base: pull.base }), headline(t, j, pull)]
    : [t("landing.position", { position: landing?.position ?? "—" }), landing?.first ? t("landing.waitFor", { first: landing.first.number }) : ""];
  return (
    <Badge color={MARK_TONE[mark]} variant="light" size="sm" title={title} data-stop={at} data-state={mark}>
      {pull.base === repo.default_branch ? text : `${pull.base} · ${text}`}
    </Badge>
  );
}

export function gatePassed(pull: PullRequest): boolean {
  const l = pull.landing;
  return l?.state === "passed" && l.position === 1 && l.base_sha === pull.base_sha && l.head_sha === pull.head_sha;
}
