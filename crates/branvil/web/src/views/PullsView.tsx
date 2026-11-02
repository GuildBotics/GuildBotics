import { Button, Card, Group, Select, Stack, Text, TextInput } from "@mantine/core";
import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import type { PullRequest } from "../api/types";
import { ErrorMessage, Loading, MemberAvatar, StateBadge, Time, type Tone } from "../components/common";
import { PageHeader } from "../components/PageHeader";
import { useT } from "../i18n";
import { useChecks, useMemberKinds, useRepos } from "../query/hooks";
import { usePullList } from "../query/pullList";
import { Link, navigate } from "../router/Router";
import { href } from "../router/routes";
import { rollup, type CiRollup } from "./board/ordering";
import { JourneyBadge } from "./pull/LandingStatus";
import { TipChecks } from "./pulls/TipChecks";
import { ReviewersCell } from "./pulls/ReviewersCell";

type State = "open" | "closed" | "all";
const ALL_REPOS = "*";

/** The most rows whose checks are read (one request each), as the board limits them. */
const CI_BUDGET = 30;

const CI_TONE: Record<CiRollup, Tone> = { passing: "success", failing: "danger", pending: "warning", not_registered: "warning", none: "neutral" };

function CiCell({ pull, load }: { pull: PullRequest; load: boolean }) {
  const t = useT();
  const checks = useChecks(pull.repository, pull.number, load);
  if (!checks.data) return null;
  return <StateBadge tone={CI_TONE[rollup(checks.data)]}>{t(`ci.${rollup(checks.data)}`)}</StateBadge>;
}

/**
 * The pull requests of a workspace's repositories; a repository's page
 * (`/{repo}`) is this list for that repository. Each open pull request says
 * where it is on its way into its base; the default branches' CI opens from
 * the header.
 */
export function PullsView({ workspace, repo }: { workspace: string; repo: string | null }) {
  const t = useT();
  const repos = useRepos();
  const kindOf = useMemberKinds();
  const [state, setState] = useState<State>("open");
  const [text, setText] = useState("");
  const repoOf = (path: string) => repos.data?.items.find((r) => r.path === path);
  const paths = useMemo(
    () => (repos.data?.items ?? []).map((r) => r.path).filter((p) => p.startsWith(`${workspace}/`)),
    [repos.data, workspace],
  );
  const shown = repo ? [repo] : paths;
  const list = usePullList(shown, state, !repos.isPending);
  const pending = repos.isPending || list.isPending;
  const failed = repos.error ?? (list.data ? null : list.error);
  const needle = text.trim().toLowerCase();
  const pulls = (list.data?.pages.flatMap((p) => p.items) ?? [])
    .filter((p) => !needle || `${p.repository} ${p.label} ${p.title}`.toLowerCase().includes(needle));
  // Checks are read for the first open rows only (closed rows show no checks).
  let opened = 0;
  const emptyMessage = needle
    ? list.hasNextPage ? t("pulls.noLoadedMatches") : t("pulls.noMatches")
    : state === "open" ? t("pulls.noneOpen") : t("pulls.none");

  return (
    <Stack gap="lg">
      <PageHeader
        title={t("pulls.title")}
        meta={
          <Text size="sm" c="dimmed">
            {repo ?? t("pulls.ofWorkspace", { workspace })}
          </Text>
        }
        right={<TipChecks key={repo ?? ALL_REPOS} workspace={workspace} paths={paths} repo={repo} />}
      />
      <Card p="lg">
        <Group gap="sm" mb={14} role="search" aria-label={t("pulls.filters")}>
          <TextInput
            aria-label={t("board.filterText")}
            placeholder={t("pulls.filterPlaceholder")}
            leftSection={<Search size={16} />}
            value={text}
            onChange={(e) => setText(e.currentTarget.value)}
            style={{ flex: "1 1 260px", maxWidth: 380 }}
          />
          <Select
            aria-label={t("pulls.repository")}
            w={220}
            value={repo ?? ALL_REPOS}
            allowDeselect={false}
            onChange={(v) => v && navigate(v === ALL_REPOS ? href.pulls(workspace) : href.repo(v))}
            data={[{ value: ALL_REPOS, label: `${t("pulls.repository")}: ${t("board.any")}` }, ...paths.map((p) => ({ value: p, label: p }))]}
          />
          <Select
            aria-label={t("pulls.state")}
            w={170}
            value={state}
            allowDeselect={false}
            onChange={(v) => v && setState(v as State)}
            data={[
              { value: "open", label: `${t("pulls.state")}: ${t("pull.state.open")}` },
              { value: "closed", label: `${t("pulls.state")}: ${t("pull.state.closed")}` },
              { value: "all", label: `${t("pulls.state")}: ${t("board.any")}` },
            ]}
          />
        </Group>
        {failed ? (
          <ErrorMessage error={failed} />
        ) : pending ? (
          <Loading />
        ) : (
          <div className="list pull-list" role="table" aria-label={t("pulls.title")}>
            <div className="list-header pull-cols" role="row">
              <span role="columnheader">{t("pulls.repository")}</span>
              <span role="columnheader">PR</span>
              <span role="columnheader">{t("ticket.title")}</span>
              <span role="columnheader">{t("pulls.state")}</span>
              <span role="columnheader">{t("ci.label")}</span>
              <span role="columnheader">{t("ticket.author")}</span>
              <span role="columnheader">{t("member.reviewers")}</span>
              <span role="columnheader">{t("pulls.updated")}</span>
            </div>
            {pulls.length === 0 ? (
              <div className="empty-row">{emptyMessage}</div>
            ) : (
              pulls.map((p) => {
                const s = p.merged ? "merged" : p.state;
                const load = s === "open" && opened++ < CI_BUDGET;
                const repository = repoOf(p.repository);
                return (
                  <Link key={p.url} to={href.pull(p.repository, p.number)} className="list-row pull-cols" role="row" aria-label={`${p.repository} ${p.label} ${p.title}`}>
                    <Text span size="sm" c="dimmed" role="cell" truncate title={p.repository}>
                      {p.repository.slice(workspace.length + 1)}
                    </Text>
                    <span className="mono" role="cell">
                      {p.label}
                    </span>
                    <span role="cell">{p.title}</span>
                    <span role="cell">
                      {s !== "open" ? <StateBadge tone={s === "closed" ? "neutral" : "success"}>{t(`pull.state.${s}`)}</StateBadge> : repository && <JourneyBadge pull={p} repo={repository} load={load} />}
                    </span>
                    <span role="cell">{p.state === "open" && <CiCell pull={p} load={load} />}</span>
                    <Group gap={6} role="cell" wrap="nowrap">
                      <MemberAvatar name={p.author} kind={kindOf(p.author)} size={24} />
                      <Text span size="sm" truncate style={{ minWidth: 0 }} title={p.author}>
                        {p.author}
                      </Text>
                    </Group>
                    <span role="cell">
                      <ReviewersCell pull={p} />
                    </span>
                    <Text span size="xs" c="dimmed" role="cell">
                      <Time value={p.updated_at} />
                    </Text>
                  </Link>
                );
              })
            )}
          </div>
        )}
        {list.data && list.error && <ErrorMessage error={list.error} />}
        {list.hasNextPage && (
          <Button variant="light" mt="sm" loading={list.isFetching} onClick={() => void list.fetchNextPage()}>
            {t("pulls.loadMore")}
          </Button>
        )}
      </Card>
    </Stack>
  );
}
