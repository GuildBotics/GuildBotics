// How a pull request lands: merged here, or, in a repository with an upstream,
// published to the upstream host and merged there by people.

import { Anchor, Button, Card, Group, Stack, Text, Textarea, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CloudUpload, GitMerge, Pencil, Users } from "lucide-react";
import { useState } from "react";
import { isApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import type { PullRequest, Repository, Upstream } from "../../api/types";
import { ErrorMessage, errorText, Notice, Refusal, Sha, StateBadge } from "../../components/common";
import { useT, type Translate } from "../../i18n";
import { usePublishPreview } from "../../query/hooks";
import { keys } from "../../query/keys";
import { gatePassed, useMissingChecks } from "./LandingStatus";

function refusalOf(t: Translate, err: unknown, notReady: "merge.notMergeable" | "publish.notReady" | "landing.notReady"): string {
  if (isApiError(err, "head_moved")) return t("merge.headMoved");
  if (isApiError(err, "base_moved")) return t("merge.baseMoved");
  if (isApiError(err, "not_mergeable")) return t(notReady, { message: err.message });
  return errorText(err, t);
}

/** A repository without an upstream merges here (fast-forward). */
export function MergeCard({ pull, repo }: { pull: PullRequest; repo: Repository }) {
  const t = useT();
  const { required_checks: requiredChecks, required_approvals: requiredApprovals } = repo;
  const missing = useMissingChecks(pull, requiredChecks);
  const blocked = missing.length > 0 || pull.approvals < requiredApprovals || !gatePassed(pull);
  const client = useQueryClient();
  const merge = useMutation({
    mutationFn: () => api.pulls.merge(pull.repository, pull.number, pull.head_sha, pull.base_sha),
    onSuccess: (p) => client.setQueryData(keys.pull(pull.repository, pull.number), p),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.repo(pull.repository) });
      void client.invalidateQueries({ queryKey: ["board"] });
    },
  });
  return (
    <Card p="lg" component="section" aria-label={t("merge.title")}>
      <Stack gap="sm">
        <Group gap="xs">
          <GitMerge size={18} />
          <Text fw={700}>{t("merge.title")}</Text>
        </Group>
        <Text size="xs" c="dimmed">
          {t("merge.expected")} <Sha repo={pull.repository} value={pull.head_sha} /> / <Sha repo={pull.repository} value={pull.base_sha} />
        </Text>
        {merge.isError && <Refusal>{refusalOf(t, merge.error, "merge.notMergeable")}</Refusal>}
        {merge.isSuccess && <Notice>{t("merge.done")}</Notice>}
        {repo.auto_land && (
          <Text size="sm" c="dimmed">
            {t("merge.auto")}
          </Text>
        )}
        {pull.draft && (
          <Text size="sm" c="dimmed">
            {t("merge.draft")}
          </Text>
        )}
        <Group justify="flex-end">
          <Button leftSection={<GitMerge size={16} />} disabled={merge.isPending || pull.draft || !pull.head_sha || !pull.base_sha || blocked} onClick={() => merge.mutate()}>
            {merge.isPending ? t("merge.merging") : t("merge.submit")}
          </Button>
        </Group>
      </Stack>
    </Card>
  );
}

/** The host's copy of a published pull request: its link and state, and why sending stopped or failed. */
function PublishedState({ pull }: { pull: PullRequest }) {
  const t = useT();
  const x = pull.upstream;
  if (!x) return null;
  return (
    <Stack gap={6}>
      <Group gap="xs">
        {x.url ? (
          <Anchor href={x.url} target="_blank" rel="noopener noreferrer" size="sm">
            {x.repository}#{x.number}
          </Anchor>
        ) : (
          <Text size="sm" c="dimmed">
            {t("publish.noPullYet")}
          </Text>
        )}
        {x.state !== "none" && <StateBadge tone={x.state === "closed" ? "neutral" : "success"}>{t(`pull.state.${x.state}`)}</StateBadge>}
        <Text size="xs" c="dimmed">
          {t("publish.as", { account: x.account })}
        </Text>
        {x.auto && (
          <Text size="xs" c="dimmed">
            {t("publish.automatically")}
          </Text>
        )}
      </Group>
      {x.stopped && <Refusal>{t("publish.stopped", { reason: x.stopped })}</Refusal>}
      {x.error && (
        <Text size="sm" c="var(--color-warning-text)">
          {t("publish.failing", { reason: x.error })}
        </Text>
      )}
    </Stack>
  );
}

/**
 * In a repository with an upstream, pull requests are merged on the host:
 * this says why, shows exactly what publishing posts there and as whom, and
 * publishes.
 */
