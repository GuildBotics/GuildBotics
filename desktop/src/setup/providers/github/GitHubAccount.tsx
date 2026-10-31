import {
  Button,
  FileInput,
  PasswordInput,
  SegmentedControl,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import type { TFunction } from "i18next";
import { FileKey } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiRequestError } from "../../../api/client";
import { SecretStatusHint } from "../../../sync/SecretStatusHint";
import { RequiredLabel } from "../../formParts";
import { MASKED_SECRET_PLACEHOLDER } from "../../secretInput";
import type { FieldErrors, MemberAccountUi, MemberContext } from "../types";
import { ensureAgentField, resolveUser, type AccountView, type AppRegistrationStatus } from "./api";
import { agentFieldQueryKey, boardTarget, parseProjectUrl } from "./GitHubBoard";
import {
  GitHubAppRegistrationPanel,
  githubAppSaveFailure,
  type GitHubAppSaveOutcome,
} from "./GitHubAppRegistration";

type Translate = TFunction | ((key: string) => string);

const ACCOUNT_TYPES = ["none", "human", "machine_user", "github_apps"] as const;
type AccountType = (typeof ACCOUNT_TYPES)[number];

export type GitHubForm = {
  accountType: AccountType;
  /** A GitHub App's settings URL, which names the app to resolve. */
  identity: string;
  username: string;
  accessToken: string;
  installationId: string;
  appId: string;
  /** The private key file chosen in this form, with its PEM text: the save
   * sends the text, since the backend cannot read a file of the browser's. */
  privateKey: { file: File; pem: string } | null;
  /** A GitHub App registered from this form: once installed, the save names it
   * and the backend stores its key and IDs, so neither passes through here. */
  registration: AppRegistrationStatus | null;
  saveOutcome: GitHubAppSaveOutcome | null;
  /** Register a new app on GitHub, or name an already-registered one. */
  setupMode: "create" | "existing";
  resolveError: string;
  stored: { privateKey: boolean; accessToken: boolean };
};

const initial = (): GitHubForm => ({
  accountType: "none",
  identity: "",
  username: "",
  accessToken: "",
  installationId: "",
  appId: "",
  privateKey: null,
  registration: null,
  saveOutcome: null,
  setupMode: "create",
  resolveError: "",
  stored: { privateKey: false, accessToken: false },
});

function toAccountType(value: string | null): AccountType {
  return ACCOUNT_TYPES.includes(value as AccountType) ? (value as AccountType) : "none";
}

function isAppUrl(value: string): boolean {
  return /^https:\/\/github\.com\/organizations\/[^/]+\/settings\/apps\/[^/]+/.test(value.trim());
}

function isUsername(value: string): boolean {
  const username = value.trim();
  return (
    /^[A-Za-z0-9-]{1,39}$/.test(username) && !username.startsWith("-") && !username.endsWith("-")
  );
}

function isAccessToken(value: string): boolean {
  return /^(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})$/.test(value.trim());
}

/** A human member's account is the human's own GitHub user. */
function withHuman(form: GitHubForm, human: boolean): GitHubForm {
  return human ? { ...form, accountType: "human" } : form;
}

/** Leaving the registration flow abandons its registration, so no app ID of it
 * can be saved beside another app's key. */
function withRegistrationFlow(form: GitHubForm): GitHubForm {
  return form.registration && (form.accountType !== "github_apps" || form.setupMode !== "create")
    ? { ...form, registration: null, saveOutcome: null }
    : form;
}

