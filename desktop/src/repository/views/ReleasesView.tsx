import {
  Button,
  Card,
  Group,
  Modal,
  Select,
  Stack,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Tag } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import * as api from "../api/endpoints";
import type { ReleasePoint, Releases, VerificationCommit } from "../api/types";
import { ErrorMessage, Loading, Refusal, Sha, StateBadge, Time } from "../components/common";
import { Header } from "../../ui/Header";
import { useT, type Translate } from "../i18n";
import { useRepos, useSession } from "../query/hooks";
import { keys } from "../query/keys";
import { Link } from "../router/Router";
import { href } from "../router/routes";

/**
 * The releases of a workspace's repository, chosen at the top of the page
 * (its first repository until one is chosen): when they are made
 * automatically, the verified commits of the default branch the owner can
 * release, and the releases made so far.
 */
export function ReleasesView({ workspace, repo }: { workspace: string; repo: string | null }) {
  const t = useT();
  const navigate = useNavigate();
  const repos = useRepos();
  const paths = (repos.data?.items ?? [])
    .map((r) => r.path)
    .filter((p) => p.startsWith(`${workspace}/`));
  const shown = repo ?? paths[0] ?? null;
  return (
    <Stack gap="lg">
      <Header title={<Title order={2}>{t("release.title")}</Title>} />
      {repos.error && <ErrorMessage error={repos.error} />}
      {repos.isPending && <Loading />}
      {repos.data && !shown && (
        <Text size="sm" c="dimmed">
          {t("release.noRepositories", { workspace })}
        </Text>
      )}
      {shown && (
        <>
          <Select
            label={t("pulls.repository")}
            w={320}
            value={shown}
            allowDeselect={false}
            onChange={(v) => v && navigate(href.releases(workspace, v))}
            data={paths.includes(shown) ? paths : [shown, ...paths]}
          />
          <RepositoryReleases key={shown} repo={shown} />
        </>
      )}
    </Stack>
  );
}

function RepositoryReleases({ repo }: { repo: string }) {
  const owner = useSession().data?.me.kind === "owner";
  const releases = useQuery({
    queryKey: keys.releases(repo),
    queryFn: () => api.repos.releases(repo),
  });
  const [target, setTarget] = useState<VerificationCommit | null>(null);
  const data = releases.data;
  return (
    <>
      {releases.error && <ErrorMessage error={releases.error} />}
      {releases.isPending && <Loading />}
      {data && (
        <>
          <RuleCard data={data} />
          <CandidatesCard repo={repo} data={data} owner={owner} onRelease={setTarget} />
          <HistoryCard repo={repo} data={data} />
          {target && (
            <ReleaseModal repo={repo} data={data} commit={target} onClose={() => setTarget(null)} />
          )}
        </>
      )}
    </>
  );
}

const timeLabel = (t: Translate, time: string) =>
  time.startsWith("*:")
    ? t("release.hourly", { minute: time.slice(2) })
    : t("release.daily", { time });

function RuleCard({ data }: { data: Releases }) {
  const t = useT();
  return (
    <Card p="lg" component="section" aria-label={t("release.rule")}>
      <Stack gap="xs">
        <Title order={3} fz={16}>
          {t("release.rule")}
        </Title>
        {data.problem ? (
          <Refusal>{t("release.problem", { problem: data.problem })}</Refusal>
        ) : data.schedule.length > 0 ? (
          <>
            <Text size="sm">
              {t("release.schedule", {
                times: data.schedule.map((time) => timeLabel(t, time)).join(t("release.and")),
                zone: data.time_zone,
              })}
            </Text>
            {data.next_at && (
              <Text size="sm">
                {t("release.nextAt")}: <Time value={data.next_at} />
              </Text>
            )}
          </>
        ) : (
          <Text size="sm" c="dimmed">
            {t("release.manualOnly")}
          </Text>
        )}
        <Text size="sm" c="dimmed">
          {t("release.prefix", { prefix: data.tag_prefix })}
        </Text>
      </Stack>
    </Card>
  );
}

/** Release tag names by commit. */
const tagsByCommit = (data: Releases) => {
  const tags = new Map<string, string[]>();
  for (const r of data.releases) tags.set(r.sha, [...(tags.get(r.sha) ?? []), r.name]);
  return tags;
};

/** The ⚠️ of a release point: its messages on hover, its job on click. */
function PointMark({ repo, point }: { repo: string; point: ReleasePoint }) {
  const t = useT();
  const first = point.notes[0];
  return (
    <Tooltip
      multiline
      maw={360}
      label={point.notes.map((note) => (
        <Text size="xs" key={`${note.attempt_id}:${note.message}`}>
          {note.job}: {note.message}
        </Text>
      ))}
    >
      {first ? (
        <Link to={href.job(repo, first.attempt_id)} aria-label={t("release.point")}>
          ⚠️
        </Link>
      ) : (
        <Text span aria-label={t("release.point")}>
          ⚠️
        </Text>
      )}
    </Tooltip>
  );
}

