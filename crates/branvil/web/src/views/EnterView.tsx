import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { isApiError, setCsrfToken } from "../api/client";
import * as api from "../api/endpoints";
import { Button, Card, Stack, Text, Title } from "@mantine/core";
import { ErrorMessage, Loading, Refusal } from "../components/common";
import { useT } from "../i18n";
import { keys } from "../query/keys";
import { navigate } from "../router/Router";
import { href } from "../router/routes";

/**
 * `/-/enter#<code>`: the page a `branvil open` link opens. The code is taken
 * from the fragment, removed from the address bar and history, and exchanged
 * once for the owner's session.
 */
export function EnterView() {
  const t = useT();
  const client = useQueryClient();
  const [code] = useState(() => window.location.hash.slice(1));
  const sent = useRef(false);
  const enter = useMutation({
    mutationFn: () => api.session.enter(code),
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session(), session);
      navigate(takeReturn() ?? href.home(), { replace: true });
    },
  });
  useEffect(() => {
    window.history.replaceState(null, "", window.location.pathname);
    // Once: the code works a single time (StrictMode runs effects twice).
    if (sent.current || !code) return;
    sent.current = true;
    enter.mutate();
  }, [code, enter]);

  if (!code) return <GuidanceView />;
  if (!enter.isError) return <Loading />;
  // 401: the code was used or expired; anything else (service unreachable,
  // wrong Host) leaves the link possibly good, so it can be tried again.
  if (isApiError(enter.error, undefined, 401)) return <GuidanceView notice={t("enter.expired")} />;
  return <GuidanceView error={enter.error} onRetry={() => enter.mutate()} />;
}

const RETURN = "branvil.return";
const RETURN_MS = 10 * 60_000;

/**
 * Remembers this page (the consent page, met without a session) for
 * `/-/enter` to return to instead of the board, for 10 minutes.
 */
export function rememberReturn(): void {
  try {
    const path = window.location.pathname + window.location.search;
    window.localStorage.setItem(RETURN, JSON.stringify({ path, expires: Date.now() + RETURN_MS }));
  } catch {
    // Without storage, entering opens the board; the app can be allowed again.
  }
}

/** The remembered consent page, once, while it has not expired. */
function takeReturn(): string | null {
  try {
    const raw = window.localStorage.getItem(RETURN);
    window.localStorage.removeItem(RETURN);
    const saved = raw ? (JSON.parse(raw) as { path?: unknown; expires?: unknown }) : null;
    const fresh = typeof saved?.expires === "number" && saved.expires > Date.now();
    return fresh && typeof saved.path === "string" && saved.path.startsWith("/-/authorize?") ? saved.path : null;
  } catch {
    return null;
  }
}

/** Without a session every page shows how to get one. */
export function GuidanceView({
  lead,
  notice,
  error,
  onRetry,
}: {
  lead?: string;
  notice?: string;
  error?: unknown;
  onRetry?: () => void;
}) {
  const t = useT();
  return (
    <main className="guide">
      <Card p="lg">
        <Stack gap="md">
          <Title order={2}>Branvil</Title>
          {notice && <Refusal>{notice}</Refusal>}
          {lead && <Text size="sm">{lead}</Text>}
          <Text size="sm">{t("guide.run")}</Text>
          <pre>
            <code>branvil open</code>
          </pre>
          <Text size="sm" c="dimmed">
            {t("guide.why")}
          </Text>
          {error !== undefined && (
            <>
              <ErrorMessage error={error} />
              <div>
                <Button variant="default" onClick={onRetry}>
                  {t("common.retry")}
                </Button>
              </div>
            </>
          )}
        </Stack>
      </Card>
    </main>
  );
}
