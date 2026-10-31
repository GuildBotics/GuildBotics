import { Alert, Button, Group, Text } from "@mantine/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { authorizeApp } from "../api/client";
import { deniedRedirect, parseAppRequest, rememberReturn, SESSION_QUERY } from "./entryPages";
import { Connecting, FailureAlert, GuideCard, NoSessionGuide } from "./Guidance";
import { leavePage } from "./pageNavigation";

/**
 * `/-/authorize`: the owner allows (or denies) a local app, such as the CLI of
 * another machine, the owner's rights. Allowing sends the browser to the app's
 * loopback address with a single-use code; denying sends it there with
 * `error=access_denied`. Without a session the page says how to enter, and
 * entering returns here.
 */
export function AuthorizeView() {
  const { t } = useTranslation();
  const [request] = useState(() => parseAppRequest(window.location.search));
  const valid = !("invalid" in request);
  const session = useQuery({ ...SESSION_QUERY, enabled: valid });
  const signedOut = session.data === false;
  useEffect(() => {
    if (signedOut) {
      rememberReturn();
    }
  }, [signedOut]);
  const allow = useMutation({
    mutationFn: authorizeApp,
    onSuccess: ({ redirect }) => leavePage(redirect),
  });

  if ("invalid" in request) {
    return (
      <GuideCard title="GuildBotics">
        <Alert color="danger">
          {t("session.authorize.invalid", { fields: request.invalid.join(", ") })}
        </Alert>
      </GuideCard>
    );
  }
  if (session.isPending) {
    return <Connecting />;
  }
  if (session.isError) {
    return <NoSessionGuide error={session.error} onRetry={() => void session.refetch()} />;
  }
  if (signedOut) {
    return <NoSessionGuide lead={t("session.authorize.enterFirst", { client: request.client })} />;
  }

  const busy = allow.isPending || allow.isSuccess;
  return (
    <GuideCard title={t("session.authorize.title", { client: request.client })}>
      <Text size="sm">{t("session.authorize.rights", { client: request.client })}</Text>
      <Text size="sm" c="dimmed">
        {t("session.authorize.returns", { address: new URL(request.redirect_uri).origin })}
      </Text>
      {allow.isError ? <FailureAlert error={allow.error} /> : null}
      <Group>
        <Button onClick={() => allow.mutate(request)} loading={busy}>
          {t("session.authorize.allow")}
        </Button>
        <Button
          variant="default"
          onClick={() => leavePage(deniedRedirect(request))}
          disabled={busy}
        >
          {t("session.authorize.deny")}
        </Button>
      </Group>
    </GuideCard>
  );
}
