import { Button, Group, Stack, Text, TextInput } from "@mantine/core";
import type { TFunction } from "i18next";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiRequestError,
  getGitHubAppRegistration,
  startGitHubAppRegistration,
  type GitHubAppRegistrationStatus,
} from "../api/client";
import { openExternal } from "../openExternal";

const POLL_INTERVAL_MS = 3000;

// Mirrors GITHUB_APP_NAME_MAX_LENGTH in editions/simple/github_app_setup.py.
const APP_NAME_MAX_LENGTH = 34;

/** Suggest an app name that is unlikely to collide and short enough to submit.
 *
 * GitHub App names are unique across all of GitHub, so a bare member ID
 * collides easily; qualifying it with the organization makes the name specific
 * to this installation. The join can outgrow the name limit, and a suggestion
 * the backend is guaranteed to reject is worse than a shortened one.
 */
function composeAppName(memberId: string, organization: string): string {
  return [memberId.trim(), organization.trim()]
    .filter(Boolean)
    .join("-")
    .slice(0, APP_NAME_MAX_LENGTH)
    .replace(/-+$/, "");
}

export type GitHubAppRegistrationFields = {
  githubUsername?: string;
  gitEmail?: string;
  appId?: string;
  privateKeyPath?: string;
  installationId?: string;
};

export function getRegistrationErrorMessage(
  error: unknown,
  t: TFunction | ((key: string) => string),
): string {
  if (error instanceof ApiRequestError) {
    if (error.code === "invalid_github_app_name") {
      return t("setup.members.githubAppRegistration.errors.invalidAppName");
    }
    if (error.code === "github_app_registration_not_found") {
      return t("setup.members.githubAppRegistration.errors.expired");
    }
  }
  return error instanceof Error ? error.message : String(error);
}

function toFields(registration: GitHubAppRegistrationStatus): GitHubAppRegistrationFields {
  const fields: GitHubAppRegistrationFields = {};
  if (registration.github_username) {
    fields.githubUsername = registration.github_username;
  }
  if (registration.git_email) {
    fields.gitEmail = registration.git_email;
  }
  if (registration.app_id !== null) {
    fields.appId = String(registration.app_id);
  }
  if (registration.private_key_path) {
    fields.privateKeyPath = registration.private_key_path;
  }
  if (registration.installation_id !== null) {
    fields.installationId = String(registration.installation_id);
  }
  return fields;
}

type Props = {
  defaultAppName: string;
  defaultOrganization?: string;
  onApplied: (fields: GitHubAppRegistrationFields) => void;
  pollIntervalMs?: number;
  // Identity of the member edit session; a change means the surrounding form
  // now describes a different member.
  memberKey?: string;
};

