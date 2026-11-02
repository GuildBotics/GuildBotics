import { Card, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";
import type { Check } from "../api/types";
import { CopyButton, ErrorMessage, Loading, Sha, Time } from "../components/common";
import { PageHeader } from "../components/PageHeader";
import { useT } from "../i18n";
import { useCommit } from "../query/hooks";
import { Link, navigate } from "../router/Router";
import { href } from "../router/routes";
import { CheckDrawer, CommitChecksCard, drawerTarget } from "./pull/Checks";

/**
 * The permanent commit URL: message, author, parents, and checks. The GUI is
 * not a code browser, so there is no tree or diff here. A CI attempt of a
 * commit no open pull request has opens its drawer here.
 */
export function CommitView({ repo, sha, openJob }: { repo: string; sha: string; openJob?: string }) {
  const t = useT();
  const commit = useCommit(repo, sha);
  const [job, setJob] = useState<{ attemptId: string; hosted: Check | null } | null>(openJob ? { attemptId: openJob, hosted: null } : null);
  const [subject, ...rest] = (commit.data?.message ?? "").split("\n");
  const workspace = repo.split("/")[0] as string;
  return (
    <Stack gap="lg">
      <PageHeader
        back={<Link to={href.pulls(workspace)}>{t("nav.pulls")}</Link>}
        title={<Title order={2}>{commit.data ? subject || t("commit.title") : t("commit.title")}</Title>}
        meta={
          <>
            <span>{repo}</span>
            <Sha repo={repo} value={commit.data?.sha ?? sha} full />
            <CopyButton value={commit.data?.sha ?? sha} />
          </>
        }
      />
      {commit.isPending ? (
        <Loading />
      ) : commit.isError ? (
        <ErrorMessage error={commit.error} />
      ) : (
        <Card p="lg">
          <Stack gap="md">
            {rest.join("\n").trim() && <pre className="commit-message">{rest.join("\n").trim()}</pre>}
            <dl>
              <dt>{t("commit.author")}</dt>
              <dd>{commit.data.author}</dd>
              <dt>{t("commit.authoredAt")}</dt>
              <dd>
                <Time value={commit.data.authored_at} />
              </dd>
              <dt>{t("commit.parents")}</dt>
              <dd>
                {commit.data.parents.length === 0 ? (
                  <Text span c="dimmed">
                    —
                  </Text>
                ) : (
                  commit.data.parents.map((p) => (
                    <span key={p}>
                      <Sha repo={repo} value={p} />{" "}
                    </span>
                  ))
                )}
              </dd>
            </dl>
          </Stack>
        </Card>
      )}
      {commit.isSuccess && <CommitChecksCard repo={repo} sha={commit.data.sha} title={t("pull.checks")} onOpen={(c) => setJob(drawerTarget(c))} />}
      <CheckDrawer
        repo={repo}
        check={job}
        onClose={() => {
          setJob(null);
          if (openJob) navigate(href.commit(repo, sha), { replace: true });
        }}
      />
    </Stack>
  );
}