function errors(account: GitHubForm, context: MemberContext, t: Translate): FieldErrors {
  const form = withHuman(account, context.human);
  const result: FieldErrors = {};
  const gitEmail = context.gitEmail.trim();
  if (form.accountType === "none") {
    return result;
  }
  if (form.accountType === "github_apps") {
    const unresolved = !form.username.trim() || !gitEmail;
    if (unresolved && !form.identity.trim() && !form.registration) {
      result.identity = t("setup.validation.memberGithubAppsUrlRequired");
    } else if (form.identity.trim() && !isAppUrl(form.identity)) {
      result.identity = t("setup.validation.memberGithubAppsUrlInvalid");
    }
    if (!form.username.trim()) {
      result.username = t("setup.validation.memberGithubUsernameRequired");
    }
    if (form.registration) {
      // The save waits for the app to be installed; the panel says so.
      if (form.registration.status !== "installed") {
        result.registration = "";
      }
    } else {
      if (!/^\d+$/.test(form.installationId.trim())) {
        result.installationId = form.installationId.trim()
          ? t("setup.validation.githubInstallationIdInvalid")
          : t("setup.validation.githubInstallationIdRequired");
      }
      if (!/^\d+$/.test(form.appId.trim())) {
        result.appId = form.appId.trim()
          ? t("setup.validation.githubAppIdInvalid")
          : t("setup.validation.githubAppIdRequired");
      }
      if (!form.privateKey && !form.stored.privateKey) {
        result.privateKey = t("setup.validation.githubPrivateKeyRequired");
      }
    }
  } else if (!form.username.trim()) {
    result.username = t("setup.validation.memberGithubUsernameRequired");
  } else if (!isUsername(form.username)) {
    result.username = t("setup.validation.memberGithubUsernameInvalid");
  }
  if (!gitEmail) {
    result.gitEmail = t("setup.validation.memberGitEmailRequired");
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(gitEmail)) {
    result.gitEmail = t("setup.validation.memberGitEmailInvalid");
  }
  if (form.accountType === "machine_user") {
    if (!form.accessToken.trim() && !form.stored.accessToken) {
      result.accessToken = t("setup.validation.githubAccessTokenRequired");
    } else if (form.accessToken.trim() && !isAccessToken(form.accessToken)) {
      result.accessToken = t("setup.validation.githubAccessTokenInvalid");
    }
  }
  return result;
}

function resolveErrorMessage(error: unknown, t: Translate): string {
  if (
    error instanceof ApiRequestError &&
    (error.code === "invalid_github_username" || error.code === "invalid_github_apps_url")
  ) {
    return t("setup.validation.memberGithubIdentityNotFound");
  }
  return t("setup.validation.memberGithubIdentityResolveFailed");
}

