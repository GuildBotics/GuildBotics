import { ErrorMessage, Loading } from "../components/common";
import { useJob, useRepoPulls } from "../query/hooks";
import { CommitView } from "./CommitView";
import { PullView } from "./PullView";

/**
 * A CI attempt's permanent address: the open pull request whose head it ran
 * on, with the attempt's drawer open; the commit's page when no open pull
 * request has that head.
 */
export function JobView({ repo, attemptId }: { repo: string; attemptId: string }) {
  const job = useJob(repo, attemptId);
  const pulls = useRepoPulls(repo, "open");
  if (job.isPending || pulls.isPending) return <Loading />;
  if (job.isError) return <ErrorMessage error={job.error} />;
  const pull = pulls.data?.items.find((p) => p.head_sha === job.data.sha);
  if (pull) return <PullView repo={repo} number={pull.number} openJob={attemptId} />;
  return <CommitView repo={repo} sha={job.data.sha} openJob={attemptId} />;
}
