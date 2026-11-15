import { Box, Button, Drawer, Group, Stack, Text } from "@mantine/core";
import { useInfiniteQuery, useQueries } from "@tanstack/react-query";
import { GitBranch } from "lucide-react";
import { useState } from "react";
import * as api from "../../api/endpoints";
import type { Check, VerificationState } from "../../api/types";
import { ErrorMessage, Loading, Sha, StateBadge, type Tone } from "../../components/common";
import { useT } from "../../i18n";
import { keys } from "../../query/keys";
import { Link } from "../../router/Router";
import { href } from "../../router/routes";
import { CheckDrawer, CommitChecksCard, drawerTarget } from "../pull/Checks";

const CI_BUDGET = 30;
type TipStatus = VerificationState | "loading" | "error";
const TONE: Record<TipStatus, Tone> = { verified: "success", failing: "danger", verifying: "warning", pending: "warning", loading: "neutral", error: "danger", empty: "neutral" };
const STATUSES: TipStatus[] = ["failing", "verifying", "pending", "verified", "empty", "loading", "error"];

function useTips(paths: string[]) {
  const queries = useQueries({ queries: paths.map((path) => ({ queryKey: keys.verification(path), queryFn: () => api.repos.verification(path) })) });
  return paths.map((path, i) => {
    const query = queries[i]!;
    const status: TipStatus = query.error ? "error" : query.data?.state ?? "loading";
    return { path, branch: query.data?.branch, sha: query.data?.tip, status, error: query.error, data: query.data };
  });
}

type Tip = ReturnType<typeof useTips>[number];

/** The same commit check list as the commit page, without a second enclosing card. */
function RepositoryTipChecks({ tip }: { tip: Tip }) {
  const t = useT();
  const [check, setCheck] = useState<{ attemptId: string; hosted: Check | null } | null>(null);
  if (tip.error) return <ErrorMessage error={tip.error} />;
  if (!tip.data) return <Loading />;
  const data = tip.data;
  return (
    <Stack gap="sm">
      <Group><StateBadge tone={TONE[tip.status]}>{verificationLabel(t, tip)}</StateBadge></Group>
      {data.state === "empty" && <Text size="sm" c="dimmed">{t("repo.noTip", { branch: data.branch })}</Text>}
      {data.state !== "verified" && <Text size="sm">{data.latest_verified ? t("verification.ahead", { count: data.ahead ?? 0 }) : t("verification.noVerified")}</Text>}
      {data.current && ["verifying", "failing"].includes(data.state) && <CommitChecksCard repo={tip.path} sha={data.current.sha} title={t("repo.tipChecks", { branch: data.branch })} embedded onOpen={(c) => setCheck(drawerTarget(c))} />}
      {data.tip && <VerificationHistory repo={tip.path} />}
      <CheckDrawer repo={tip.path} check={check} onClose={() => setCheck(null)} />
    </Stack>
  );
}

function VerificationHistory({ repo }: { repo: string }) {
  const t = useT();
  const query = useInfiniteQuery({ queryKey: keys.verificationCommits(repo), initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => api.repos.verificationCommits(repo, pageParam), getNextPageParam: (page) => page.next_cursor });
  return <Stack gap="xs">
    {query.error && <ErrorMessage error={query.error} />}
    {query.isPending && <Loading />}
    {query.data?.pages.flatMap((page) => page.items).map((commit) => <Group key={commit.sha}>
      <Sha repo={repo} value={commit.sha} /><Text size="sm">{commit.title}</Text>
      <Text size="xs" c="dimmed">{commit.author} · {commit.authored_at}</Text>
      {commit.mark && <StateBadge tone={commit.mark === "verified" ? "success" : commit.mark === "failed" ? "danger" : "warning"}>{t(`verification.${commit.mark}`)}</StateBadge>}
    </Group>)}
    {query.hasNextPage && <Button loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>{t("verification.more")}</Button>}
  </Stack>;
}

function WorkspaceTipChecks({ workspace, tips }: { workspace: string; tips: Tip[] }) {
  const t = useT();
  return (
    <Stack gap="xs" component="section" aria-label={t("pulls.tipOfWorkspace", { workspace })}>
      {tips.map((row) => (
        <Box key={row.path}>
          <Group gap="sm">
            <Link to={href.repo(row.path)} style={{ flex: 1, textDecoration: "none" }}>
              <Group gap="sm">
                <Text span size="sm">{row.path}</Text>{" "}
                <Text span size="sm" c="dimmed">{row.branch ?? "—"}</Text>{" "}
                <StateBadge tone={TONE[row.status]}>{verificationLabel(t, row)}</StateBadge>
              </Group>
            </Link>
            {row.sha && <Sha repo={row.path} value={row.sha} />}
          </Group>
          <RepositoryTipChecks tip={row} />
        </Box>
      ))}
    </Stack>
  );
}

const label = (t: ReturnType<typeof useT>, status: TipStatus) => status === "loading" ? t("common.loading") : status === "error" ? t("pulls.tipError") : status === "empty" ? t("pulls.tipEmpty") : t(`verification.${status}`);

const verificationLabel = (t: ReturnType<typeof useT>, tip: Tip) => tip.status === "verifying" && (tip.data?.current?.retry.number ?? 0) > 0
  ? t("verification.retry", { number: tip.data!.current!.retry.number, max: tip.data!.current!.retry.max }) : label(t, tip.status);

/**
 * Default-branch CI, scoped by the list's repository selector: a summary in
 * the page header, the repositories (or the selected one's jobs) in a drawer.
 */
export function TipChecks({ workspace, paths, repo }: { workspace: string; paths: string[]; repo: string | null }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const shown = repo ? [repo] : paths;
  const tips = useTips(shown.slice(0, CI_BUDGET));
  const branch = repo && tips[0]?.branch;
  const title = branch ? t("repo.tipChecks", { branch }) : t("pulls.tipChecks");
  return (
    <>
      <Button variant="default" size="xs" leftSection={<GitBranch size={14} />} aria-expanded={open} onClick={() => setOpen(true)}>
        <Group gap={6} wrap="nowrap">
          {title}
          {STATUSES.map((status) => {
            const count = tips.filter((tip) => tip.status === status).length;
            return count > 0 && <StateBadge key={status} tone={TONE[status]}>{repo && tips[0] ? verificationLabel(t, tips[0]) : label(t, status)}{repo ? "" : ` ${count}`}</StateBadge>;
          })}
        </Group>
      </Button>
      <Drawer opened={open} onClose={() => setOpen(false)} position="right" size="lg" title={<Text fw={700}>{repo ?? title}</Text>} closeButtonProps={{ "aria-label": t("common.close") }}>
        {repo ? tips[0] && <RepositoryTipChecks tip={tips[0]} /> : <WorkspaceTipChecks workspace={workspace} tips={tips} />}
        {shown.length > CI_BUDGET && <Text size="xs" c="dimmed" mt="xs">{t("pulls.tipCut", { count: CI_BUDGET, omitted: shown.length - CI_BUDGET })}</Text>}
      </Drawer>
    </>
  );
}