function AccountPanel({
  form: account,
  onChange,
  context,
  errors: fieldErrors,
}: {
  form: GitHubForm;
  onChange(update: (form: GitHubForm) => GitHubForm): void;
  context: MemberContext;
  errors: FieldErrors;
}) {
  const { t } = useTranslation();
  const form = withHuman(account, context.human);
  const [resolving, setResolving] = useState(false);
  const set = (fields: Partial<GitHubForm>) =>
    onChange((current) => withRegistrationFlow({ ...current, ...fields }));
  const choosePrivateKey = async (file: File | null) =>
    set({ privateKey: file && { file, pem: await file.text() } });
  const isApp = form.accountType === "github_apps";
  const resolveInput = (isApp ? form.identity : form.username).trim();
  const canResolve =
    resolveInput.length > 0 && (isApp ? isAppUrl(resolveInput) : isUsername(resolveInput));
  const resolveLabel = isApp ? t("setup.members.githubAppsUrl") : t("setup.members.githubUsername");
  const resolveError = (isApp ? fieldErrors.identity : fieldErrors.username) || form.resolveError;

  const resolve = async () => {
    if (!canResolve || form.accountType === "none") return;
    set({ resolveError: "" });
    setResolving(true);
    try {
      const resolved = await resolveUser({
        account_type: form.accountType,
        identity: resolveInput,
      });
      if (!resolved.username.trim() || !resolved.git_email.trim() || resolved.user_id <= 0) {
        set({ resolveError: t("setup.validation.memberGithubIdentityNotFound") });
        return;
      }
      set({ username: resolved.username });
      context.setGitEmail(resolved.git_email);
    } catch (error) {
      set({ resolveError: resolveErrorMessage(error, t) });
    } finally {
      setResolving(false);
    }
  };

  return (
    <Stack>
      <Select
        label={t("setup.members.githubAccountType")}
        description={t("setup.members.githubAccountTypeHint")}
        data={ACCOUNT_TYPES.map((option) => ({
          value: option,
          label: t(`setup.members.githubAccountTypeOptions.${option}`),
        }))}
        value={form.accountType}
        disabled={context.human}
        onChange={(value) => set({ accountType: toAccountType(value), resolveError: "" })}
      />
      {isApp ? (
        <SegmentedControl
          aria-label={t("setup.members.githubAppsSetupMode.label")}
          value={form.setupMode}
          onChange={(value) =>
            set({ setupMode: value === "existing" ? "existing" : "create", resolveError: "" })
          }
          data={[
            { value: "create", label: t("setup.members.githubAppsSetupMode.create") },
            { value: "existing", label: t("setup.members.githubAppsSetupMode.existing") },
          ]}
        />
      ) : null}
      {isApp && form.setupMode === "create" ? (
        <GitHubAppRegistrationPanel
          memberKey={context.memberKey}
          personId={context.savedPersonId || context.personId}
          defaultAppName={context.personId}
          defaultOrganization={
            parseProjectUrl(String(context.services.ticket_service?.url ?? "")).organization
          }
          registration={form.registration}
          onRegistrationChange={(action) =>
            onChange((current) => ({
              ...current,
              registration: typeof action === "function" ? action(current.registration) : action,
            }))
          }
          saveOutcome={form.saveOutcome}
          onApplied={(fields) => {
            if (fields.githubUsername) set({ username: fields.githubUsername, resolveError: "" });
            if (fields.gitEmail) context.setGitEmail(fields.gitEmail);
          }}
        />
      ) : null}
      {form.accountType === "none" ? (
        <Text size="sm" c="dimmed">
          {t("setup.members.githubDisabledMemberHint")}
        </Text>
      ) : (
        <>
          {!isApp || form.setupMode === "existing" ? (
            <Stack gap={4}>
              <div>
                <Text fw={500} size="sm">
                  {resolveLabel}
                  <Text span c="danger" inherit aria-hidden="true">
                    {" *"}
                  </Text>
                </Text>
                <Text c="dimmed" size="xs">
                  {isApp
                    ? t("setup.members.githubAppsUrlHint")
                    : t("setup.members.githubUsernameHint")}
                </Text>
              </div>
              <div className="field-action-row">
                <TextInput
                  aria-label={resolveLabel}
                  aria-required
                  value={isApp ? form.identity : form.username}
                  onChange={(event) => {
                    const value = event.currentTarget.value;
                    set(
                      isApp
                        ? { identity: value, resolveError: "" }
                        : { username: value, resolveError: "" },
                    );
                  }}
                  error={Boolean(resolveError)}
                  flex={1}
                />
                <Button
                  variant="default"
                  loading={resolving}
                  disabled={!canResolve}
                  onClick={() => void resolve()}
                >
                  {t("setup.members.resolve")}
                </Button>
              </div>
              {resolveError ? (
                <Text c="danger" size="xs">
                  {resolveError}
                </Text>
              ) : null}
            </Stack>
          ) : null}
          {isApp ? (
            <TextInput
              label={<RequiredLabel text={t("setup.members.githubResolvedIdentity")} />}
              aria-label={t("setup.members.githubResolvedIdentity")}
              aria-required
              value={form.username}
              onChange={(event) => set({ username: event.currentTarget.value })}
              error={fieldErrors.username}
            />
          ) : null}
          <TextInput
            label={<RequiredLabel text={t("setup.members.gitEmail")} />}
            aria-label={t("setup.members.gitEmail")}
            aria-required
            value={context.gitEmail}
            onChange={(event) => context.setGitEmail(event.currentTarget.value)}
            error={fieldErrors.gitEmail}
          />
        </>
      )}
      {isApp ? (
        <>
          <TextInput
            label={<RequiredLabel text={t("setup.members.installationId")} />}
            aria-label={t("setup.members.installationId")}
            aria-required
            value={
              form.registration
                ? String(form.registration.installation_id ?? "")
                : form.installationId
            }
            onChange={(event) => set({ installationId: event.currentTarget.value })}
            readOnly={form.registration !== null}
            error={fieldErrors.installationId}
          />
          <TextInput
            label={<RequiredLabel text={t("setup.members.appId")} />}
            aria-label={t("setup.members.appId")}
            aria-required
            value={form.registration ? String(form.registration.app_id ?? "") : form.appId}
            onChange={(event) => set({ appId: event.currentTarget.value })}
            readOnly={form.registration !== null}
            error={fieldErrors.appId}
          />
          {form.registration ? null : (
            <FileInput
              label={
                form.stored.privateKey ? (
                  t("setup.members.privateKey")
                ) : (
                  <RequiredLabel text={t("setup.members.privateKey")} />
                )
              }
              aria-label={t("setup.members.privateKey")}
              aria-required={!form.stored.privateKey}
              accept=".pem"
              clearable
              leftSection={<FileKey size={16} />}
              placeholder={
                form.stored.privateKey
                  ? MASKED_SECRET_PLACEHOLDER
                  : t("setup.members.privateKeyPlaceholder")
              }
              value={form.privateKey?.file ?? null}
              onChange={(file) => void choosePrivateKey(file)}
              error={fieldErrors.privateKey}
            />
          )}
        </>
      ) : null}
      {form.accountType === "machine_user" ? (
        <PasswordInput
          label={
            form.stored.accessToken ? (
              t("setup.members.accessToken")
            ) : (
              <RequiredLabel text={t("setup.members.accessToken")} />
            )
          }
          aria-label={t("setup.members.accessToken")}
          aria-required={!form.stored.accessToken}
          placeholder={
            form.stored.accessToken
              ? MASKED_SECRET_PLACEHOLDER
              : t("setup.members.accessTokenPlaceholder")
          }
          value={form.accessToken}
          onChange={(event) => set({ accessToken: event.currentTarget.value })}
          error={fieldErrors.accessToken}
          description={
            <SecretStatusHint
              envKey={context.secretEnvKeys.GITHUB_ACCESS_TOKEN}
              scope="identities"
            />
          }
        />
      ) : null}
      {form.accountType === "human" ? (
        <Text size="sm" c="dimmed">
          {t("setup.members.githubAuthNotRequired")}
        </Text>
      ) : null}
    </Stack>
  );
}

