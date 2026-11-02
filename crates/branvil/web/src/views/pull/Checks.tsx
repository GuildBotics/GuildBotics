// The checks of a pull request's head or of a commit: rows on the page, and one
// check's details, actions, and log in a drawer.

import { Anchor, Box, Button, Card, Drawer, Group, Select, Stack, Text } from "@mantine/core";
import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { ChevronRight, CircleCheck, CircleDashed, CircleX, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import * as api from "../../api/endpoints";
import type { Check, CheckSet, PathSkip, PullRequest } from "../../api/types";
import { ErrorMessage, Loading, Sha, StateBadge, Time, type Tone } from "../../components/common";
import { useT } from "../../i18n";
import { useChecks, useCommitChecks, useJob, useJobLog } from "../../query/hooks";
import { keys } from "../../query/keys";
import { Link } from "../../router/Router";
import { href } from "../../router/routes";
import { rollup, type CiRollup } from "../board/ordering";

export const TAIL_SIZES = [16 * 1024, 64 * 1024, 256 * 1024];

const FAILED = new Set(["failure", "cancelled", "timed_out", "startup_failure"]);

function toneOf(c: Pick<Check, "status" | "conclusion">): Tone {
  if (c.status !== "completed") return "warning";
  if (c.conclusion === "success") return "success";
  if (c.conclusion && FAILED.has(c.conclusion)) return "danger";
  return "neutral";
}

const ROLLUP_TONE: Record<CiRollup, Tone> = { passing: "success", failing: "danger", pending: "warning", not_registered: "warning", none: "neutral" };

export function CheckStatusBadge({ status, conclusion, path_skip }: Pick<Check, "status" | "conclusion" | "path_skip">) {
  const t = useT();
  const label = status === "completed" && conclusion === "skipped" && path_skip ? t("ci.pathsSkipped") : status === "completed" && conclusion ? t(`ci.conclusion.${conclusion}`) : t(`ci.status.${status}`);
  return <StateBadge tone={toneOf({ status, conclusion })}>{label}</StateBadge>;
}

function PathSkipReason({ repo, skip }: { repo: string; skip: PathSkip }) {
  const t = useT();
  return <Stack gap={2}>
    <Text size="xs" c="dimmed">{t("ci.pathsReason", { paths: skip.paths.join(", ") })}</Text>
    {skip.bases.map((b) => <Text key={b.pull} size="xs" c="dimmed">
      !{b.pull} · {b.base} <Sha repo={repo} value={b.base_sha} /> · merge-base {b.merge_base && <Sha repo={repo} value={b.merge_base} />}
    </Text>)}
  </Stack>;
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
    return s < 60 ? t("ci.seconds", { seconds: s }) : t("ci.minutesSeconds", { minutes: Math.floor(s / 60), seconds: s % 60 });
  };
}

/** How long a job ran, or has run so far (counting each second from when it
 * is shown, so it is shown only once the job started), and while it runs what
 * its estimate leaves: the time remaining, or once that is past, the time the
 * job took before. */
