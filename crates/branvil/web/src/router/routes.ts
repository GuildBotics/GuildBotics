// Client-side routes. The permanent web URLs of docs/api.md are
// `/{repo}/-/{kind}/{id}` where `{repo}` is `<workspace>/<name>`; a workspace's
// page (its task flow) is `/{workspace}`, its board `/{workspace}/-/board`,
// its pull requests `/{workspace}/-/pulls`,
// and a repository's page `/{repo}` lists that repository's pull requests;
// app routes live under `/-/` (`/-/settings/repos/{repo}` opens a repository's
// editor in its workspace's settings).

export type Route =
  | { name: "home" }
  | { name: "enter" }
  | { name: "authorize" }
  | { name: "settings"; section: string | null; repo?: string }
  | { name: "workspace"; workspace: string }
  | { name: "pulls"; workspace: string }
  | { name: "board"; workspace: string }
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
    segs.push(d);
  }
  if (segs.length === 0) return { name: "home" };

  if (segs[0] === "-") {
    const [, page, ...rest] = segs;
    if (page === "enter" && rest.length === 0) return { name: "enter" };
    if (page === "authorize" && rest.length === 0) return { name: "authorize" };
    if (page === "settings" && rest.length <= 1) return { name: "settings", section: rest[0] ?? null };
    if (page === "settings" && rest[0] === "repos" && rest.length === 3) return { name: "settings", section: null, repo: rest.slice(1).join("/") };
    return { name: "not_found", pathname };
  }

  const dash = segs.indexOf("-");
  if (dash === 1 && segs.length === 3 && (segs[2] === "pulls" || segs[2] === "board"))
    return { name: segs[2], workspace: segs[0] as string };
  if (dash === -1) {
    if (segs.length === 1) return { name: "workspace", workspace: segs[0] as string };
    return segs.length === 2 ? { name: "repo", repo: segs.join("/") } : { name: "not_found", pathname };
  }
  if (dash !== 2 || segs.length !== 5) return { name: "not_found", pathname };
  const path = segs.slice(0, dash).join("/");
  const kind = segs[dash + 1];
  const id = segs[dash + 2] as string;
  switch (kind) {
    // Tickets and pull requests are numbered per repository from 1.
    case "tickets":
    case "pulls": {
      if (!/^[1-9][0-9]*$/.test(id)) return { name: "not_found", pathname };
      return kind === "tickets" ? { name: "ticket", repo: path, key: id } : { name: "pull", repo: path, number: Number(id) };
    }
    case "commits":
      return { name: "commit", repo: path, sha: id };
    case "jobs":
      return { name: "job", repo: path, attemptId: id };
    default:
      return { name: "not_found", pathname };
  }
}

const joinPath = (path: string) => path.split("/").map(encodeURIComponent).join("/");

export const href = {
  home: () => "/",
  settings: (section?: string) => (section ? `/-/settings/${encodeURIComponent(section)}` : "/-/settings"),
  repoSettings: (repo: string) => `/-/settings/repos/${joinPath(repo)}`,
  workspace: (workspace: string) => `/${encodeURIComponent(workspace)}`,
  pulls: (workspace: string) => `/${encodeURIComponent(workspace)}/-/pulls`,
  board: (workspace: string) => `/${encodeURIComponent(workspace)}/-/board`,
  ticket: (repo: string, key: string) => `/${joinPath(repo)}/-/tickets/${key}`,
  pull: (repo: string, number: number) => `/${joinPath(repo)}/-/pulls/${number}`,
  commit: (repo: string, sha: string) => `/${joinPath(repo)}/-/commits/${encodeURIComponent(sha)}`,
  job: (repo: string, id: string) => `/${joinPath(repo)}/-/jobs/${encodeURIComponent(id)}`,
  repo: (repo: string) => `/${joinPath(repo)}`,
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
  return target.pathname + target.hash;
}

/** The route a service URL points at, when it is one of this Branvil's. */
export function routeOfUrl(url: string, origin?: string): Route | null {
  const path = inAppPath(url, origin);
  if (path === null) return null;
  return parseRoute(path.split("#")[0] as string);
}
