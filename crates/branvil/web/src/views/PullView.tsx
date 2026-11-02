import { Anchor, Button, Group, Stack, Text, Title } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileDiff } from "lucide-react";
import { useState } from "react";
import * as api from "../api/endpoints";
import type { Check } from "../api/types";
import { ErrorMessage, Loading, Post, PostHead, StateBadge } from "../components/common";
import { Markdown } from "../components/Markdown";
import { PageHeader } from "../components/PageHeader";
import { ReviewersPicker } from "../components/PeoplePicker";
import { useT } from "../i18n";
import { useMemberKinds, usePull, usePullActivity, usePullCommits, usePullFiles, usePullThreads, useRepo, usePeople } from "../query/hooks";
import { keys } from "../query/keys";
import { Link, navigate } from "../router/Router";
import { href } from "../router/routes";
import { CheckDrawer, ChecksCard, drawerTarget } from "./pull/Checks";
import { Conversation } from "./pull/Conversation";
import { FilesDrawer } from "./pull/FilesDrawer";
import { MergeCard, PublishCard } from "./pull/Landing";
import { LandingJourney } from "./pull/LandingStatus";
import { Tickets } from "./pull/Tickets";

export function PullView({ repo, number, openJob }: { repo: string; number: number; openJob?: string }) {
  const t = useT();
  const client = useQueryClient();
  const pull = usePull(repo, number);
  const repository = useRepo(repo);
  const activity = usePullActivity(repo, number);
  const threads = usePullThreads(repo, number);
  const commits = usePullCommits(repo, number);
  const files = usePullFiles(repo, number, pull.data?.head_sha);
  const kindOf = useMemberKinds();
  const [filesOpen, setFilesOpen] = useState<{ focus: string | null } | null>(null);
  const [check, setCheck] = useState<{ attemptId: string; hosted: Check | null } | null>(openJob ? { attemptId: openJob, hosted: null } : null);
  const refresh = () => void client.invalidateQueries({ queryKey: keys.pull(repo, number) });
  const people = usePeople();

  const setDraft = useMutation({
    mutationFn: (draft: boolean) => api.pulls.update(repo, number, { draft }),
    onSuccess: (p) => client.setQueryData(keys.pull(repo, number), p),
    onSettled: refresh,
  });
  const closeReopen = useMutation({
    mutationFn: (close: boolean) => (close ? api.pulls.close(repo, number) : api.pulls.reopen(repo, number)),
    onSuccess: (p) => client.setQueryData(keys.pull(repo, number), p),
    onSettled: refresh,
  });
  const reviewer = useMutation({
    mutationFn: ({ name, request }: { name: string; request: boolean }) =>
      request ? api.pulls.requestReviewer(repo, number, name) : api.pulls.withdrawReviewer(repo, number, name),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.pull(repo, number), exact: true }),
  });

  if (pull.isPending) return <Loading />;
  if (pull.isError) return <ErrorMessage error={pull.error} />;
  const pr = pull.data;
  const state = pr.merged ? "merged" : pr.state;
  const workspace = repo.split("/")[0] as string;
  const fileItems = files.data?.items;

  return (
    <Stack gap="lg">
      <PageHeader
        back={<Link to={href.pulls(workspace)}>{t("nav.pulls")}</Link>}
        title={
          <Title order={2}>
            <span className="page-key">{pr.label}</span> {pr.title}
          </Title>
        }
        meta={
          <>
            <StateBadge tone={state === "closed" ? "neutral" : "success"}>{t(`pull.state.${state}`)}</StateBadge>
            {pr.draft && <StateBadge tone="warning">{t("pull.draft")}</StateBadge>}
            <Anchor component={Link} to={href.repo(repo)} size="sm" title={t("pull.repoPulls")}>
              {repo}
            </Anchor>
            <Text span c="dimmed">
              ·
            </Text>
            <code>{pr.head}</code> → <code>{pr.base}</code>
            {!pr.head_sha && (
              <Text span size="sm" c="var(--color-danger-text)">
                ({t("pull.branchGone")})
              </Text>
            )}
            <Text span c="dimmed">
              ·
            </Text>
            <Anchor component="button" type="button" size="sm" onClick={() => setFilesOpen({ focus: null })}>
              <Group gap={4} wrap="nowrap">
                <FileDiff size={14} />
                {t("files.summary", { count: fileItems?.length ?? pr.changed_files })}
                {fileItems && (
                  <>
                    <span className="add">+{fileItems.reduce((n, f) => n + f.additions, 0)}</span>
                    <span className="del">−{fileItems.reduce((n, f) => n + f.deletions, 0)}</span>
                  </>
                )}
              </Group>
            </Anchor>
          </>
        }
        right={
          <ReviewersPicker
            people={people}
            states={pr.review_states}
            author={pr.author}
            disabled={pr.state !== "open" || reviewer.isPending}
            onToggle={(name, request) => reviewer.mutate({ name, request })}
          />
        }
      />
      {reviewer.isError && <ErrorMessage error={reviewer.error} />}
      {pr.state === "open" && repository.data && <LandingJourney pull={pr} repo={repository.data} />}

      <Post head={<PostHead author={pr.author} kind={kindOf(pr.author)} at={pr.created_at} />}>
        <section aria-label={t("pull.description")}>
          {pr.body.trim() ? (
            <Markdown repo={repo} source={pr.body} />
          ) : (
            <Text size="sm" c="dimmed">
              {t("common.notProvided")}
            </Text>
          )}
        </section>
        <Tickets key={pr.url} pull={pr} />
      </Post>

      <Conversation
        pull={pr}
        activity={activity.data}
        activityError={activity.error}
        threads={threads.data?.items}
        threadsError={threads.error}
        commits={commits.data?.items}
        commitsError={commits.error}
        kindOf={kindOf}
        onOpenFile={(path) => setFilesOpen({ focus: path })}
      />

      <ChecksCard repo={repo} pull={pr} requiredChecks={repository.data?.required_checks ?? []} onOpen={(c) => setCheck(drawerTarget(c))} />

      {pr.state === "open" &&
        (repository.data?.upstream ? (
          <PublishCard pull={pr} repo={repository.data} upstream={repository.data.upstream} />
        ) : repository.data ? (
          <MergeCard pull={pr} repo={repository.data} />
        ) : null)}

      <Group justify="flex-end" gap="xs" component="section" aria-label={t("pull.otherActions")}>
        {(setDraft.isError || closeReopen.isError) && <ErrorMessage error={setDraft.error ?? closeReopen.error} />}
        {pr.state === "open" && (
          <Button variant="default" onClick={() => setDraft.mutate(!pr.draft)} disabled={setDraft.isPending}>
            {pr.draft ? t("pull.markReady") : t("pull.markDraft")}
          </Button>
        )}
        {!pr.merged && (
          <Button variant="default" onClick={() => closeReopen.mutate(pr.state === "open")} disabled={closeReopen.isPending}>
            {pr.state === "open" ? t("pull.close") : t("pull.reopen")}
          </Button>
        )}
      </Group>

      <FilesDrawer
        repo={repo}
        number={number}
        headSha={pr.head_sha}
        baseSha={pr.base_sha}
        opened={filesOpen !== null}
        focus={filesOpen?.focus ?? null}
        onClose={() => setFilesOpen(null)}
      />
      <CheckDrawer
        repo={repo}
        check={check}
        onClose={() => {
          setCheck(null);
          // A job's own address showed this page with its drawer; closing it leaves the page.
          if (openJob) navigate(href.pull(repo, number), { replace: true });
        }}
      />
    </Stack>
  );
}
