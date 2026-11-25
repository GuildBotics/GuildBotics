export type Route =
  | { name: "home" }
  | { name: "settings"; section: string | null; repo?: string }
  | { name: "workspace"; workspace: string }
  | { name: "pulls"; workspace: string }
  | { name: "ticket"; repo: string; key: string }
  | { name: "pull"; repo: string; number: number }
  | { name: "commit"; repo: string; sha: string }
  | { name: "job"; repo: string; attemptId: string }
  | { name: "repo"; repo: string }
  | { name: "not_found"; pathname: string };

function decode(segment: string): string | null {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}

export function parseRoute(pathname: string): Route {
  const raw = pathname.split("/").filter((s) => s !== "");
  const segs: string[] = [];
  for (const s of raw) {
    const d = decode(s);
    if (d === null) return { name: "not_found", pathname };
    if (!d || d.includes("/") || d.includes("\\")) return { name: "not_found", pathname };
    segs.push(d);
  }
  if (segs.length === 0 || (segs.length === 1 && segs[0] === "workspaces")) return { name: "home" };

  const [area, workspace, page, name, kind, id] = segs;
  if (area === "settings" && workspace === "repositories" && segs.length <= 3)
    return { name: "settings", section: page ?? null };
  if (area !== "workspaces" || !workspace) return { name: "not_found", pathname };
  if (page === "settings" && name === "repositories" && segs.length <= 5)
    return { name: "settings", section: kind ?? null };
  if (segs.length === 3 && page === "task-flow") return { name: "workspace", workspace };
  if (segs.length === 3 && page === "pulls") return { name: "pulls", workspace };
  if (page !== "repos" || !name) return { name: "not_found", pathname };
  const path = `${workspace}/${name}`;
  if (segs.length === 4) return { name: "repo", repo: path };
  if (segs.length === 5 && kind === "settings")
    return { name: "settings", section: null, repo: path };
  if (segs.length !== 6 || !id) return { name: "not_found", pathname };
  switch (kind) {
    // Tickets and pull requests are numbered per repository from 1.
    case "tickets":
    case "pulls": {
      if (!/^[1-9][0-9]*$/.test(id) || !Number.isSafeInteger(Number(id)))
        return { name: "not_found", pathname };
      return kind === "tickets"
        ? { name: "ticket", repo: path, key: id }
        : { name: "pull", repo: path, number: Number(id) };
    }
    case "commits":
      return { name: "commit", repo: path, sha: id };
    case "jobs":
      return { name: "job", repo: path, attemptId: id };
    default:
      return { name: "not_found", pathname };
  }
}

export const workspacePath = (workspace: string) => `/workspaces/${encodeURIComponent(workspace)}`;

export function repositoryPath(path: string): string {
  const [workspace, name] = path.split("/");
  return `${workspacePath(workspace)}/repos/${encodeURIComponent(name)}`;
}

export const POST_MERGE_FRAGMENT = "post-merge-checks";

export const href = {
  home: () => "/workspaces",
  settings: (section?: string, workspace?: string) =>
    (workspace ? workspacePath(workspace) : "") +
    "/settings/repositories" +
    (section ? `/${encodeURIComponent(section)}` : ""),
  repoSettings: (repo: string) => `${repositoryPath(repo)}/settings`,
  workspace: (workspace: string) => `${workspacePath(workspace)}/task-flow`,
  pulls: (workspace: string) => `${workspacePath(workspace)}/pulls`,
  ticket: (repo: string, key: string) => `${repositoryPath(repo)}/tickets/${key}`,
  pull: (repo: string, number: number) => `${repositoryPath(repo)}/pulls/${number}`,
  pullVerification: (repo: string, number: number): string =>
    `${href.pull(repo, number)}#${POST_MERGE_FRAGMENT}`,
  commit: (repo: string, sha: string) =>
    `${repositoryPath(repo)}/commits/${encodeURIComponent(sha)}`,
  job: (repo: string, id: string) => `${repositoryPath(repo)}/jobs/${encodeURIComponent(id)}`,
  repo: repositoryPath,
};

const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

/**
 * Returns the in-app path of a URL the service returned (`url` fields), or
 * null when the URL is not one of this Branvil's web URLs (for example a
 * ticket of another service referenced by a pull request).
 */
export function inAppPath(url: string, origin: string = window.location.origin): string | null {
  let target: URL;
  let here: URL;
  try {
    target = new URL(url, origin);
    here = new URL(origin);
  } catch {
    return null;
  }
  const sameOrigin = target.origin === here.origin;
  // The service's base URL may name 127.0.0.1 while the browser uses localhost.
  const sameLoopback =
    target.protocol === here.protocol &&
    target.port === here.port &&
    LOOPBACK.has(target.hostname) &&
    LOOPBACK.has(here.hostname);
  if (!sameOrigin && !sameLoopback) return null;
  const route = parseRoute(target.pathname);
  if (route.name === "not_found") return null;
  return target.pathname + target.search + target.hash;
}

/** The route a service URL points at, when it is one of this Branvil's. */
export function routeOfUrl(url: string, origin?: string): Route | null {
  const path = inAppPath(url, origin);
  if (path === null) return null;
  return parseRoute(path.split(/[?#]/)[0]);
}
