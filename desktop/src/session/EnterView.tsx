import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiRequestError, enterSession } from "../api/client";
import { samePagePath, takeReturn } from "./entryPages";
import { Connecting, NoSessionGuide } from "./Guidance";
import { replacePage } from "./pageNavigation";

/**
 * `/-/enter?next=<page>#<code>`: the page an entry link opens. The code is
 * taken from the fragment, removed from the address bar and history, and
 * exchanged once for the session; then the page goes to `next`, the consent
 * page that sent the owner here, or the App.
 */
export function EnterView() {
  const { t } = useTranslation();
  const [{ code, next }] = useState(() => ({
    code: window.location.hash.slice(1),
    next: samePagePath(new URLSearchParams(window.location.search).get("next")),
  }));
  useEffect(() => window.history.replaceState(null, "", window.location.pathname), []);
  // A query, not a mutation: the code works a single time, and every mount of
  // this page (StrictMode mounts it twice) shares the one exchange.
  const entering = useQuery({
    queryKey: ["enter", code],
    queryFn: async () => {
      await enterSession(code);
      replacePage(next ?? takeReturn() ?? "/");
      return true;
    },
    enabled: code !== "",
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  if (!code) {
    return <NoSessionGuide />;
  }
  if (!entering.isError) {
    return <Connecting />;
  }
  // The code was used or expired; anything else (the server unreachable)
  // leaves the link possibly good, so it can be tried again.
  if (entering.error instanceof ApiRequestError && entering.error.code === "unauthorized") {
    return <NoSessionGuide notice={t("session.guide.expired")} />;
  }
  return <NoSessionGuide error={entering.error} onRetry={() => void entering.refetch()} />;
}
