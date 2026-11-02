// A pull request's conversation: comments, reviews, line threads, and commits
// in one frame, in the order they were posted (a commit when it was committed),
// with one box for a comment or a verdict and one line for a commit.

import { Anchor, Badge, Button, Card, Group, Text } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, GitCommitHorizontal, MessageSquare } from "lucide-react";
import { useState } from "react";
import { isApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import type { Activity, MemberKind, PullCommit, PullRequest, Reaction, Review, Thread, Verdict } from "../../api/types";
import { CommentView, ComposeForm, ErrorMessage, Loading, Notice, PostHead, Reactions, Refusal, Sha, StateBadge, Time, type OnReact } from "../../components/common";
import { Markdown } from "../../components/Markdown";
import { useT } from "../../i18n";
import { keys } from "../../query/keys";
import { useFragmentTarget } from "../../router/Router";
import { href } from "../../router/routes";

type Entry =
  | { at: string; comment: Activity["comments"][number] }
  | { at: string; review: Review }
  | { at: string; thread: Thread }
  | { at: string; commit: PullCommit };

function CommitLine({ repo, commit }: { repo: string; commit: PullCommit }) {
  const t = useT();
  return (
    <div className="timeline-commit">
      <GitCommitHorizontal size={16} color="var(--mantine-color-dimmed)" />
      <Text span size="xs" c="dimmed">
        {t("commit.by", { author: commit.author })}
      </Text>
      <Text span size="sm" lineClamp={1} style={{ flex: 1, minWidth: 0 }}>
        {commit.subject}
      </Text>
      <Sha repo={repo} value={commit.sha} />
      <Text span size="xs" c="dimmed">
        <Time value={commit.committed_at} />
      </Text>
    </div>
  );
}

function ReviewView({ repo, number, review, headSha, kind, onReact }: { repo: string; number: number; review: Review; headSha: string; kind?: MemberKind; onReact: OnReact }) {
  const t = useT();
  const target = useFragmentTarget(review.id);
  return (
    <article {...target} className={`comment ${review.verdict}`} id={review.id} aria-label={t("review.by", { author: review.author })}>
      <PostHead author={review.author} kind={kind} at={review.submitted_at} url={`${href.pull(repo, number)}#${encodeURIComponent(review.id)}`}>
        <StateBadge tone={review.verdict === "approve" ? "success" : review.verdict === "request_changes" ? "danger" : "neutral"}>
          {t(`verdict.${review.verdict}`)}
        </StateBadge>
        <Text span size="xs" c="dimmed">
          {t("pull.onRevision")} <Sha repo={repo} value={review.commit} />
          {review.commit !== headSha && ` (${t("pull.olderRevision")})`}
        </Text>
      </PostHead>
      <div className="post-body">
        {review.body.trim() && <Markdown repo={repo} source={review.body} />}
        <Reactions reactions={review.reactions ?? []} onReact={onReact} />
      </div>
    </article>
  );
}

function ThreadView({
  repo,
  number,
  thread,
  kindOf,
  onOpenFile,
}: {
  repo: string;
  number: number;
  thread: Thread;
  kindOf: (name: string) => MemberKind | undefined;
  onOpenFile: (path: string) => void;
}) {
  const t = useT();
  const client = useQueryClient();
  const [open, setOpen] = useState(!thread.resolved);
  const refresh = () => {
    void client.invalidateQueries({ queryKey: keys.pullPart(repo, number, "threads") });
    void client.invalidateQueries({ queryKey: keys.pullPart(repo, number, "activity") });
  };
  const reply = useMutation({ mutationFn: (body: string) => api.pulls.reply(repo, number, thread.id, body), onSettled: refresh });
  const resolve = useMutation({ mutationFn: (resolved: boolean) => api.pulls.resolve(repo, number, thread.id, resolved), onSettled: refresh });
  const react = useMutation({
    mutationFn: ({ id, reaction, on }: { id: string; reaction: Reaction; on: boolean }) => api.pulls.react(repo, number, id, reaction, on),
    onSettled: refresh,
  });
  const first = thread.comments[0];
  return (
    <details className="thread" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>
        <ChevronRight size={14} style={{ transform: open ? "rotate(90deg)" : undefined, color: "var(--mantine-color-dimmed)" }} />
        <MessageSquare size={14} color="var(--mantine-color-dimmed)" />
        <Anchor
          component="button"
          type="button"
          size="sm"
          title={t("thread.openFile")}
          onClick={(e) => {
            e.preventDefault();
            onOpenFile(thread.path);
          }}
        >
          <code>
            {thread.path}:{thread.line}
          </code>
        </Anchor>
        <Badge color="neutral" variant="light" size="sm">
          {thread.side === "LEFT" ? t("thread.left") : t("thread.right")}
        </Badge>
        {!open && first && (
          <Text span size="sm" c="dimmed" lineClamp={1} style={{ flex: 1, minWidth: 0 }}>
            {first.author}: {first.body}
          </Text>
        )}
        <Group gap={6} ml="auto">
          {thread.outdated && <StateBadge>{t("thread.outdated")}</StateBadge>}
          {thread.resolved && <StateBadge tone="success">{t("thread.resolved")}</StateBadge>}
        </Group>
      </summary>
      {/* Keep replies mounted so a fragment can reveal a collapsed thread. */}
      <div className="thread-body">
        {thread.comments.map((c) => (
          <CommentView repo={repo} key={c.id} comment={c} url={`${href.pull(repo, number)}#${encodeURIComponent(c.id)}`} kind={kindOf(c.author)} onReact={(reaction, on) => react.mutate({ id: c.id, reaction, on })} />
        ))}
        <ComposeForm
          label={t("thread.reply")}
          submitLabel={t("thread.replySubmit")}
          pending={reply.isPending}
          error={reply.error}
          onSubmit={(body) => reply.mutateAsync(body)}
          actions={() => (
            <Button variant="default" onClick={() => resolve.mutate(!thread.resolved)} disabled={resolve.isPending}>
              {thread.resolved ? t("thread.unresolve") : t("thread.resolve")}
            </Button>
          )}
        />
        {(resolve.isError || react.isError) && <ErrorMessage error={resolve.error ?? react.error} />}
      </div>
    </details>
  );
}

export function Conversation({
  pull,
  activity,
  activityError,
  threads,
  threadsError,
  commits,
  commitsError,
  kindOf,
  onOpenFile,
}: {
  pull: PullRequest;
  activity: Activity | undefined;
  activityError: unknown;
  threads: Thread[] | undefined;
  threadsError: unknown;
  commits: PullCommit[] | undefined;
  commitsError: unknown;
  kindOf: (name: string) => MemberKind | undefined;
  onOpenFile: (path: string) => void;
}) {
  const t = useT();
  const client = useQueryClient();
  const repo = pull.repository;
  const number = pull.number;
  const [headMoved, setHeadMoved] = useState<string | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: keys.pullPart(repo, number, "activity") });
  const react = useMutation({
    mutationFn: ({ id, reaction, on }: { id: string; reaction: Reaction; on: boolean }) => api.pulls.react(repo, number, id, reaction, on),
    onSettled: refresh,
  });
  const comment = useMutation({ mutationFn: (body: string) => api.pulls.comment(repo, number, body), onSettled: refresh });
  const review = useMutation({
    // `commit` is the head the user is looking at; the service refuses it if the head moved.
    mutationFn: ({ verdict, body }: { verdict: Verdict; body: string }) => api.pulls.review(repo, number, verdict, body, pull.head_sha),
    onMutate: () => setHeadMoved(null),
    onError: (err) => {
      if (isApiError(err, "head_moved")) setHeadMoved(pull.head_sha);
    },
    onSettled: () => {
      refresh();
      void client.invalidateQueries({ queryKey: keys.pull(repo, number) });
    },
  });

  // A link target scrolls into view once, when it mounts, so the timeline
  // appears only when all of its sources have answered: nothing is inserted
  // above the target afterwards. A thread takes its place in the timeline
  // when its first comment was posted.
  const settled = Boolean((activity || activityError) && (threads || threadsError) && (commits || commitsError));
  const entries: Entry[] = settled
    ? [
        ...(activity?.comments ?? []).map((c) => ({ at: c.created_at, comment: c })),
        ...(activity?.reviews ?? []).map((r) => ({ at: r.submitted_at, review: r })),
        ...(threads ?? []).map((th) => ({ at: th.comments[0]?.created_at ?? "", thread: th })),
        ...(commits ?? []).map((c) => ({ at: c.committed_at, commit: c })),
      ].sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0))
    : [];
  const open = pull.state === "open";
  const [beforeHead, afterHead] = t("pull.headMoved").split("{sha}");

  return (
    <Card p="lg" component="section" aria-label={t("pull.conversation")}>
      <div className="timeline">
        {Boolean(activityError) && <ErrorMessage error={activityError} />}
        {Boolean(threadsError) && <ErrorMessage error={threadsError} />}
        {Boolean(commitsError) && <ErrorMessage error={commitsError} />}
        {!settled && <Loading />}
        {entries.map((e) =>
          "comment" in e ? (
            <CommentView repo={repo} key={e.comment.id} comment={e.comment} url={`${href.pull(repo, number)}#${encodeURIComponent(e.comment.id)}`} kind={kindOf(e.comment.author)} onReact={(reaction, on) => react.mutate({ id: e.comment.id, reaction, on })} />
          ) : "review" in e ? (
            <ReviewView repo={repo} number={number} key={e.review.id} review={e.review} headSha={pull.head_sha} kind={kindOf(e.review.author)} onReact={(reaction, on) => react.mutate({ id: e.review.id, reaction, on })} />
          ) : "commit" in e ? (
            <CommitLine key={e.commit.sha} repo={repo} commit={e.commit} />
          ) : (
            <ThreadView key={e.thread.id} repo={repo} number={number} thread={e.thread} kindOf={kindOf} onOpenFile={onOpenFile} />
          ),
        )}
        {react.isError && <ErrorMessage error={react.error} />}
        {headMoved && <Refusal>{beforeHead}<Sha repo={repo} value={headMoved} />{afterHead}</Refusal>}
        {review.isError && !headMoved && <ErrorMessage error={review.error} />}
        {review.isSuccess && <Notice>{t("pull.reviewSubmitted")}</Notice>}
        <ComposeForm
          label={open ? t("pull.composeReview") : t("comment.new")}
          submitLabel={t("comment.submit")}
          pending={comment.isPending || review.isPending}
          error={comment.error}
          onSubmit={(body) => comment.mutateAsync(body)}
          hint={
            open && (
              <>
                {t("pull.reviewingRevision")} <Sha repo={repo} value={pull.head_sha} />
              </>
            )
          }
          submitVariant={open ? "default" : "filled"}
          trailing={(body, clear) =>
            open && (
              <>
                <Button variant="default" disabled={review.isPending || !pull.head_sha} onClick={() => review.mutate({ verdict: "request_changes", body }, { onSuccess: clear })}>
                  {t("verdict.request_changes")}
                </Button>
                <Button variant="light" disabled={review.isPending || !pull.head_sha} onClick={() => review.mutate({ verdict: "approve", body }, { onSuccess: clear })}>
                  {t("verdict.approve")}
                </Button>
              </>
            )
          }
        />
      </div>
    </Card>
  );
}
