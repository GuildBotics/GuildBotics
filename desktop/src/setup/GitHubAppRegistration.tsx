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
};

export function GitHubAppRegistrationPanel({
  defaultAppName,
  defaultOrganization = "",
  onApplied,
  pollIntervalMs = POLL_INTERVAL_MS,
}: Props) {
  const { t } = useTranslation();
  const [appName, setAppName] = useState("");
  const [organization, setOrganization] = useState(defaultOrganization);
  const organizationEditedRef = useRef(false);

  useEffect(() => {
    // Track the project-derived default until the user edits the field; an
    // emptied field must stay empty (= personal account), so a placeholder
    // fallback would be wrong here.
    if (!organizationEditedRef.current) {
      setOrganization(defaultOrganization);
    }
  }, [defaultOrganization]);
  const [registration, setRegistration] = useState<GitHubAppRegistrationStatus | null>(null);
  const appliedStatusRef = useRef("");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

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
    const name = appName.trim() || defaultAppName.trim();
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
          placeholder={defaultAppName}
          onChange={(event) => setAppName(event.currentTarget.value)}
          flex={1}
        />
        <TextInput
          label={t("setup.members.githubAppRegistration.organization")}
          aria-label={t("setup.members.githubAppRegistration.organization")}
          value={organization}
          onChange={(event) => {
            organizationEditedRef.current = true;
            setOrganization(event.currentTarget.value);
          }}
          flex={1}
        />
        <Button
          variant="default"
          loading={starting}
          disabled={!(appName.trim() || defaultAppName.trim())}
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