export function RunTime({ startedAt, finishedAt, expectedSecs }: { startedAt: string; finishedAt?: string | null; expectedSecs?: number }) {
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

/** The checks of the pull request's head; a row opens its drawer. */
export function ChecksCard({ repo, pull, requiredChecks, onOpen }: { repo: string; pull: PullRequest; requiredChecks: string[]; onOpen: (check: Check) => void }) {
  const t = useT();
  const checks = useChecks(repo, pull.number);
  const behind = checks.data?.behind_by ?? 0;
  return (
    <CheckList repo={repo} title={t("pull.checks")} query={checks} sha={checks.data?.head_sha} requiredChecks={requiredChecks} onOpen={onOpen}>
      {pull.state === "open" && !pull.merged && behind > 0 && (
        <Text size="sm" c="dimmed" mb="xs">
          {t("pull.behindBy", { base: pull.base, count: behind })}
        </Text>
      )}
    </CheckList>
  );
}

/** The checks of a commit; a row opens its drawer. */
export function CommitChecksCard({ repo, sha, title, embedded, onOpen }: { repo: string; sha: string; title: string; embedded?: boolean; onOpen: (check: Check) => void }) {
  const checks = useCommitChecks(repo, sha);
  return <CheckList repo={repo} title={title} query={checks} sha={checks.data?.sha} embedded={embedded} onOpen={onOpen} />;
}

function CheckList({
  repo,
  title,
  query,
  sha,
  embedded,
  requiredChecks = [],
  onOpen,
  children,
}: {
  repo: string;
  title: string;
  query: UseQueryResult<CheckSet>;
  sha: string | undefined;
  embedded?: boolean;
  requiredChecks?: string[];
  onOpen: (check: Check) => void;
  children?: React.ReactNode;
}) {
  const t = useT();
  const Container = embedded ? Box : Card;
  return (
    <Container p={embedded ? 0 : "lg"} component="section" aria-label={title}>
      <Group gap="xs" mb="xs">
        <Text fw={700}>{title}</Text>
        {query.data && !query.isError && <StateBadge tone={ROLLUP_TONE[rollup(query.data)]}>{t(`ci.${rollup(query.data)}`)}</StateBadge>}
        {sha !== undefined && (
          <Text size="xs" c="dimmed" ml="auto">
            {t("pull.checksOn")}{" "}
            <Sha repo={repo} value={sha} />
          </Text>
        )}
      </Group>
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <ErrorMessage error={query.error} />
      ) : (
        <>
          {children}
          {query.data.checks.length === 0 ? (
            <Text size="sm" c="dimmed">
              {query.data.checks_expected ? t("ci.notRegisteredHelp") : t("ci.noneHelp")}
            </Text>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {query.data.checks.map((c) => (
                <li key={`${c.name}#${c.attempt_id ?? "pending"}`}>
                  <CheckRow check={c} repo={repo} required={requiredChecks.includes(c.name) || (c.name.startsWith("gate/") && requiredChecks.includes(c.name.slice(5)))} onOpen={onOpen} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Container>
  );
}

function CheckRow({ check, repo, required, onOpen }: { check: Check; repo: string; required: boolean; onOpen: (check: Check) => void }) {
  const t = useT();
  const failed = check.status === "completed" && !!check.conclusion && FAILED.has(check.conclusion);
  // A failed attempt's reason (`details`) is shown in the row.
  const job = useJob(repo, check.attempt_id ?? "", !!check.attempt_id && !isHosted(check) && failed);
  const reason = isHosted(check) ? check.details : (job.data?.details ?? check.details);
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
      <span style={{ marginLeft: "auto" }} />
      {check.started_at && <RunTime startedAt={check.started_at} finishedAt={check.finished_at} expectedSecs={check.expected_secs} />}
      <CheckStatusBadge status={check.status} conclusion={check.conclusion} path_skip={check.path_skip} />
      {check.attempt_id && <ChevronRight size={16} color="var(--mantine-color-dimmed)" />}
    </>
  );
  return (
    <>
      {check.attempt_id ? (
        <button type="button" className="check-row" onClick={() => onOpen(check)} aria-label={t("ci.open", { name: check.name })}>
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
      {check.reused_from && (
        <Text size="xs" ml={24} mb={4}>
          <Link to={href.job(repo, check.reused_from.attempt_id)}>
            {t("ci.reused")}
          </Link>{" "}· <Sha repo={repo} value={check.reused_from.sha} />
        </Text>
      )}
      {check.path_skip && <Box ml={24} mb={4}><PathSkipReason repo={repo} skip={check.path_skip} /></Box>}
      {check.cache_refresh && (
        <Text size="xs" ml={24} mb={4}>
          <Link to={href.job(repo, check.cache_refresh)}>{t("ci.cacheRefresh")}</Link>
        </Text>
      )}
    </>
  );
}

/** A bounded tail of a CI attempt's log; the user may ask for a larger tail. */
export function LogViewer({ repo, attemptId, action }: { repo: string; attemptId: string; action?: React.ReactNode }) {
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
          data={TAIL_SIZES.map((s) => ({ value: String(s), label: t("ci.tail", { size: s / 1024 }) }))}
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
              {t("ci.truncated", { shown: Math.min(tail, log.data.log_bytes), total: log.data.log_bytes })}
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

/** One attempt of a Branvil job: status, commit, times, reason, artifacts, retry/cancel, log. */
function JobDetails({ repo, attemptId, onAttempt }: { repo: string; attemptId: string; onAttempt: (id: string) => void }) {
  const t = useT();
  const client = useQueryClient();
  const job = useJob(repo, attemptId);
  const settle = () => {
    void client.invalidateQueries({ queryKey: keys.job(repo, attemptId) });
    void client.invalidateQueries({ queryKey: ["repo", repo, "pull"] });
    if (job.data) void client.invalidateQueries({ queryKey: keys.commitChecks(repo, job.data.sha) });
  };
  const retry = useMutation({ mutationFn: () => api.jobs.retry(repo, attemptId), onSuccess: (j) => onAttempt(j.id), onSettled: settle });
  const cancel = useMutation({ mutationFn: () => api.jobs.cancel(repo, attemptId), onSettled: settle });
  if (job.isPending) return <Loading />;
  if (job.isError) return <ErrorMessage error={job.error} />;
  const j = job.data;
  return (
    <Stack gap="md">
      {j.path_skip && <PathSkipReason repo={repo} skip={j.path_skip} />}
      {j.purpose === "cache_refresh" && (
        <Stack gap="xs">
          <Text fw={600}>{t("ci.cacheRefresh")}</Text>
          <Text size="sm" c="dimmed">{t("ci.cacheRefreshEffect")}</Text>
        </Stack>
      )}
      <dl>
        <dt>{t("ci.commit")}</dt>
        <dd>
          <Sha repo={repo} value={j.sha} />
        </dd>
        <dt>{t("ci.started")}</dt>
        <dd>{j.started_at ? <Time value={j.started_at} /> : "—"}</dd>
        <dt>{t("ci.finished")}</dt>
        <dd>{j.finished_at ? <Time value={j.finished_at} /> : "—"}</dd>
        <dt>{t("ci.duration")}</dt>
        <dd>{j.started_at ? <RunTime startedAt={j.started_at} finishedAt={j.finished_at} /> : "—"}</dd>
        {j.reused_from && (
          <>
            <dt>{t("ci.reused")}</dt>
            <dd>
              <Link to={href.job(repo, j.reused_from.attempt_id)}>
                {j.reused_from.attempt_id}
              </Link>{" "}· <Sha repo={repo} value={j.reused_from.sha} />
            </dd>
          </>
        )}
        {j.cache_refresh && (
          <>
            <dt>{t("ci.cacheRefresh")}</dt>
            <dd><Link to={href.job(repo, j.cache_refresh)}>{j.cache_refresh}</Link></dd>
          </>
        )}
        {j.details && (
          <>
            <dt>{t("ci.reason")}</dt>
            <dd>{j.details}</dd>
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
            <Button size="xs" variant="default" onClick={() => retry.mutate()} disabled={retry.isPending || j.purpose === "cache_refresh"}>
              {t("ci.retry")}
            </Button>
            <Button size="xs" variant="default" onClick={() => cancel.mutate()} disabled={cancel.isPending || j.status === "completed"}>
              {t("ci.cancel")}
            </Button>
          </>
        }
      />
    </Stack>
  );
}

/** The drawer of one check: a Branvil job, or a check of the upstream host. */
export function CheckDrawer({ repo, check, onClose }: { repo: string; check: { attemptId: string; hosted: Check | null } | null; onClose: () => void }) {
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
      <CheckStatusBadge status={job.data.status} conclusion={job.data.conclusion} path_skip={job.data.path_skip} />
    </Group>
  ) : (
    t("pull.checks")
  );
  return (
    <Drawer opened={check !== null} onClose={close} position="right" size={780} title={title} closeButtonProps={{ "aria-label": t("common.close") }}>
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
                  <Anchor href={hosted.details_url} target="_blank" rel="noopener noreferrer" size="sm">
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
          <JobDetails repo={repo} attemptId={attemptId} onAttempt={setAttempt} />
        ))}
    </Drawer>
  );
}

/** What a check row asks the drawer to show. */
export const drawerTarget = (c: Check) => ({ attemptId: c.attempt_id ?? "", hosted: isHosted(c) ? c : null });
