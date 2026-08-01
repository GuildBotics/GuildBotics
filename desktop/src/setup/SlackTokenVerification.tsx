import { Button, Group, Stack, Text } from "@mantine/core";
import type { TFunction } from "i18next";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  verifySlackTokens,
  type SlackChannelVerification,
  type SlackTokenSource,
  type SlackTokenVerifyResponse,
} from "../api/client";

// Slack error codes the GUI explains in its own words; anything else is shown
// with the raw code so an unexpected Slack failure is still actionable.
const KNOWN_ERROR_CODES = ["missing", "wrong_token_type", "unreachable", "invalid_auth"];

type Translate = TFunction | ((key: string, options?: Record<string, unknown>) => string);

export function getSlackTokenErrorMessage(code: string, t: Translate): string {
  if (KNOWN_ERROR_CODES.includes(code)) {
    return t(`setup.members.slackTokenVerify.errors.${code}`);
  }
  return t("setup.members.slackTokenVerify.errors.unknown", { code });
}

/** Name the token that was checked, so "OK" on an empty field is never a mystery. */
export function getSlackTokenLabel(
  token: "bot" | "appToken",
  source: SlackTokenSource,
  t: Translate,
): string {
  const suffix = source === "stored" ? "Stored" : "";
  return t(`setup.members.slackTokenVerify.labels.${token}${suffix}`);
}

type Props = {
  botToken: string;
  appToken: string;
  personId?: string;
  channels?: string[];
};

export function SlackTokenVerificationPanel({
  botToken,
  appToken,
  personId = "",
  channels = [],
}: Props) {
  const { t } = useTranslation();
  const [result, setResult] = useState<SlackTokenVerifyResponse | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState("");
  // What the displayed verdict describes, compared during render so an edit
  // retires it instead of leaving a stale green line behind.
  const [describedTokens, setDescribedTokens] = useState({ botToken, appToken });
  // The same values readable from an async closure, which would otherwise see
  // the tokens as they were when the request started.
  const currentTokensRef = useRef({ botToken, appToken });

  useEffect(() => {
    currentTokensRef.current = { botToken, appToken };
  }, [botToken, appToken]);

  if (describedTokens.botToken !== botToken || describedTokens.appToken !== appToken) {
    setDescribedTokens({ botToken, appToken });
    setResult(null);
    setError("");
  }

  const isCurrent = (requested: { botToken: string; appToken: string }) =>
    currentTokensRef.current.botToken === requested.botToken &&
    currentTokensRef.current.appToken === requested.appToken;

  const handleVerify = async () => {
    const requested = { botToken, appToken };
    setVerifying(true);
    setError("");
    setResult(null);
    try {
      const verification = await verifySlackTokens({
        bot_token: requested.botToken,
        app_token: requested.appToken,
        person_id: personId,
        channels,
      });
      // The tokens were edited while the request was in flight; its verdict no
      // longer describes what is in the fields.
      if (isCurrent(requested)) {
        setResult(verification);
      }
    } catch (verifyError) {
      if (isCurrent(requested)) {
        setError(verifyError instanceof Error ? verifyError.message : String(verifyError));
      }
    } finally {
      setVerifying(false);
    }
  };

  const botDetail = (verification: SlackTokenVerifyResponse) => {
    if (!verification.bot_ok) {
      return getSlackTokenErrorMessage(verification.bot_error, t);
    }
    return t(
      verification.workspace
        ? "setup.members.slackTokenVerify.ok.bot"
        : "setup.members.slackTokenVerify.ok.botNoWorkspace",
      {
        displayName: verification.bot_display_name,
        userId: verification.bot_user_id,
        workspace: verification.workspace,
      },
    );
  };

  // No verdict at all (a backend that predates the scope probe) must read as
  // "not checked", never as a failure with an empty reason.
  const hasScopeVerdict = (verification: SlackTokenVerifyResponse) =>
    verification.bot_ok && (verification.scopes_ok || Boolean(verification.scope_error));

  const scopeDetail = (verification: SlackTokenVerifyResponse) => {
    if (verification.scopes_ok) {
      return t("setup.members.slackTokenVerify.ok.scopes");
    }
    if (verification.scope_error !== "missing_scope") {
      return getSlackTokenErrorMessage(verification.scope_error, t);
    }
    return verification.scope_needed
      ? t("setup.members.slackTokenVerify.scopeMissing", { needed: verification.scope_needed })
      : t("setup.members.slackTokenVerify.scopeMissingUnnamed");
  };

  const channelDetail = (channel: SlackChannelVerification) => {
    if (channel.ok) {
      return t("setup.members.slackTokenVerify.ok.channel");
    }
    if (channel.error === "not_in_channel") {
      return t("setup.members.slackTokenVerify.channelNotJoined", {
        botName: result?.bot_display_name ?? "",
      });
    }
    if (channel.error === "not_found") {
      return t("setup.members.slackTokenVerify.channelNotFound");
    }
    return getSlackTokenErrorMessage(channel.error, t);
  };

  return (
    <Stack gap={4} className="slack-token-verification">
      <Group>
        <Button variant="default" loading={verifying} onClick={() => void handleVerify()}>
          {t("setup.members.slackTokenVerify.button")}
        </Button>
      </Group>
      {error ? (
        <Text c="danger" size="xs">
          {error}
        </Text>
      ) : null}
      {result ? (
        <Stack gap={2}>
          <Text c={result.bot_ok ? "teal" : "danger"} size="xs">
            {`${getSlackTokenLabel("bot", result.bot_source, t)}: ${botDetail(result)}`}
          </Text>
          {hasScopeVerdict(result) ? (
            <Text c={result.scopes_ok ? "teal" : "danger"} size="xs">
              {`${t("setup.members.slackTokenVerify.labels.scopes")}: ${scopeDetail(result)}`}
            </Text>
          ) : null}
          <Text c={result.app_token_ok ? "teal" : "danger"} size="xs">
            {`${getSlackTokenLabel("appToken", result.app_token_source, t)}: ${
              result.app_token_ok
                ? t("setup.members.slackTokenVerify.ok.appToken")
                : getSlackTokenErrorMessage(result.app_token_error, t)
            }`}
          </Text>
          {(result.channels ?? []).map((channel) => (
            <Text key={channel.channel} c={channel.ok ? "teal" : "danger"} size="xs">
              {`#${channel.channel.replace(/^#/, "")}: ${channelDetail(channel)}`}
            </Text>
          ))}
        </Stack>
      ) : null}
    </Stack>
  );
}