export const githubAccount: MemberAccountUi<GitHubForm> = {
  initial,
  fromView(view, current, human) {
    const stored = (view ?? {}) as Partial<AccountView>;
    // A registration started in this form outlives a reload of the member: its
    // app already exists on GitHub, and only a save stores its key.
    const keepsRegistration = current.registration !== null;
    return {
      ...initial(),
      accountType: keepsRegistration
        ? "github_apps"
        : human
          ? "human"
          : toAccountType(stored.account_type || "none"),
      username: stored.username ?? "",
      installationId: stored.installation_id?.toString() ?? "",
      appId: stored.app_id?.toString() ?? "",
      registration: current.registration,
      saveOutcome: current.saveOutcome,
      setupMode: !keepsRegistration && stored.app_id != null ? "existing" : "create",
      stored: {
        privateKey: Boolean(stored.has_private_key),
        accessToken: Boolean(stored.has_access_token),
      },
    };
  },
  request(form, human) {
    const username = form.username.trim();
    if (human) {
      return { account_type: "human", username };
    }
    const account: Record<string, unknown> = {
      account_type: form.accountType === "none" ? "" : form.accountType,
      username,
    };
    if (form.registration?.status === "installed") {
      account.app_registration_id = form.registration.state;
    } else if (form.accountType === "github_apps") {
      account.installation_id = form.installationId ? Number(form.installationId) : undefined;
      account.app_id = form.appId ? Number(form.appId) : undefined;
      account.private_key = form.privateKey?.pem;
    }
    if (form.accountType === "machine_user") {
      account.access_token = form.accessToken.trim() || undefined;
    }
    return account;
  },
  errors,
  Panel: AccountPanel,
  // A registration is for the member it was started for.
  holdsPersonId: (form) => form.registration !== null,
  saved(form, { failure, context, queryClient, t }) {
    if (failure !== undefined) {
      if (!form.registration || form.registration.status !== "installed") return form;
      const outcome = githubAppSaveFailure(failure);
      return {
        ...form,
        saveOutcome: outcome,
        registration: outcome === "expired" ? null : form.registration,
      };
    }
    // A member that cannot be a GitHub assignee is assigned through the
    // board's Agent field, so keep its options in step with every saved
    // GitHub-linked non-human member. Best-effort: the save already succeeded.
    const target = boardTarget(context.services);
    if (target && !context.human && form.accountType !== "none") {
      ensureAgentField(target)
        .then((state) => queryClient.setQueryData(agentFieldQueryKey(target), state))
        .catch(() => {
          notifications.show({
            color: "warning",
            title: t("setup.members.agentFieldSyncFailedTitle"),
            message: t("setup.members.agentFieldSyncFailedBody"),
          });
        });
    }
    const registration = form.registration;
    if (registration?.status !== "installed") return form;
    // The backend consumed the registration; the member now holds its key and
    // IDs as any stored member does.
    return {
      ...form,
      appId: String(registration.app_id ?? ""),
      installationId: String(registration.installation_id ?? ""),
      registration: null,
      saveOutcome: "saved",
      stored: { ...form.stored, privateKey: true },
    };
  },
};
