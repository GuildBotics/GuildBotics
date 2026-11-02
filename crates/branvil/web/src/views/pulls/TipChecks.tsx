import { Box, Button, Drawer, Group, Stack, Text } from "@mantine/core";
import { useQueries } from "@tanstack/react-query";
import { GitBranch } from "lucide-react";
import { useState } from "react";
import { isApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import type { Check } from "../../api/types";
import { ErrorMessage, Loading, Sha, StateBadge, type Tone } from "../../components/common";
import { useT } from "../../i18n";
import { keys } from "../../query/keys";
import { Link } from "../../router/Router";
import { href } from "../../router/routes";
import { rollup, type CiRollup } from "../board/ordering";
import { CheckDrawer, CommitChecksCard, drawerTarget } from "../pull/Checks";

const CI_BUDGET = 30;
type TipStatus = CiRollup | "loading" | "error" | "empty";
const TONE: Record<TipStatus, Tone> = { passing: "success", failing: "danger", pending: "warning", not_registered: "warning", none: "neutral", loading: "neutral", error: "danger", empty: "neutral" };
const STATUSES: TipStatus[] = ["failing", "pending", "not_registered", "passing", "none", "empty", "loading", "error"];

/** Each repository's default branch tip and its CI; the queries stay mounted while the drawer is closed, so the summary follows repository events. */
function useTips(paths: string[]) {
  const repositories = useQueries({ queries: paths.map((path) => ({ queryKey: keys.repo(path), queryFn: () => api.repos.get(path) })) });
  const branches = useQueries({ queries: paths.map((path, i) => {
    const branch = repositories[i]?.data?.default_branch ?? "";
    return { queryKey: keys.branch(path, branch), queryFn: () => api.repos.branch(path, branch), enabled: branch !== "" && !repositories[i]?.isError };
  }) });
  const checks = useQueries({ queries: paths.map((path, i) => {
    const sha = branches[i]?.data?.sha ?? "";
    return { queryKey: keys.commitChecks(path, sha), queryFn: () => api.repos.commitChecks(path, sha), enabled: sha !== "" && !branches[i]?.isError && !repositories[i]?.isError };
  }) });
  return paths.map((path, i) => {
    const repository = repositories[i]!;
    const branch = branches[i]!;
    const check = checks[i]!;
    const empty = branch.isError && isApiError(branch.error, undefined, 404);
    const error = repository.error ?? (empty ? null : branch.error ?? check.error);
    const status: TipStatus = error ? "error" : empty ? "empty" : check.data ? rollup(check.data) : "loading";
    return { path, branch: repository.data?.default_branch, sha: branch.data?.sha, status, error };
  });
}

type Tip = ReturnType<typeof useTips>[number];

/** The same commit check list as the commit page, without a second enclosing card. */
function RepositoryTipChecks({ tip }: { tip: Tip }) {
  const t = useT();
  const [check, setCheck] = useState<{ attemptId: string; hosted: Check | null } | null>(null);
  if (tip.status === "empty") return <Text size="sm" c="dimmed">{t("repo.noTip", { branch: tip.branch ?? "" })}</Text>;
  if (tip.error) return <ErrorMessage error={tip.error} />;
  if (!tip.sha) return <Loading />;
  return (
    <>
      <CommitChecksCard repo={tip.path} sha={tip.sha} title={t("repo.tipChecks", { branch: tip.branch ?? "" })} embedded onOpen={(c) => setCheck(drawerTarget(c))} />
      <CheckDrawer repo={tip.path} check={check} onClose={() => setCheck(null)} />
    </>
  );
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
                <StateBadge tone={TONE[row.status]}>{label(t, row.status)}</StateBadge>
              </Group>
            </Link>
            {row.sha && <Sha repo={row.path} value={row.sha} />}
          </Group>
          {row.error && <ErrorMessage error={row.error} />}
        </Box>
      ))}
    </Stack>
  );
}

const label = (t: ReturnType<typeof useT>, status: TipStatus) => status === "loading" ? t("common.loading") : status === "error" ? t("pulls.tipError") : status === "empty" ? t("pulls.tipEmpty") : t(`ci.${status}`);

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
            return count > 0 && <StateBadge key={status} tone={TONE[status]}>{label(t, status)}{repo ? "" : ` ${count}`}</StateBadge>;
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