export function GitHubAppRegistrationPanel({
  defaultAppName,
  defaultOrganization = "",
  onApplied,
  pollIntervalMs = POLL_INTERVAL_MS,
  memberKey = "",
}: Props) {
  const { t } = useTranslation();
  // Both fields follow their derived default until the user types into them,
  // so the override is what gets stored. An emptied organization must stay
  // empty (= personal account), which "" as an override expresses and a
  // fallback would not.
  const [appNameOverride, setAppNameOverride] = useState<string | null>(null);
  const [organizationOverride, setOrganizationOverride] = useState<string | null>(null);
  const [registration, setRegistration] = useState<GitHubAppRegistrationStatus | null>(null);
  const appliedStatusRef = useRef("");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [appliedMemberKey, setAppliedMemberKey] = useState(memberKey);

  const organization = organizationOverride ?? defaultOrganization;
  const appName = appNameOverride ?? composeAppName(defaultAppName, organization);
  const appNameError = appName.trim().length > APP_NAME_MAX_LENGTH;

  // The form is reused for the next member, so nothing about the previous one
  // may survive: not the hand-edited fields, and not a registration whose
  // polling and links belong to the previous member's app. Adjusting during
  // render (rather than in an effect) keeps the stale values from ever being
  // painted. The dedupe marker needs no reset here: handleStart clears it, and
  // dropping the registration already stops the poll that reads it.
  if (memberKey !== appliedMemberKey) {
    setAppliedMemberKey(memberKey);
    setAppNameOverride(null);
    setOrganizationOverride(null);
    setRegistration(null);
    setError("");
  }

  const applyRegistration = useCallback(
    (next: GitHubAppRegistrationStatus) => {
      setRegistration((current) =>
        // The start response carries the start_url; polling responses do not.
        current && current.state === next.state && !next.start_url
          ? { ...next, start_url: current.start_url }
          : next,
      );
      if (next.status !== "pending" && appliedStatusRef.current !== next.status) {
        appliedStatusRef.current = next.status;
        onApplied(toFields(next));
      }
    },
    [onApplied],
  );

  const handleStart = async () => {
    const name = appName.trim();
    if (!name) {
      return;
    }
    setStarting(true);
    setError("");
    appliedStatusRef.current = "";
    try {
      const started = await startGitHubAppRegistration({
        app_name: name,
        organization: organization.trim(),
      });
      setRegistration(started);
      await openExternal(started.start_url);
    } catch (startError) {
      setError(getRegistrationErrorMessage(startError, t));
    } finally {
      setStarting(false);
    }
  };

  const registrationState = registration?.state ?? "";
  const registrationStatus = registration?.status ?? "";

  useEffect(() => {
    if (!registrationState || registrationStatus === "installed") {
      return;
    }
    // Chain timeouts instead of using an interval so a slow GitHub round trip
    // never overlaps with the next poll.
    let cancelled = false;
    let timer = 0;
    const scheduleNext = () => {
      timer = window.setTimeout(() => {
        getGitHubAppRegistration(registrationState).then(
          (next) => {
            if (cancelled) {
              return;
            }
            applyRegistration(next);
            scheduleNext();
          },
          (pollError) => {
            if (cancelled) {
              return;
            }
            if (pollError instanceof ApiRequestError) {
              setError(getRegistrationErrorMessage(pollError, t));
              setRegistration(null);
              return;
            }
            // Transient network failure: keep polling.
            scheduleNext();
          },
        );
      }, pollIntervalMs);
    };
    scheduleNext();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [registrationState, registrationStatus, applyRegistration, pollIntervalMs, t]);

  return (
    <Stack gap="xs" className="github-app-registration">
      <Text c="dimmed" size="xs">
        {t("setup.members.githubAppRegistration.hint")}
      </Text>
      <Group align="end">
        <TextInput
          label={t("setup.members.githubAppRegistration.appName")}
          aria-label={t("setup.members.githubAppRegistration.appName")}
          value={appName}
          onChange={(event) => setAppNameOverride(event.currentTarget.value)}
          error={
            appNameError ? t("setup.members.githubAppRegistration.errors.invalidAppName") : null
          }
          flex={1}
        />
        <TextInput
          label={t("setup.members.githubAppRegistration.organization")}
          aria-label={t("setup.members.githubAppRegistration.organization")}
          value={organization}
          onChange={(event) => setOrganizationOverride(event.currentTarget.value)}
          flex={1}
        />
        <Button
          variant="default"
          loading={starting}
          disabled={!appName.trim() || appNameError}
          onClick={() => void handleStart()}
        >
          {t("setup.members.githubAppRegistration.register")}
        </Button>
      </Group>
      {error ? (
        <Text c="danger" size="xs">
          {error}
        </Text>
      ) : null}
      {registrationStatus === "pending" ? (
        <Group gap="xs">
          <Text size="sm">{t("setup.members.githubAppRegistration.pending")}</Text>
          <Button
            variant="subtle"
            size="compact-sm"
            onClick={() => void openExternal(registration?.start_url ?? "")}
          >
            {t("setup.members.githubAppRegistration.reopen")}
          </Button>
        </Group>
      ) : null}
      {registrationStatus === "converted" ? (
        <Stack gap={4}>
          <Group gap="xs">
            <Text size="sm">
              {t("setup.members.githubAppRegistration.converted", {
                slug: registration?.slug ?? "",
              })}
            </Text>
            <Button
              variant="subtle"
              size="compact-sm"
              onClick={() => void openExternal(registration?.installation_page_url ?? "")}
            >
              {t("setup.members.githubAppRegistration.openInstall")}
            </Button>
          </Group>
          {registration?.installation_check_error ? (
            <Text c="danger" size="xs">
              {t("setup.members.githubAppRegistration.installCheckError", {
                message: registration.installation_check_error,
              })}
            </Text>
          ) : null}
        </Stack>
      ) : null}
      {registrationStatus === "installed" ? (
        <Text size="sm" c="teal">
          {t("setup.members.githubAppRegistration.installed")}
        </Text>
      ) : null}
    </Stack>
  );
}