export function PublishCard({ pull, repo, upstream }: { pull: PullRequest; repo: Repository; upstream: Upstream }) {
  const t = useT();
  const { required_checks: requiredChecks, required_approvals: requiredApprovals } = repo;
  const missing = useMissingChecks(pull, requiredChecks);
  const headReady = missing.length === 0 && pull.approvals >= requiredApprovals;
  const blocked = !headReady || !gatePassed(pull);
  const client = useQueryClient();
  const host = upstream.host;
  const [editing, setEditing] = useState(false);
  // Published to this upstream: one published to an earlier upstream is published again here.
  const x = pull.upstream?.provider === upstream.provider && pull.upstream.repository === upstream.repository ? pull.upstream : null;
  const published = x !== null && x.number !== null && x.stopped === null;
  const preview = usePublishPreview(pull.repository, pull.number, !published);
  const publish = useMutation({
    mutationFn: () => api.pulls.publish(pull.repository, pull.number, pull.head_sha, pull.base_sha),
    onSuccess: (p) => client.setQueryData(keys.pull(pull.repository, pull.number), p),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.pull(pull.repository, pull.number) }),
  });
  const again = x?.stopped != null;
  const needsResume = again && !pull.landing?.resume_publish;
  const resume = useMutation({
    mutationFn: () => api.pulls.resumeLanding(pull.repository, pull.number, pull.head_sha, pull.base_sha),
    onSuccess: (p) => client.setQueryData(keys.pull(pull.repository, pull.number), p),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.pull(pull.repository, pull.number) });
      void client.invalidateQueries({ queryKey: keys.landing(pull.repository) });
    },
  });
  const p = preview.data;
  return (
    <Card p="lg" component="section" aria-label={t("publish.title", { host })} style={{ borderColor: "var(--color-brand-border-subtle)" }}>
      <Stack gap="sm">
        <Group gap="xs">
          <CloudUpload size={18} />
          <Text fw={700}>{t("publish.title", { host })}</Text>
        </Group>
        <Text size="sm" c="var(--color-text-subtle)">
          {t(published ? "publish.mergeThere" : "publish.explain", { host, repository: upstream.repository })}
        </Text>
        {x && <PublishedState pull={pull} />}
        {!published &&
          (preview.isError ? (
            <ErrorMessage error={preview.error} optional />
          ) : p && !editing ? (
            <figure className="preview" aria-label={t("publish.preview")} style={{ margin: 0 }}>
              <Text size="xs" c="dimmed">
                {t("publish.preview")}
              </Text>
              <Text fw={700}>{p.title}</Text>
              <pre>{p.body || t("common.notProvided")}</pre>
              {p.login ? (
                <Group gap={6}>
                  <Users size={14} color="var(--mantine-color-dimmed)" />
                  <Text size="xs" c="dimmed">
                    {t("publish.postedAs", { host, login: p.login })}
                  </Text>
                </Group>
              ) : (
                <Text size="sm" c="var(--color-warning-text)">
                  {t("publish.noAccount", { account: p.account, host })}
                </Text>
              )}
            </figure>
          ) : null)}
        {editing && <TitleBodyEditor pull={pull} host={host} onDone={() => setEditing(false)} />}
        {publish.isError && <Refusal>{refusalOf(t, publish.error, "publish.notReady")}</Refusal>}
        {resume.isError && <Refusal>{refusalOf(t, resume.error, "landing.notReady")}</Refusal>}
        {!published && repo.auto_land && (
          <Text size="sm" c="dimmed">
            {t("publish.auto", { host })}
          </Text>
        )}
        {!published && pull.draft && (
          <Text size="sm" c="dimmed">
            {t("publish.draft")}
          </Text>
        )}
        {!editing && (
          <Group justify="space-between">
            <Button size="xs" variant="default" leftSection={<Pencil size={14} />} onClick={() => setEditing(true)}>
              {t("publish.edit")}
            </Button>
            {needsResume ? (
              <Button
                leftSection={<GitMerge size={16} />}
                disabled={resume.isPending || pull.draft || !pull.head_sha || !pull.base_sha || !headReady || (p !== undefined && p.login === null)}
                onClick={() => resume.mutate()}
              >
                {t(resume.isPending ? "landing.resuming" : "landing.resume")}
              </Button>
            ) : !published && (
              <Button
                leftSection={<CloudUpload size={16} />}
                disabled={publish.isPending || pull.draft || !pull.head_sha || !pull.base_sha || blocked || (p !== undefined && p.login === null)}
                onClick={() => (!again || window.confirm(t("publish.confirmAgain", { host }))) && publish.mutate()}
              >
                {publish.isPending ? t("publish.publishing") : t(again ? "publish.again" : "publish.submit", { host })}
              </Button>
            )}
          </Group>
        )}
      </Stack>
    </Card>
  );
}

/**
 * Edits the pull request's own title and body (Branvil's: the host gets them
 * as the preview shows, at publishing or at the next poll). For the rare case
 * the rewritten text still needs a person's hand.
 */
function TitleBodyEditor({ pull, host, onDone }: { pull: PullRequest; host: string; onDone: () => void }) {
  const t = useT();
  const client = useQueryClient();
  const [form, setForm] = useState({ title: pull.title, body: pull.body, revision: pull.revision });
  const save = useMutation({
    mutationFn: () => api.pulls.update(pull.repository, pull.number, form),
    onSuccess: (p) => {
      client.setQueryData(keys.pull(pull.repository, pull.number), p);
      onDone();
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.pull(pull.repository, pull.number) });
      void client.invalidateQueries({ queryKey: keys.pullPart(pull.repository, pull.number, "publish") });
    },
  });
  return (
    <form
      aria-label={t("publish.edit")}
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <Stack gap="sm">
        <Text size="xs" c="dimmed">
          {t("publish.source", { host })}
        </Text>
        <TextInput label={t("ticket.title")} value={form.title} onChange={(e) => setForm({ ...form, title: e.currentTarget.value })} required />
        <Textarea label={t("publish.body")} value={form.body} onChange={(e) => setForm({ ...form, body: e.currentTarget.value })} rows={8} />
        {save.isError && (isApiError(save.error, "conflict") ? <Refusal>{t("settings.conflict")}</Refusal> : <ErrorMessage error={save.error} />)}
        <Group justify="flex-end" gap="xs">
          <Button variant="default" onClick={onDone}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={save.isPending}>
            {t("common.save")}
          </Button>
        </Group>
      </Stack>
    </form>
  );
}
