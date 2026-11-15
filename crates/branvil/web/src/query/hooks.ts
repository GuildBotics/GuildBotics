// Query hooks: one per resource the views show. Views never call the API
// client for reads directly.

import { QueryCache, QueryClient, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { ApiError, isApiError, setCsrfToken } from "../api/client";
import * as api from "../api/endpoints";
import type { MemberKind, Session } from "../api/types";
import { keys } from "./keys";

export function createQueryClient(): QueryClient {
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({
      onError: (err) => {
        // An expired session anywhere brings back the guidance page.
        if (isApiError(err, undefined, 401)) client.setQueryData(keys.session(), null);
      },
    }),
    defaultOptions: {
      queries: {
        // Freshness comes from the event stream, not from polling.
        staleTime: 60_000,
        refetchOnWindowFocus: false,
        retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
      },
      mutations: { retry: false },
    },
  });
  return client;
}

export function useSession() {
  return useQuery({
    queryKey: keys.session(),
    queryFn: async (): Promise<Session | null> => {
      try {
        const s = await api.session.get();
        setCsrfToken(s.csrf);
        return s;
      } catch (err) {
        if (isApiError(err, undefined, 401)) {
          setCsrfToken(null);
          return null;
        }
        throw err;
      }
    },
    staleTime: Infinity,
  });
}

export const useRepos = () => useQuery({ queryKey: keys.repos(), queryFn: () => api.repos.list() });

export const useMarkdown = (repo: string, source: string) =>
  useQuery({ queryKey: keys.markdown(repo, source), queryFn: ({ signal }) => api.markdown(repo, source, signal), enabled: source.length > 0 });

export const useWorkspaces = () => useQuery({ queryKey: keys.workspaces(), queryFn: () => api.workspaces.list() });

/** A commit never changes: cached for the session. */
export const useCommit = (repo: string, sha: string) =>
  useQuery({ queryKey: keys.commit(repo, sha), queryFn: () => api.repos.commit(repo, sha), staleTime: Infinity });

export const useCommitChecks = (repo: string, sha: string) =>
  useQuery({ queryKey: keys.commitChecks(repo, sha), queryFn: () => api.repos.commitChecks(repo, sha) });

export const useBranch = (repo: string, name: string, enabled = true) =>
  useQuery({ queryKey: keys.branch(repo, name), queryFn: () => api.repos.branch(repo, name), enabled });

export const useRepo = (repo: string) => useQuery({ queryKey: keys.repo(repo), queryFn: () => api.repos.get(repo) });

export const useLanding = (repo: string) => useQuery({ queryKey: keys.landing(repo), queryFn: () => api.repos.landing(repo) });

export const useRepoPulls = (repo: string, state: "open" | "closed" | "all") =>
  useQuery({ queryKey: keys.repoPulls(repo, state), queryFn: () => api.repos.pulls(repo, state) });

export const useMembers = () => useQuery({ queryKey: keys.members(), queryFn: () => api.members.list() });

/** Those pickers offer (an assignee, reviewers): everyone but runners; empty while loading or on failure. */
export function usePeople(): { name: string; kind: MemberKind }[] {
  const { data } = useMembers();
  return useMemo(() => (data?.items ?? []).filter((m) => m.kind !== "runner").map((m) => ({ name: m.name, kind: m.kind })), [data]);
}

/** A member's kind by name (avatars color agents); undefined while loading or for a stranger. */
export function useMemberKinds(): (name: string) => MemberKind | undefined {
  const { data } = useMembers();
  return useMemo(() => {
    const kinds = new Map((data?.items ?? []).map((m) => [m.name.toLowerCase(), m.kind]));
    return (name: string) => kinds.get(name.toLowerCase());
  }, [data]);
}

/** A member's picture by name; undefined while loading, for none, or for a stranger. */
export function useMemberAvatar(name: string): string | undefined {
  const { data } = useMembers();
  return data?.items.find((m) => m.name.toLowerCase() === name.toLowerCase())?.avatar_url ?? undefined;
}

