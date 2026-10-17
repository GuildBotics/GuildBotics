import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { getWorkspaceSyncStatus, retryWorkspaceSync, type SyncScope } from "../api/client";
import {
  SYNC_REFETCH_MS,
  SYNC_SCOPES,
  syncIndicatorState,
  syncQueryKey,
  type ScopedSyncStatus,
} from "./syncState";

const syncStatusQuery = (scope: SyncScope) => ({
  queryKey: syncQueryKey(scope),
  queryFn: () => getWorkspaceSyncStatus(scope),
  refetchInterval: SYNC_REFETCH_MS,
});

/** One scope's synchronization state. */
export function useSyncStatus(scope: SyncScope) {
  return useQuery(syncStatusQuery(scope));
}

/** Both scopes' synchronization states, identities first. */
export function useSyncStatuses(): ScopedSyncStatus[] {
  const results = useQueries({ queries: SYNC_SCOPES.map(syncStatusQuery) });
  return SYNC_SCOPES.map((scope, index) => {
    const status = results[index]?.data;
    return { scope, status, state: syncIndicatorState(status) };
  });
}

/** Another attempt for each of the given scopes, answered with that attempt. */
export function useSyncRetry() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (scopes: SyncScope[]) =>
      Promise.all(scopes.map((scope) => retryWorkspaceSync(scope))),
    onSuccess: (results, scopes) => {
      scopes.forEach((scope, index) =>
        queryClient.setQueryData(syncQueryKey(scope), results[index]),
      );
    },
  });
}
