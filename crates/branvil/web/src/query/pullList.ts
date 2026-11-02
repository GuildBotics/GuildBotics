import { useInfiniteQuery } from "@tanstack/react-query";
import * as api from "../api/endpoints";
import type { PullRequest } from "../api/types";
import { keys } from "./keys";

const PAGE_SIZE = 30;
type Source = { repo: string; items: PullRequest[]; cursor: string | null | undefined };

/** Same order as each repository's API; repository paths break cross-repo ties. */
function compare(a: PullRequest, b: PullRequest) {
  return a.updated_at === b.updated_at
    ? a.repository.localeCompare(b.repository) || b.number - a.number
    : a.updated_at > b.updated_at ? -1 : 1;
}

/** Merge sorted streams, keeping unused rows for the next workspace page. */
async function nextPage(sources: Source[], state: "open" | "closed" | "all") {
  const remaining = sources.map((s) => ({ ...s, items: [...s.items] }));
  const items: PullRequest[] = [];
  while (items.length < PAGE_SIZE) {
    // An empty source must be refilled before picking the next newest row.
    await Promise.all(remaining.map(async (s) => {
      if (s.items.length === 0 && s.cursor !== null) {
        const page = await api.repos.pulls(s.repo, state, s.cursor);
        s.items = page.items;
        s.cursor = page.next_cursor;
      }
    }));
    const available = remaining.filter((s) => s.items.length > 0);
    if (available.length === 0) break;
    available.sort((a, b) => compare(a.items[0]!, b.items[0]!));
    items.push(available[0]!.items.shift()!);
  }
  return { items, next: remaining.some((s) => s.items.length > 0 || s.cursor !== null) ? remaining : undefined };
}

export function usePullList(paths: string[], state: "open" | "closed" | "all", enabled: boolean) {
  return useInfiniteQuery({
    queryKey: keys.pullList(paths, state),
    initialPageParam: paths.map((repo): Source => ({ repo, items: [], cursor: undefined })),
    queryFn: ({ pageParam }) => nextPage(pageParam, state),
    getNextPageParam: (last) => last.next,
    enabled,
  });
}
