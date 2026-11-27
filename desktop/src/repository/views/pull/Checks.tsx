// The checks of a pull request's head or of a commit: rows on the page, and one
// check's details, actions, and log in a drawer.

import {
  Alert,
  Anchor,
  Box,
  Button,
  Card,
  Drawer,
  Group,
  MultiSelect,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { ChevronRight, CircleCheck, CircleDashed, CircleX, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import * as api from "../../api/endpoints";
import type {
  Check,
  CheckSet,
  Job,
  PathSkip,
  PullRequest,
  ReusedFrom,
  RunnerMachineSnapshot,
  RunnerRecovery,
  RunnerSelection,
} from "../../api/types";
import { ErrorMessage, Loading, Sha, StateBadge, Time, type Tone } from "../../components/common";
import { useT } from "../../i18n";
import { resourceShortfallText } from "../../i18n/resourceShortfall";
import { useChecks, useCommitChecks, useJob, useJobLog, useRunnerLabel } from "../../query/hooks";
import { keys } from "../../query/keys";
import { Link, useFragmentTarget } from "../../router/Router";
import { href, POST_MERGE_FRAGMENT } from "../../router/routes";
import { CI_TONE, rollup, type CiRollup } from "../board/ordering";

export const TAIL_SIZES = [16 * 1024, 64 * 1024, 256 * 1024];

const FAILED = new Set(["failure", "cancelled", "timed_out", "startup_failure"]);

function toneOf(c: Pick<Check, "status" | "conclusion">): Tone {
  if (c.status !== "completed") return "warning";
  if (c.conclusion === "success") return "success";
  if (c.conclusion && FAILED.has(c.conclusion)) return "danger";
  return "neutral";
}

/** A paths reuse ran on a commit whose files outside the job's `paths` differ, so it is labeled apart. */
const reusedLabel = (r: ReusedFrom) => (r.basis === "paths" ? "ci.reusedPaths" : "ci.reused");

const DECISION_ROLLUP = {
  passed: "passing",
  failed: "failing",
  running: "pending",
} as const;

export function CheckStatusBadge({
  status,
  conclusion,
  path_skip,
}: Pick<Check, "status" | "conclusion" | "path_skip">) {
  const t = useT();
  const label =
    status === "completed" && conclusion === "skipped" && path_skip
      ? t(path_skip.reason === "post_merge" ? "ci.postMergeSkipped" : "ci.pathsSkipped")
      : status === "completed" && conclusion
        ? t(`ci.conclusion.${conclusion}`)
        : t(`ci.status.${status}`);
  return <StateBadge tone={toneOf({ status, conclusion })}>{label}</StateBadge>;
}

type PullCheckContext = { base: string; defaultBranch: string };

function PathSkipReason({
  repo,
  skip,
  context,
}: {
  repo: string;
  skip: PathSkip;
  context?: PullCheckContext;
}) {
  const t = useT();
  if (skip.reason === "post_merge")
    return (
      <Text size="xs" c="dimmed">
        {context
          ? t(
              context.base === context.defaultBranch
                ? "ci.postMergeDefaultReason"
                : "ci.postMergeCollectingReason",
              { branch: context.defaultBranch },
            )
          : t("ci.postMergeReason")}
      </Text>
    );
  return (
    <Stack gap={2}>
      <Text size="xs" c="dimmed">
        {t("ci.pathsReason", { paths: skip.paths.join(", ") })}
      </Text>
      {skip.bases.map((b) => (
        <Text key={b.pull} size="xs" c="dimmed">
          !{b.pull} · {b.base} <Sha repo={repo} value={b.base_sha} /> · merge-base{" "}
          {b.merge_base && <Sha repo={repo} value={b.merge_base} />}
        </Text>
      ))}
    </Stack>
  );
}

function StatusIcon({ check }: { check: Check }) {
  const tone = toneOf(check);
  if (tone === "success") return <CircleCheck size={16} color="var(--color-success)" />;
  if (tone === "danger") return <CircleX size={16} color="var(--color-danger)" />;
  return <CircleDashed size={16} color="var(--color-warning)" />;
}

/** Whole seconds in minutes and seconds ("3m 12s"). */
function useDuration() {
  const t = useT();
  return (secs: number) => {
    const s = Math.max(0, Math.floor(secs));
    return s < 60
      ? t("ci.seconds", { seconds: s })
      : t("ci.minutesSeconds", { minutes: Math.floor(s / 60), seconds: s % 60 });
  };
}

/** How long a job ran, or has run so far (counting each second from when it
 * is shown, so it is shown only once the job started), and while it runs what
 * its estimate leaves: the time remaining, or once that is past, the time the
 * job took before. */
export function RunTime({
  startedAt,
  finishedAt,
  expectedSecs,
}: {
  startedAt: string;
  finishedAt?: string | null;
  expectedSecs?: number;
}) {
  const t = useT();
  const duration = useDuration();
  const running = !finishedAt;
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const secs = ((finishedAt ? Date.parse(finishedAt) : now) - Date.parse(startedAt)) / 1000;
  const estimate =
    running && expectedSecs !== undefined
      ? expectedSecs > secs
        ? t("ci.remaining", { time: duration(expectedSecs - secs) })
        : t("ci.lastTime", { time: duration(expectedSecs) })
      : null;
  return (
    <Text span size="sm" c="dimmed">
      {duration(secs)}
      {estimate && ` · ${estimate}`}
    </Text>
  );
}

/** A check of the upstream host has an id but no Branvil attempt; its page is on the host. */
const isHosted = (c: Check) => !!c.attempt_id && c.attempt === undefined;

export function RunnerRecoveryLink({ repo, recovery }: { repo: string; recovery: RunnerRecovery }) {
  const t = useT();
  const label = useRunnerLabel();
  return (
    <Link to={href.job(repo, recovery.failed_attempt)}>
      {t("ci.runnerRecovery", {
        from: label(recovery.failed_runner),
        to: label(recovery.successful_runner),
      })}
    </Link>
  );
}

/** The checks of the pull request's head; a row opens its drawer. */
export function ChecksCard({
  repo,
  pull,
  context,
  onOpen,
}: {
  repo: string;
  pull: PullRequest;
  context?: PullCheckContext;
  onOpen: (check: Check) => void;
}) {
  const t = useT();
  const checks = useChecks(repo, pull.number);
  const behind = checks.data?.behind_by ?? 0;
  if (pull.merged) {
    const decision = checks.data?.merge_checks;
    const confirmation = checks.data?.post_merge_checks;
    const judged: Check[] =
      decision?.checks.map((check) => ({
        ...check,
        runner: null,
      })) ?? [];
    return (
      <>
        <CheckList
          repo={repo}
          title={t(
            decision?.source === "upstream_merge"
              ? "ci.upstreamMergeHeadChecks"
              : "ci.mergeDecision",
          )}
          query={checks}
          data={decision ? { checks: judged, checks_expected: false } : undefined}
          sha={decision?.head_sha}
          state={decision ? DECISION_ROLLUP[decision.required_checks] : undefined}
          empty={t(
            !decision
              ? "ci.mergeDecisionUnavailable"
              : decision.required_checks === "passed"
                ? "ci.mergeDecisionNoRequired"
                : "ci.mergeDecisionNoResults",
          )}
          requiredChecks={judged.map((check) => check.name)}
          context={context}
          onOpen={onOpen}
        >
          {decision?.source === "publication" && (
            <Text size="sm" c="dimmed" mb="xs">
              {t("ci.mergeDecisionAtPublication")}
            </Text>
          )}
          {decision?.source === "upstream_merge" && (
            <Text size="sm" c="dimmed" mb="xs">
              {t("ci.upstreamMergeHeadChecksReason")}
            </Text>
          )}
          {pull.upstream?.state === "merged" && pull.upstream.url && (
            <Anchor href={pull.upstream.url} target="_blank" rel="noopener noreferrer" size="sm">
              {t("ci.openOnHost")}
            </Anchor>
          )}
        </CheckList>
        <CheckList
          repo={repo}
          title={t("ci.postMergeConfirmation")}
          fragment={POST_MERGE_FRAGMENT}
          query={checks}
          data={confirmation ?? undefined}
          sha={confirmation?.sha}
          state={confirmation?.status ?? null}
          empty={confirmation ? undefined : t("ci.postMergeUnavailable")}
          context={context}
          onOpen={onOpen}
        >
          {confirmation && (
            <Group gap="xs" mb="xs">
              <Text size="sm" c="dimmed">
                {t(
                  confirmation.pulls.length ? "ci.postMergePullReason" : "ci.postMergeBranchReason",
                )}
              </Text>
              {confirmation.pulls.length ? (
                confirmation.pulls.map((number) => (
                  <Link key={number} to={href.pull(repo, number)}>
                    !{number}
                  </Link>
                ))
              ) : (
                <Link to={href.commit(repo, confirmation.sha)}>{confirmation.branch}</Link>
              )}
            </Group>
          )}
        </CheckList>
      </>
    );
  }
  const failure = checks.data?.base_failure;
  const failureTitle = failure ? t("pull.baseFailure", { base: failure.branch }) : undefined;
  return (
    <CheckList
      repo={repo}
      title={t("pull.checks")}
      query={checks}
      data={checks.data}
      sha={checks.data?.head_sha}
      requiredChecks={checks.data?.required_names}
      context={context}
      onOpen={onOpen}
    >
      {failure && (
        <Alert color="red" role="status" mb="xs" aria-label={failureTitle} title={failureTitle}>
          <Sha repo={repo} value={failure.sha} />
          <ul>
            {failure.failed_jobs.map((job) => (
              <li key={job.name}>
                {job.attempt_id ? (
                  <Link to={href.job(repo, job.attempt_id)}>{job.name}</Link>
                ) : (
                  job.name
                )}
              </li>
            ))}
          </ul>
          {failure.repair && (
            <Link to={href.ticket(repo, String(failure.repair.number))}>
              {t("landing.repairLink", { number: failure.repair.number })}
            </Link>
          )}
        </Alert>
      )}
      {!!checks.data?.conflicts?.length && (
        <Text size="sm" c="var(--color-danger-text)" mb="xs">
          {t("pull.conflicts", { files: checks.data.conflicts.join(", ") })}
        </Text>
      )}
      {pull.state === "open" && !pull.merged && behind > 0 && (
        <Text size="sm" c="dimmed" mb="xs">
          {t("pull.behindBy", { base: pull.base, count: behind })}
        </Text>
      )}
    </CheckList>
  );
}

/** The checks of a commit; a row opens its drawer. */
export function CommitChecksCard({
  repo,
  sha,
  title,
  embedded,
  onOpen,
}: {
  repo: string;
  sha: string;
  title: string;
  embedded?: boolean;
  onOpen: (check: Check) => void;
}) {
  const checks = useCommitChecks(repo, sha);
  return (
    <CheckList
      repo={repo}
      title={title}
      query={checks}
      data={checks.data}
      sha={checks.data?.sha}
      embedded={embedded}
      onOpen={onOpen}
    />
  );
}

function CheckList({
  repo,
  title,
  fragment,
  query,
  data,
  sha,
  embedded,
  requiredChecks = [],
  empty,
  state = data ? rollup(data) : undefined,
  context,
  onOpen,
  children,
}: {
  repo: string;
  title: string;
  fragment?: string;
  query: Pick<UseQueryResult<CheckSet>, "isPending" | "error">;
  data?: CheckSet;
  sha: string | undefined;
  embedded?: boolean;
  requiredChecks?: string[];
  empty?: string;
  state?: CiRollup | null;
  context?: PullCheckContext;
  onOpen: (check: Check) => void;
  children?: React.ReactNode;
}) {
  const t = useT();
  const target = useFragmentTarget(fragment ?? "");
  const Container = embedded ? Box : Card;
  return (
    <Container
      p={embedded ? 0 : "lg"}
      component="section"
      aria-label={title}
      id={fragment}
      {...(fragment ? target : undefined)}
    >
      <Group gap="xs" mb="xs">
        <Text fw={700}>{title}</Text>
        {state && !query.error && <StateBadge tone={CI_TONE[state]}>{t(`ci.${state}`)}</StateBadge>}
        {sha !== undefined && (
          <Text size="xs" c="dimmed" ml="auto">
            {t("pull.checksOn")} <Sha repo={repo} value={sha} />
          </Text>
        )}
      </Group>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorMessage error={query.error} />
      ) : (
        <>
          {children}
          {!data?.checks.length ? (
            <Text size="sm" c="dimmed">
              {empty ?? t(data?.checks_expected ? "ci.notRegisteredHelp" : "ci.noneHelp")}
            </Text>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {data.checks.map((c) => (
                <li key={`${c.name}#${c.attempt_id ?? "pending"}`}>
                  <CheckRow
                    check={c}
                    repo={repo}
                    required={
                      requiredChecks.includes(c.name) ||
                      (c.name.startsWith("gate/") && requiredChecks.includes(c.name.slice(5)))
                    }
                    context={context}
                    onOpen={onOpen}
                  />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Container>
  );
}

function CheckRow({
  check,
  repo,
  required,
  context,
  onOpen,
}: {
  check: Check;
  repo: string;
  required: boolean;
  context?: PullCheckContext;
  onOpen: (check: Check) => void;
}) {
  const t = useT();
  const label = useRunnerLabel();
  const failed = check.status === "completed" && !!check.conclusion && FAILED.has(check.conclusion);
  // A failed attempt's reason (`details`) is shown in the row.
  const job = useJob(
    repo,
    check.attempt_id ?? "",
    !!check.attempt_id && !isHosted(check) && failed,
  );
  const reason = isHosted(check) ? check.details : (job.data?.details ?? check.details);
  const runner = check.runner ? `${t("ci.runner")}: ${label(check.runner)}` : undefined;
  const content = (
    <>
      <StatusIcon check={check} />
      <strong>{check.name}</strong>
      {required && <StateBadge tone="neutral">{t("ci.required")}</StateBadge>}
      {check.attempt !== undefined && (
        <Text span size="sm" c="dimmed">
          #{check.attempt}
        </Text>
      )}
      {runner && (
        <Text span size="sm" c="dimmed" style={{ overflowWrap: "anywhere" }}>
          {runner}
        </Text>
      )}
      <span style={{ marginLeft: "auto" }} />
      {check.started_at && (
        <RunTime
          startedAt={check.started_at}
          finishedAt={check.finished_at}
          expectedSecs={check.expected_secs}
        />
      )}
      <CheckStatusBadge
        status={check.status}
        conclusion={check.conclusion}
        path_skip={check.path_skip}
      />
      {check.attempt_id && <ChevronRight size={16} color="var(--mantine-color-dimmed)" />}
    </>
  );
  return (
    <>
      {check.attempt_id ? (
        <button
          type="button"
          className="check-row"
          onClick={() => onOpen(check)}
          aria-label={t("ci.open", { name: check.name })}
          aria-description={runner}
        >
          {content}
        </button>
      ) : (
        <div className="check-row" style={{ cursor: "default" }}>
          {content}
        </div>
      )}
      {reason && (
        <Text size="xs" c={failed ? "var(--color-danger-text)" : "dimmed"} ml={24} mb={4}>
          {t("ci.reason")}: {reason}
        </Text>
      )}
      {check.runner_recovery && (
        <Text size="xs" ml={24} mb={4}>
          <RunnerRecoveryLink repo={repo} recovery={check.runner_recovery} />
        </Text>
      )}
      {check.reused_from && (
        <Text size="xs" ml={24} mb={4}>
          <Link to={href.job(repo, check.reused_from.attempt_id)}>
            {t(reusedLabel(check.reused_from))}
          </Link>{" "}
          · <Sha repo={repo} value={check.reused_from.sha} />
        </Text>
      )}
      {check.path_skip && (
        <Box ml={24} mb={4}>
          <PathSkipReason repo={repo} skip={check.path_skip} context={context} />
        </Box>
      )}
    </>
  );
}

/** A bounded tail of a CI attempt's log; the user may ask for a larger tail. */
export function LogViewer({
  repo,
  attemptId,
  action,
}: {
  repo: string;
  attemptId: string;
  action?: React.ReactNode;
}) {
  const t = useT();
  const [tail, setTail] = useState(TAIL_SIZES[0] as number);
  const log = useJobLog(repo, attemptId, tail);
  return (
    <Stack gap="xs">
      <Group gap="xs">
        {action}
        <Select
          ml="auto"
          size="xs"
          w={140}
          aria-label={t("ci.tailSize")}
          value={String(tail)}
          onChange={(v) => v && setTail(Number(v))}
          allowDeselect={false}
          data={TAIL_SIZES.map((s) => ({
            value: String(s),
            label: t("ci.tail", { size: s / 1024 }),
          }))}
        />
      </Group>
      {log.isPending ? (
        <Loading />
      ) : log.isError ? (
        <ErrorMessage error={log.error} />
      ) : (
        <>
          {log.data.truncated && (
            <Text size="xs" c="dimmed">
              {t("ci.truncated", {
                shown: Math.min(tail, log.data.log_bytes),
                total: log.data.log_bytes,
              })}
            </Text>
          )}
          <pre className="log-text" tabIndex={0} aria-label={t("ci.log")}>
            {log.data.log || t("ci.emptyLog")}
          </pre>
        </>
      )}
    </Stack>
  );
}

function RetryDetails({ repo, job }: { repo: string; job: Job }) {
  const t = useT();
  const label = useRunnerLabel();
  const retry = job.retry_decision;
  if (!retry) return null;
  const { runner, exclude_runners } = retry.selection;
  const wait = job.retry_wait;
  return (
    <>
      <dt>{t("ci.retryRequestedBy")}</dt>
      <dd>
        {retry.requested_by ?? t("ci.automaticRetry")}
        {" · "}
        <Link to={href.job(repo, retry.source_attempt)}>{t("ci.previousAttempt")}</Link>
      </dd>
      {runner && (
        <>
          <dt>{t("ci.retryRunner")}</dt>
          <dd>{label(runner, true)}</dd>
        </>
      )}
      {!!exclude_runners?.length && (
        <>
          <dt>{t("ci.excludeRunners")}</dt>
          <dd>{exclude_runners.map((name) => label(name, true)).join(", ")}</dd>
        </>
      )}
      {wait && (
        <>
          <dt>{t("ci.reason")}</dt>
          <dd>
            {wait.reason === "prefer_other"
              ? t("ci.retryWait.prefer_other", {
                  name: label(retry.failed_runner ?? ""),
                  until: new Date(wait.until).toLocaleTimeString(),
                })
              : wait.reason === "resources"
                ? resourceShortfallText(t, label(runner ?? ""), wait)
                : t(`ci.retryWait.${wait.reason}`, { name: label(runner ?? "") })}
          </dd>
        </>
      )}
    </>
  );
}

function RetryControls({
  job,
  onRetry,
  pending,
}: {
  job: Job;
  onRetry: (selection: RunnerSelection) => void;
  pending: boolean;
}) {
  const t = useT();
  const label = useRunnerLabel();
  const [runner, setRunner] = useState<string | null>(null);
  const [excluded, setExcluded] = useState<string[]>([]);
  const options = job.retry_runners ?? [];
  const choices = Array.from(new Set([...options, ...excluded, ...(runner ? [runner] : [])])).map(
    (name) => ({ value: name, label: label(name, true), disabled: !options.includes(name) }),
  );
  return (
    <>
      {choices.length > 0 && (
        <>
          <Select
            size="xs"
            aria-label={t("ci.retryRunner")}
            placeholder={t("ci.retryRunner")}
            data={choices.filter(({ value }) => !excluded.includes(value))}
            value={runner}
            onChange={setRunner}
            clearable
          />
          <MultiSelect
            size="xs"
            aria-label={t("ci.excludeRunners")}
            placeholder={t("ci.excludeRunners")}
            data={choices.filter(({ value }) => value !== runner)}
            value={excluded}
            onChange={setExcluded}
            clearable
          />
        </>
      )}
      <Button
        size="xs"
        variant="default"
        onClick={() => onRetry({ runner: runner ?? undefined, exclude_runners: excluded })}
        disabled={pending || job.retry_supported === false || job.status !== "completed"}
      >
        {t("ci.retry")}
      </Button>
    </>
  );
}

function RunnerMachine({ machine }: { machine: RunnerMachineSnapshot | null }) {
  const t = useT();
  if (!machine) return <div>{t("ci.runnerMachineUnknown")}</div>;
  return (
    <div>
      {t("ci.runnerMachine", {
        cpus: machine.logical_cpus,
        available: machine.available_memory_mib,
        total: machine.total_memory_mib,
      })}
      {machine.commit_available_mib != null &&
        " · " + t("ci.runnerCommitAvailable", { memory: machine.commit_available_mib })}
    </div>
  );
}

function RunnerClaimDetails({ job }: { job: Job }) {
  const t = useT();
  const claim = job.runner_claim;
  if (!claim) return null;
  const { machine, budget, memory_samples: samples } = claim;
  const allocation = job.allocation;
  return (
    <>
      <dt>{t("ci.runnerClaim")}</dt>
      <dd>
        <RunnerMachine machine={machine} />
        {allocation ? (
          <>
            <div>
              {t("ci.claimAllocation", {
                cpus: allocation.cpus,
                memory: allocation.vm_memory_mib,
              })}
            </div>
            <div>
              {samples
                ? t("ci.claimEstimateSamples", { memory: allocation.reserved_memory_mib, samples })
                : t("ci.claimEstimateFallback", { memory: allocation.reserved_memory_mib })}
            </div>
          </>
        ) : (
          <div>{t("ci.claimReuse")}</div>
        )}
        {budget && (
          <div>
            {t("ci.claimBudget", {
              reserve: budget.memory_reserve_mib,
              cpus: budget.remaining_cpus,
              memory: budget.remaining_memory_mib,
            })}
            {budget.remaining_commit_mib != null &&
              " · " + t("ci.claimCommit", { memory: budget.remaining_commit_mib })}
          </div>
        )}
      </dd>
    </>
  );
}

/** One attempt of a Branvil job: status, commit, times, reason, artifacts, retry/cancel, log. */
function JobDetails({
  repo,
  attemptId,
  context,
  onAttempt,
}: {
  repo: string;
  attemptId: string;
  context?: PullCheckContext;
  onAttempt: (id: string) => void;
}) {
  const t = useT();
  const label = useRunnerLabel();
  const client = useQueryClient();
  const job = useJob(repo, attemptId);
  const settle = () => {
    void client.invalidateQueries({ queryKey: keys.job(repo, attemptId) });
    void client.invalidateQueries({ queryKey: ["repo", repo, "pull"] });
    if (job.data)
      void client.invalidateQueries({ queryKey: keys.commitChecks(repo, job.data.sha) });
  };
  const retry = useMutation({
    mutationFn: (selection: RunnerSelection) => api.jobs.retry(repo, attemptId, selection),
    onSuccess: (j) => onAttempt(j.id),
    onSettled: settle,
  });
  const cancel = useMutation({
    mutationFn: () => api.jobs.cancel(repo, attemptId),
    onSettled: settle,
  });
  if (job.isPending) return <Loading />;
  if (job.isError) return <ErrorMessage error={job.error} />;
  const j = job.data;
  return (
    <Stack gap="md">
      {j.path_skip && <PathSkipReason repo={repo} skip={j.path_skip} context={context} />}
      <dl>
        <dt>{t("ci.commit")}</dt>
        <dd>
          <Sha repo={repo} value={j.sha} />
        </dd>
        {j.runner && (
          <>
            <dt>{t("ci.runner")}</dt>
            <dd>{label(j.runner, true)}</dd>
          </>
        )}
        <RunnerClaimDetails job={j} />
        <dt>{t("ci.started")}</dt>
        <dd>{j.started_at ? <Time value={j.started_at} /> : "—"}</dd>
        <dt>{t("ci.finished")}</dt>
        <dd>{j.finished_at ? <Time value={j.finished_at} /> : "—"}</dd>
        <dt>{t("ci.duration")}</dt>
        <dd>
          {j.started_at ? <RunTime startedAt={j.started_at} finishedAt={j.finished_at} /> : "—"}
        </dd>
        {j.reused_from && (
          <>
            <dt>{t(reusedLabel(j.reused_from))}</dt>
            <dd>
              <Link to={href.job(repo, j.reused_from.attempt_id)}>{j.reused_from.attempt_id}</Link>{" "}
              · <Sha repo={repo} value={j.reused_from.sha} />
            </dd>
          </>
        )}
        {j.details && (
          <>
            <dt>{t("ci.reason")}</dt>
            <dd>{j.details}</dd>
          </>
        )}
        <RetryDetails repo={repo} job={j} />
        {Object.entries(j.runner_deferrals ?? {}).map(([name, wait]) => (
          <div key={name}>
            <dt>{t("ci.runnerDeferral")}</dt>
            <dd>
              <Time value={wait.at} />
              {" · "}
              {resourceShortfallText(t, label(name), wait)}
              <RunnerMachine machine={wait.machine} />
            </dd>
          </div>
        ))}
        {j.runner_recovery && (
          <>
            <dt>{t("ci.runnerChange")}</dt>
            <dd>
              <RunnerRecoveryLink repo={repo} recovery={j.runner_recovery} />
            </dd>
          </>
        )}
        <dt>{t("ci.artifacts")}</dt>
        <dd>
          {j.artifact_names.length === 0 ? (
            <Text span size="sm" c="dimmed">
              {t("ci.noArtifacts")}
            </Text>
          ) : (
            <Group gap="sm">
              {j.artifact_names.map((n) => (
                <Anchor key={n} href={api.jobs.artifactUrl(repo, j.sha, n)} download size="sm">
                  {n}
                </Anchor>
              ))}
            </Group>
          )}
        </dd>
      </dl>
      {(retry.isError || cancel.isError) && <ErrorMessage error={retry.error ?? cancel.error} />}
      <LogViewer
        repo={repo}
        attemptId={attemptId}
        action={
          <>
            <RetryControls
              key={attemptId}
              job={j}
              onRetry={retry.mutate}
              pending={retry.isPending}
            />
            <Button
              size="xs"
              variant="default"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending || j.status === "completed"}
            >
              {t("ci.cancel")}
            </Button>
          </>
        }
      />
    </Stack>
  );
}

/** The drawer of one check: a Branvil job, or a check of the upstream host. */
export function CheckDrawer({
  repo,
  check,
  context,
  onClose,
}: {
  repo: string;
  context?: PullCheckContext;
  check: { attemptId: string; hosted: Check | null } | null;
  onClose: () => void;
}) {
  const t = useT();
  const [attempt, setAttempt] = useState<string | null>(null);
  const attemptId = attempt ?? check?.attemptId ?? "";
  const job = useJob(repo, attemptId, check !== null && check.hosted === null);
  const hosted = check?.hosted ?? null;
  const close = () => {
    setAttempt(null);
    onClose();
  };
  const title = hosted ? (
    <Group gap="xs">
      {hosted.name}
      <CheckStatusBadge status={hosted.status} conclusion={hosted.conclusion} />
    </Group>
  ) : job.data ? (
    <Group gap="xs">
      {job.data.job}
      <Text span c="dimmed">
        #{job.data.attempt}
      </Text>
      <CheckStatusBadge
        status={job.data.status}
        conclusion={job.data.conclusion}
        path_skip={job.data.path_skip}
      />
    </Group>
  ) : (
    t("pull.checks")
  );
  return (
    <Drawer
      opened={check !== null}
      onClose={close}
      position="right"
      size={780}
      title={title}
      closeButtonProps={{ "aria-label": t("common.close") }}
    >
      {check &&
        (hosted ? (
          <Stack gap="md">
            {hosted.details && (
              <Text size="sm">
                {t("ci.reason")}: {hosted.details}
              </Text>
            )}
            <LogViewer
              repo={repo}
              attemptId={check.attemptId}
              action={
                hosted.details_url && (
                  <Anchor
                    href={hosted.details_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    size="sm"
                  >
                    <Group gap={4}>
                      {t("ci.openOnHost")}
                      <ExternalLink size={14} />
                    </Group>
                  </Anchor>
                )
              }
            />
          </Stack>
        ) : (
          <JobDetails repo={repo} attemptId={attemptId} context={context} onAttempt={setAttempt} />
        ))}
    </Drawer>
  );
}

/** What a check row asks the drawer to show. */
export const drawerTarget = (c: Check) => ({
  attemptId: c.attempt_id ?? "",
  hosted: isHosted(c) ? c : null,
});