export const useMemberTokens = (name: string) =>
  useQuery({ queryKey: keys.memberTokens(name), queryFn: () => api.members.tokens(name) });

/** The service's state: what the warnings at the top of every page are made of. The runner has no events, so it is polled. */
export const useDiagnostics = () =>
  useQuery({ queryKey: keys.diagnostics(), queryFn: () => api.admin.diagnostics(), refetchInterval: 30_000 });

/** The notification webhook; polled, since how its last send went has no events. */
export const useWebhook = () => useQuery({ queryKey: keys.webhook(), queryFn: () => api.admin.webhook(), refetchInterval: 30_000 });

/** The CI settings; polled while an image operation runs (it has no events). */
export const useCiSettings = () =>
  useQuery({
    queryKey: keys.ci(),
    queryFn: () => api.admin.ci(),
    // Follow a running image operation, and a runner switched on until it reports.
    refetchInterval: (query) => {
      const s = query.state.data;
      if (s?.operation?.state === "running") return 2_000;
      return s?.runner.mode === "auto" && s.runner.state !== "ready" ? 3_000 : false;
    },
  });

export const useTicket = (repo: string, key: string) =>
  useQuery({ queryKey: keys.ticket(repo, key), queryFn: () => api.tickets.get(repo, key) });

export const useTicketComments = (repo: string, key: string) =>
  useQuery({ queryKey: keys.ticketPart(repo, key, "comments"), queryFn: () => api.tickets.comments(repo, key) });

export const useTicketPulls = (repo: string, key: string) =>
  useQuery({ queryKey: keys.ticketPart(repo, key, "pulls"), queryFn: () => api.tickets.pulls(repo, key) });

export const useBoard = (workspace: string) =>
  useQuery({ queryKey: keys.board(workspace), queryFn: () => api.workspaces.get(workspace) });

export const useCards = (workspace: string, state: "open" | "all", enabled = true) =>
  useQuery({ queryKey: keys.cards(workspace, state), queryFn: () => api.workspaces.cards(workspace, state), enabled });

export const usePull = (repo: string, number: number) =>
  useQuery({ queryKey: keys.pull(repo, number), queryFn: () => api.pulls.get(repo, number) });

export const usePullActivity = (repo: string, number: number) =>
  useQuery({ queryKey: keys.pullPart(repo, number, "activity"), queryFn: () => api.pulls.activity(repo, number) });

export const usePullThreads = (repo: string, number: number) =>
  useQuery({ queryKey: keys.pullPart(repo, number, "threads"), queryFn: () => api.pulls.threads(repo, number) });

export const usePullCommits = (repo: string, number: number) =>
  useQuery({ queryKey: keys.pullPart(repo, number, "commits"), queryFn: () => api.pulls.commits(repo, number) });

/** Keyed by the head sha: the diff changes only when the head does. */
export const usePullFiles = (repo: string, number: number, headSha: string | undefined) =>
  useQuery({
    queryKey: [...keys.pullPart(repo, number, "files"), headSha ?? ""],
    queryFn: () => api.pulls.files(repo, number),
    enabled: headSha !== undefined,
    staleTime: Infinity,
  });

export const usePublishPreview = (repo: string, number: number, enabled = true) =>
  useQuery({
    queryKey: keys.pullPart(repo, number, "publish"),
    queryFn: () => api.pulls.publishPreview(repo, number),
    enabled,
  });

export const useChecks = (repo: string, number: number, enabled = true) =>
  useQuery({ queryKey: keys.pullPart(repo, number, "checks"), queryFn: () => api.pulls.checks(repo, number), enabled });

export const useJob = (repo: string, id: string, enabled = true) =>
  useQuery({ queryKey: keys.job(repo, id), queryFn: () => api.jobs.get(repo, id), enabled });

export const useJobLog = (repo: string, id: string, tail: number, enabled = true) =>
  useQuery({ queryKey: keys.jobLog(repo, id, tail), queryFn: () => api.jobs.log(repo, id, tail), enabled });
