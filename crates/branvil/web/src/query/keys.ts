// Query keys. Every key starts with the kind of item and its path, so a
// change event (`{kind, path, id}`) maps to a key prefix.

export const keys = {
  markdown: (repo: string, source: string) => ["markdown", repo, source] as const,
  session: () => ["session"] as const,
  repos: () => ["repos"] as const,
  workspaces: () => ["workspaces"] as const,
  members: () => ["members"] as const,
  memberTokens: (name: string) => ["members", name, "tokens"] as const,
  diagnostics: () => ["diagnostics"] as const,
  ci: () => ["ci"] as const,
  webhook: () => ["webhook"] as const,

  repo: (repo: string) => ["repo", repo] as const,
  landing: (repo: string) => ["repo", repo, "landing"] as const,
  repoPulls: (repo: string, state: string) => ["repo", repo, "pull-list", state] as const,
  pullList: (paths: string[], state: string) => ["pull-lists", paths, state] as const,
  branch: (repo: string, name: string) => ["repo", repo, "branch", name] as const,
  commit: (repo: string, sha: string) => ["repo", repo, "commit", sha] as const,
  commitChecks: (repo: string, sha: string) => ["repo", repo, "commit", sha, "checks"] as const,
  pull: (repo: string, number: number) => ["repo", repo, "pull", number] as const,
  pullPart: (repo: string, number: number, part: "activity" | "threads" | "commits" | "files" | "checks" | "publish") =>
    ["repo", repo, "pull", number, part] as const,
  job: (repo: string, id: string) => ["repo", repo, "job", id] as const,
  jobLog: (repo: string, id: string, tail: number) => ["repo", repo, "job", id, "log", tail] as const,
  ticket: (repo: string, key: string) => ["repo", repo, "ticket", key] as const,
  ticketPart: (repo: string, key: string, part: "comments" | "pulls") => ["repo", repo, "ticket", key, part] as const,

  /** A workspace with its board. */
  board: (workspace: string) => ["board", workspace] as const,
  cards: (workspace: string, state: "open" | "all") => ["board", workspace, "cards", state] as const,
};
