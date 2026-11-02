import { Button, Card, Group, Stack, Text, Title } from "@mantine/core";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import * as api from "../api/endpoints";
import { ErrorMessage, Loading, Refusal } from "../components/common";
import { useT } from "../i18n";
import { useSession } from "../query/hooks";
import { leave } from "../router/Router";
import { GuidanceView, rememberReturn } from "./EnterView";

/** A local app's request, from the consent page's query (docs/api.md). */
interface AppRequest {
  client: string;
  redirect_uri: string;
  state: string;
  code_challenge: string;
}

const LOOPBACK = ["127.0.0.1", "localhost", "[::1]"];

/** The request, or the names of the parameters that are not valid. */
export function parseRequest(search: string): AppRequest | { invalid: string[] } {
  const q = new URLSearchParams(search);
  const client = q.get("client") ?? "";
  const redirect = q.get("redirect_uri") ?? "";
  const state = q.get("state") ?? "";
  const challenge = q.get("code_challenge") ?? "";
  let url: URL | null = null;
  try {
    url = new URL(redirect);
  } catch {
    // Not an absolute URL.
  }
  const checks: [string, boolean][] = [
    ["client", /^[A-Za-z0-9 ._-]{1,64}$/.test(client) && client.trim() !== ""],
    [
      "redirect_uri",
      url !== null &&
        url.protocol === "http:" &&
        LOOPBACK.includes(url.hostname) &&
        !url.username &&
        !url.password &&
        !redirect.includes("#"),
    ],
    ["state", state.length >= 1 && state.length <= 512],
    ["code_challenge", /^[A-Za-z0-9_-]{43}$/.test(challenge)],
    ["code_challenge_method", q.get("code_challenge_method") === "S256"],
  ];
  const invalid = checks.filter(([, ok]) => !ok).map(([name]) => name);
  if (invalid.length > 0) return { invalid };
  return { client, redirect_uri: redirect, state, code_challenge: challenge };
}

/**
 * `/-/authorize`: the owner allows (or denies) a local app owner's rights.
 * Allowing sends the browser to the app's loopback address with a single-use
 * code; denying sends it there with `error=access_denied`. Without a session
 * the page says how to enter and is returned to after entering.
 */
export function AuthorizeView() {
  const t = useT();
  const session = useSession();
  const [request] = useState(() => parseRequest(window.location.search));
  const valid = !("invalid" in request);
  const signedOut = session.data === null;
  useEffect(() => {
    if (valid && signedOut) rememberReturn();
  }, [valid, signedOut]);
  const allow = useMutation({
    mutationFn: (r: AppRequest) => api.apps.authorize(r),
    onSuccess: ({ redirect }) => leave(redirect),
  });

  if ("invalid" in request) {
    return (
      <Shell>
        <Refusal>{t("authorize.invalid", { fields: request.invalid.join(", ") })}</Refusal>
      </Shell>
    );
  }
  if (session.isPending) return <Loading />;
  if (session.isError) return <GuidanceView error={session.error} onRetry={() => void session.refetch()} />;
  if (!session.data) return <GuidanceView lead={t("authorize.enterFirst", { client: request.client })} />;

  const deny = () => {
    const url = new URL(request.redirect_uri);
    url.searchParams.append("error", "access_denied");
    url.searchParams.append("state", request.state);
    leave(url.toString());
  };
  const busy = allow.isPending || allow.isSuccess;
  return (
    <Shell>
      <Title order={3}>{t("authorize.title", { client: request.client })}</Title>
      <Text size="sm">{t("authorize.rights")}</Text>
      <Text size="sm" c="dimmed">
        {t("authorize.revoke", { owner: session.data.me.name, client: request.client })}
      </Text>
      <Text size="sm" c="dimmed">
        {t("authorize.returns", { address: new URL(request.redirect_uri).origin })}
      </Text>
      {allow.isError && <ErrorMessage error={allow.error} />}
      <Group>
        <Button onClick={() => allow.mutate(request)} loading={busy}>
          {t("authorize.allow")}
        </Button>
        <Button variant="default" onClick={deny} disabled={busy}>
          {t("authorize.deny")}
        </Button>
      </Group>
    </Shell>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <main className="guide">
      <Card p="lg">
        <Stack gap="md">
          <Title order={2}>Branvil</Title>
          {children}
        </Stack>
      </Card>
    </main>
  );
}
