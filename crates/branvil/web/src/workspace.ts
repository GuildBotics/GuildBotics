// The workspace the GUI shows: the one the address names, else the one last
// shown on this browser, else the first. GuildBotics Desktop will fix it to its
// own workspace's.

import { useEffect, useMemo } from "react";
import { useWorkspaces } from "./query/hooks";
import type { Route } from "./router/routes";
import { useStored } from "./storage";

const REMEMBERED = "branvil.workspace";

/** The workspace a route belongs to, if it names one. */
export function workspaceOfRoute(route: Route): string | null {
  switch (route.name) {
    case "workspace":
    case "pulls":
    case "board":
      return route.workspace;
    case "repo":
    case "ticket":
    case "pull":
    case "commit":
    case "job":
      return route.repo.split("/")[0] ?? null;
    case "settings":
      return route.repo?.split("/")[0] ?? null;
    default:
      return null;
  }
}

/**
 * The shown workspace and the list it was picked from (null while the list
 * loads; `name` is null when there is none).
 */
export function useCurrentWorkspace(route: Route): { name: string | null; names: string[] | null; select: (name: string) => void } {
  const workspaces = useWorkspaces();
  const [last, select] = useStored(REMEMBERED);
  const fromRoute = workspaceOfRoute(route);
  const names = useMemo(() => (workspaces.data ? workspaces.data.items.map((w) => w.name) : null), [workspaces.data]);
  useEffect(() => {
    if (fromRoute) select(fromRoute);
  }, [fromRoute, select]);
  if (fromRoute) return { name: fromRoute, names, select };
  if (!names) return { name: null, names, select };
  return { name: names.find((n) => n === last) ?? names[0] ?? null, names, select };
}
