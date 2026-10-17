import { PasswordInput, Stack, TextInput } from "@mantine/core";
import { useTranslation } from "react-i18next";

import { SecretStatusHint } from "../../../sync/SecretStatusHint";
import { RequiredLabel } from "../../formParts";
import { MASKED_SECRET_PLACEHOLDER } from "../../secretInput";
import type { FieldErrors, MemberAccountUi, MemberContext } from "../types";

/** A member's stored Branvil account, as the backend shows it. */
export type AccountView = {
  username: string;
  has_token: boolean;
};

export type BranvilForm = {
  username: string;
  /** A new token; empty keeps the stored one. */
  token: string;
  storedToken: boolean;
};

const MEMBER_NAME = /^[A-Za-z0-9_.-]{1,64}$/;

const initial = (): BranvilForm => ({ username: "", token: "", storedToken: false });

function errors(
  form: BranvilForm,
  context: MemberContext,
  t: Parameters<MemberAccountUi<BranvilForm>["errors"]>[2],
): FieldErrors {
  const result: FieldErrors = {};
  const username = form.username.trim();
  if (!username) {
    result.username = t("setup.branvil.usernameRequired");
  } else if (!MEMBER_NAME.test(username)) {
    result.username = t("setup.branvil.usernameInvalid");
  }
  if (!context.human && !form.token.trim() && !form.storedToken) {
    result.token = t("setup.branvil.tokenRequired");
  }
  return result;
}

function AccountPanel({
  form,
  onChange,
  context,
  errors: fieldErrors,
}: {
  form: BranvilForm;
  onChange(update: (form: BranvilForm) => BranvilForm): void;
  context: MemberContext;
  errors: FieldErrors;
}) {
  const { t } = useTranslation();
  const set = (fields: Partial<BranvilForm>) => onChange((current) => ({ ...current, ...fields }));
  const tokenRequired = !form.storedToken;
  return (
    <Stack>
      <TextInput
        label={<RequiredLabel text={t("setup.branvil.username")} />}
        aria-label={t("setup.branvil.username")}
        aria-required
        description={t("setup.branvil.usernameHint")}
        value={form.username}
        onChange={(event) => set({ username: event.currentTarget.value })}
        error={fieldErrors.username}
      />
      {context.human ? null : (
        <PasswordInput
          label={
            tokenRequired ? (
              <RequiredLabel text={t("setup.branvil.token")} />
            ) : (
              t("setup.branvil.token")
            )
          }
          aria-label={t("setup.branvil.token")}
          aria-required={tokenRequired}
          placeholder={
            form.storedToken ? MASKED_SECRET_PLACEHOLDER : t("setup.branvil.tokenPlaceholder")
          }
          value={form.token}
          onChange={(event) => set({ token: event.currentTarget.value })}
          error={fieldErrors.token}
          description={
            <>
              {t("setup.branvil.tokenHint")}
              <SecretStatusHint envKey={context.secretEnvKeys.BRANVIL_TOKEN} />
            </>
          }
        />
      )}
    </Stack>
  );
}

export const branvilAccount: MemberAccountUi<BranvilForm> = {
  initial,
  fromView(view) {
    const stored = (view ?? {}) as Partial<AccountView>;
    return {
      ...initial(),
      username: stored.username ?? "",
      storedToken: Boolean(stored.has_token),
    };
  },
  request: (form, human) =>
    human
      ? { username: form.username.trim() }
      : { username: form.username.trim(), token: form.token.trim() },
  errors,
  Panel: AccountPanel,
};
