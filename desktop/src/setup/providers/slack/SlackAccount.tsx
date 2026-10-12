import { PasswordInput, SegmentedControl, Stack, TextInput } from "@mantine/core";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { SecretStatusHint } from "../../../sync/SecretStatusHint";
import { RequiredLabel } from "../../formParts";
import { MASKED_SECRET_PLACEHOLDER } from "../../secretInput";
import type { FieldErrors, MemberAccountUi, MemberContext } from "../types";
import type { AccountView } from "./api";
import { SlackAppRegistrationPanel } from "./SlackAppRegistration";
import { SlackTokenVerificationPanel } from "./SlackTokenVerification";

type Translate = TFunction | ((key: string) => string);

export type SlackForm = {
  userId: string;
  botToken: string;
  appToken: string;
  /** Create a new app from a manifest deep link, or paste an existing app's tokens. */
  setupMode: "create" | "existing";
  stored: { botToken: boolean; appToken: boolean };
};

const initial = (): SlackForm => ({
  userId: "",
  botToken: "",
  appToken: "",
  setupMode: "create",
  stored: { botToken: false, appToken: false },
});

/** Whether a channel is named as Slack accepts it: its ID, or its name. */
export function isSlackChannel(reference: string): boolean {
  const channel = reference.trim();
  return (
    /^[CGD][A-Z0-9]{8,}$/.test(channel) ||
    /^#?[\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Han}a-z0-9][\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Han}a-z0-9_-]{0,79}$/u.test(
      channel,
    )
  );
}

function errors(form: SlackForm, context: MemberContext, t: Translate): FieldErrors {
  const result: FieldErrors = {};
  if (context.human) {
    if (!form.userId.trim()) {
      result.userId = t("setup.validation.slackUserIdRequired");
    } else if (!/^U[A-Z0-9]{8,}$/.test(form.userId.trim())) {
      result.userId = t("setup.validation.slackUserIdInvalid");
    }
    return result;
  }
  // A bot that listens on channels needs both tokens.
  const listens = context.chatChannels.length > 0;
  if (form.botToken.trim() && !/^xoxb-[A-Za-z0-9-]{8,}$/.test(form.botToken.trim())) {
    result.botToken = t("setup.validation.slackBotTokenInvalid");
  } else if (listens && !form.botToken.trim() && !form.stored.botToken) {
    result.botToken = t("setup.validation.slackBotTokenRequired");
  }
  if (form.appToken.trim() && !/^xapp-[A-Za-z0-9-]{8,}$/.test(form.appToken.trim())) {
    result.appToken = t("setup.validation.slackAppTokenInvalid");
  } else if (listens && !form.appToken.trim() && !form.stored.appToken) {
    result.appToken = t("setup.validation.slackAppTokenRequired");
  }
  return result;
}

function AccountPanel({
  form,
  onChange,
  context,
  errors: fieldErrors,
}: {
  form: SlackForm;
  onChange(update: (form: SlackForm) => SlackForm): void;
  context: MemberContext;
  errors: FieldErrors;
}) {
  const { t } = useTranslation();
  const set = (fields: Partial<SlackForm>) => onChange((current) => ({ ...current, ...fields }));
  if (context.human) {
    return (
      <TextInput
        label={<RequiredLabel text={t("setup.members.slackUserId")} />}
        aria-label={t("setup.members.slackUserId")}
        aria-required
        value={form.userId}
        onChange={(event) => set({ userId: event.currentTarget.value })}
        description={t("setup.members.slackUserIdHint")}
        error={fieldErrors.userId}
      />
    );
  }
  const listens = context.chatChannels.length > 0;
  const tokenField = (
    token: "botToken" | "appToken",
    label: string,
    placeholder: string,
    secret: string,
  ) => {
    const required = listens && !form.stored[token];
    return (
      <PasswordInput
        label={required ? <RequiredLabel text={label} /> : label}
        aria-label={label}
        aria-required={required}
        placeholder={form.stored[token] ? MASKED_SECRET_PLACEHOLDER : placeholder}
        value={form[token]}
        onChange={(event) => set({ [token]: event.currentTarget.value })}
        error={fieldErrors[token]}
        description={<SecretStatusHint envKey={context.secretEnvKeys[secret]} />}
      />
    );
  };
  return (
    <Stack>
      <SegmentedControl
        aria-label={t("setup.members.slackAppsSetupMode.label")}
        value={form.setupMode}
        onChange={(value) => set({ setupMode: value === "existing" ? "existing" : "create" })}
        data={[
          { value: "create", label: t("setup.members.slackAppsSetupMode.create") },
          { value: "existing", label: t("setup.members.slackAppsSetupMode.existing") },
        ]}
      />
      {form.setupMode === "create" ? (
        <SlackAppRegistrationPanel
          memberKey={context.memberKey}
          defaultAppName={context.personId}
        />
      ) : null}
      {context.chatChannelsField}
      {tokenField(
        "botToken",
        t("setup.members.slackBotToken"),
        t("setup.members.slackBotTokenPlaceholder"),
        "SLACK_BOT_TOKEN",
      )}
      {tokenField(
        "appToken",
        t("setup.members.slackAppToken"),
        t("setup.members.slackAppTokenPlaceholder"),
        "SLACK_APP_TOKEN",
      )}
      <SlackTokenVerificationPanel
        botToken={form.botToken}
        appToken={form.appToken}
        // Empty fields keep the saved tokens, so verification needs to know
        // whose saved tokens those are.
        personId={context.savedPersonId}
        channels={context.chatChannels}
      />
    </Stack>
  );
}

export const slackAccount: MemberAccountUi<SlackForm> = {
  initial,
  fromView(view) {
    const stored = (view ?? {}) as Partial<AccountView>;
    const tokens = {
      botToken: Boolean(stored.has_bot_token),
      appToken: Boolean(stored.has_app_token),
    };
    return {
      ...initial(),
      userId: stored.user_id ?? "",
      // A member that already carries tokens is being edited, not set up, so
      // the mode that does not push them into Slack is the default.
      setupMode: tokens.botToken || tokens.appToken ? "existing" : "create",
      stored: tokens,
    };
  },
  request: (form, human) =>
    human
      ? { user_id: form.userId.trim() }
      : { bot_token: form.botToken.trim(), app_token: form.appToken.trim() },
  errors,
  Panel: AccountPanel,
};
