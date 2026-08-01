import { Button, Group, List, Stack, Text, TextInput } from "@mantine/core";
import type { TFunction } from "i18next";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiRequestError,
  startSlackAppRegistration,
  type SlackAppRegistrationStatus,
} from "../api/client";
import { openExternal } from "../openExternal";

// Slack applies the manifest's scopes to the app, but the bot token it hands
// out on the confirmation screen is issued without them (verified on a freshly
// created app). Reinstalling is what mints a token that actually carries the
// scopes, so it is a required step rather than a remedy.
const STEP_KEYS = ["create", "reinstall", "copyBot", "copyAppToken", "paste"] as const;

// Mirrors APP_NAME_MAX_LENGTH in integrations/slack/app_manifest.py.
const APP_NAME_MAX_LENGTH = 35;

export function getSlackRegistrationErrorMessage(
  error: unknown,
  t: TFunction | ((key: string) => string),
): string {
  if (error instanceof ApiRequestError && error.code === "invalid_slack_app_name") {
    return t("setup.members.slackAppRegistration.errors.invalidAppName");
  }
  return error instanceof Error ? error.message : String(error);
}

type Props = {
  defaultAppName: string;
  // Identity of the member edit session; a change means the surrounding form
  // now describes a different member.
  memberKey?: string;
};

export function SlackAppRegistrationPanel({ defaultAppName, memberKey = "" }: Props) {
  const { t } = useTranslation();
  // The field follows the member ID until the user types into it, so the name
  // that will actually be sent is visible rather than hinted at.
  const [appNameOverride, setAppNameOverride] = useState<string | null>(null);
  const [registration, setRegistration] = useState<SlackAppRegistrationStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [appliedMemberKey, setAppliedMemberKey] = useState(memberKey);

  const appName = appNameOverride ?? defaultAppName.trim().slice(0, APP_NAME_MAX_LENGTH);
  const appNameError = appName.trim().length > APP_NAME_MAX_LENGTH;

  // The form is reused for the next member, so nothing about the previous one
  // may survive: not the hand-edited name, and not a registration whose links
  // point at the previous member's manifest. Adjusting during render (rather
  // than in an effect) keeps the stale values from ever being painted.
  if (memberKey !== appliedMemberKey) {
    setAppliedMemberKey(memberKey);
    setAppNameOverride(null);
    setRegistration(null);
    setError("");
  }

  const handleStart = async () => {
    const name = appName.trim();
    if (!name) {
      return;
    }
    setStarting(true);
    setError("");
    try {
      const started = await startSlackAppRegistration({ app_name: name });
      setRegistration(started);
      await openExternal(started.registration_url);
    } catch (startError) {
      setError(getSlackRegistrationErrorMessage(startError, t));
    } finally {
      setStarting(false);
    }
  };

  return (
    <Stack gap="xs" className="slack-app-registration">
      <Text c="dimmed" size="xs">
        {t("setup.members.slackAppRegistration.hint")}
      </Text>
      <Group align="end">
        <TextInput
          label={t("setup.members.slackAppRegistration.appName")}
          aria-label={t("setup.members.slackAppRegistration.appName")}
          value={appName}
          onChange={(event) => setAppNameOverride(event.currentTarget.value)}
          error={
            appNameError ? t("setup.members.slackAppRegistration.errors.invalidAppName") : null
          }
          flex={1}
        />
        <Button
          variant="default"
          loading={starting}
          disabled={!appName.trim() || appNameError}
          onClick={() => void handleStart()}
        >
          {t("setup.members.slackAppRegistration.register")}
        </Button>
      </Group>
      {error ? (
        <Text c="danger" size="xs">
          {error}
        </Text>
      ) : null}
      {registration ? (
        <Stack gap={4}>
          <Text size="sm">{t("setup.members.slackAppRegistration.stepsTitle")}</Text>
          <List type="ordered" size="sm" withPadding>
            {STEP_KEYS.map((step) => (
              <List.Item key={step}>
                {t(`setup.members.slackAppRegistration.steps.${step}`)}
              </List.Item>
            ))}
          </List>
          <Text c="dimmed" size="xs">
            {t("setup.members.slackAppRegistration.signedOutHint")}
          </Text>
          <Group gap="xs">
            <Button
              variant="subtle"
              size="compact-sm"
              onClick={() => void openExternal(registration.registration_url)}
            >
              {t("setup.members.slackAppRegistration.reopen")}
            </Button>
            <Button
              variant="subtle"
              size="compact-sm"
              onClick={() => void openExternal(registration.app_directory_url)}
            >
              {t("setup.members.slackAppRegistration.openAppList")}
            </Button>
          </Group>
        </Stack>
      ) : null}
    </Stack>
  );
}