function CandidatesCard({
  repo,
  data,
  owner,
  onRelease,
}: {
  repo: string;
  data: Releases;
  owner: boolean;
  onRelease: (commit: VerificationCommit) => void;
}) {
  const t = useT();
  const query = useInfiniteQuery({
    queryKey: keys.verificationCommits(repo),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => api.repos.verificationCommits(repo, pageParam),
    getNextPageParam: (page) => page.next_cursor,
  });
  const tags = tagsByCommit(data);
  const points = new Map(data.points.map((point) => [point.sha, point]));
  // Commits are newest first: those before the latest release are newer than
  // it, and those before the oldest unreleased release point wait for it.
  const oldestPoint = data.points.at(-1)?.sha;
  let released = false;
  let waiting = oldestPoint !== undefined;
  return (
    <Card p="lg" component="section" aria-label={t("release.candidates")}>
      <Stack gap="xs">
        <Title order={3} fz={16}>
          {t("release.candidates")}
        </Title>
        {data.points.length > 0 && (
          <Text size="sm" c="dimmed">
            {t("release.pointsHelp")}
          </Text>
        )}
        {query.error && <ErrorMessage error={query.error} />}
        {query.isPending && <Loading />}
        {query.data?.pages
          .flatMap((page) => page.items)
          .map((commit) => {
            const names = tags.get(commit.sha) ?? [];
            const newer = !released && names.length === 0;
            released ||= names.length > 0;
            const point = points.get(commit.sha);
            const blocked = waiting && commit.sha !== oldestPoint;
            if (commit.sha === oldestPoint) waiting = false;
            return (
              <Group key={commit.sha} justify="space-between" wrap="nowrap" align="flex-start">
                <Group gap="sm" miw={0}>
                  {point && <PointMark repo={repo} point={point} />}
                  <Sha repo={repo} value={commit.sha} />
                  <Text size="sm">{commit.title}</Text>
                  <Text size="xs" c="dimmed">
                    {commit.author} · <Time value={commit.authored_at} />
                  </Text>
                  {commit.mark && (
                    <StateBadge
                      tone={
                        commit.mark === "verified"
                          ? "success"
                          : commit.mark === "failed"
                            ? "danger"
                            : "warning"
                      }
                    >
                      {t(`verification.${commit.mark}`)}
                    </StateBadge>
                  )}
                  {names.map((name) => (
                    <StateBadge key={name}>{name}</StateBadge>
                  ))}
                </Group>
                {owner && newer && commit.mark === "verified" && (
                  <Button
                    size="xs"
                    variant="default"
                    style={{ flexShrink: 0 }}
                    leftSection={<Tag size={14} />}
                    disabled={blocked}
                    onClick={() => onRelease(commit)}
                  >
                    {t("release.release")}
                  </Button>
                )}
              </Group>
            );
          })}
        {query.hasNextPage && (
          <Button loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
            {t("verification.more")}
          </Button>
        )}
      </Stack>
    </Card>
  );
}

function HistoryCard({ repo, data }: { repo: string; data: Releases }) {
  const t = useT();
  return (
    <Card p="lg" component="section" aria-label={t("release.history")}>
      <Stack gap="xs">
        <Title order={3} fz={16}>
          {t("release.history")}
        </Title>
        {data.releases.length === 0 && (
          <Text size="sm" c="dimmed">
            {t("release.none")}
          </Text>
        )}
        {data.releases.map((release) => (
          <Group gap="sm" key={release.name}>
            <StateBadge tone="success">{release.name}</StateBadge>
            <Sha repo={repo} value={release.sha} />
            <Text size="sm">{release.title}</Text>
            <Text size="xs" c="dimmed">
              <Time value={release.authored_at} />
            </Text>
          </Group>
        ))}
      </Stack>
    </Card>
  );
}

function ReleaseModal({
  repo,
  data,
  commit,
  onClose,
}: {
  repo: string;
  data: Releases;
  commit: VerificationCommit;
  onClose: () => void;
}) {
  const t = useT();
  const client = useQueryClient();
  const suggested = data.next_name.slice(data.tag_prefix.length);
  const [name, setName] = useState(suggested);
  const release = useMutation({
    mutationFn: () =>
      api.repos.release(repo, commit.sha, name.trim() === suggested ? undefined : name.trim()),
    onSuccess: (view) => {
      client.setQueryData(keys.releases(repo), view);
      onClose();
    },
  });
  return (
    <Modal opened onClose={onClose} title={<Text fw={700}>{t("release.confirmTitle")}</Text>}>
      <Stack gap="sm">
        <Group gap="xs">
          <Sha repo={repo} value={commit.sha} />
          <Text size="sm">{commit.title}</Text>
        </Group>
        <TextInput
          label={t("release.name")}
          description={t("release.nameHelp")}
          leftSection={
            <Text size="sm" c="dimmed" pl="xs">
              {data.tag_prefix}
            </Text>
          }
          leftSectionWidth={Math.max(48, data.tag_prefix.length * 8 + 16)}
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
        />
        {release.error && <ErrorMessage error={release.error} />}
        <Group justify="flex-end" gap="xs">
          <Button variant="default" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            leftSection={<Tag size={14} />}
            loading={release.isPending}
            disabled={name.trim() === ""}
            onClick={() => release.mutate()}
          >
            {t("release.release")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
