import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import { onConnectionLost, type ConnectionLoss } from "../api/client";
import { AuthorizeView } from "./AuthorizeView";
import { EnterView } from "./EnterView";
import { AUTHORIZE_PATH, ENTER_PATH, SESSION_QUERY } from "./entryPages";
import { Connecting, NoSessionGuide, UnreachableNotice } from "./Guidance";

/**
 * A page the server serves: the entry and consent pages by their path, and
 * otherwise `children` (the App) once this browser's session is taken up.
 */
export function EntryPage({ children }: { children: ReactNode }) {
  const [page] = useState(() => window.location.pathname);
  if (page === ENTER_PATH) {
    return <EnterView />;
  }
  if (page === AUTHORIZE_PATH) {
    return <AuthorizeView />;
  }
  return <AppSession>{children}</AppSession>;
}

/**
 * Show `children` while the session holds. A request that finds it gone shows
 * how to enter again. A request that finds the server unreachable keeps the
 * screen under a notice until a retry reaches it, and then reloads what it
 * shows.
 */
function AppSession({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const session = useQuery(SESSION_QUERY);
  const [lost, setLost] = useState<ConnectionLoss | null>(null);
  // A session found gone stays gone: a later failure to reach the server
  // does not bring the screen back.
  useEffect(
    () =>
      onConnectionLost((loss) =>
        setLost((previous) => (previous === "signed_out" ? previous : loss)),
      ),
    [],
  );

  if (lost === "signed_out" || session.data === false) {
    return <NoSessionGuide />;
  }
  const retry = async () => {
    if ((await session.refetch()).data) {
      setLost(null);
      void queryClient.invalidateQueries();
    }
  };
  const notice =
    lost === "unreachable" || session.isError ? (
      <UnreachableNotice retrying={session.isFetching} onRetry={() => void retry()} />
    ) : null;
  if (!session.data) {
    return notice ?? <Connecting />;
  }
  return (
    <>
      {children}
      {notice}
    </>
  );
}
