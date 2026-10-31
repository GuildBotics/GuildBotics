import { Checkbox, Radio, Select, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import type { FieldErrors, MemberAccountUi, MemberContext } from "../types";
import { branvilKey, listAgents } from "./api";
import { BranvilConnection, heldConnection, useConnection } from "./BranvilConnection";

/** A member's stored Branvil account, as the backend shows it. */
export type AccountView = {
  username: string;
  has_token: boolean;
};

export type BranvilForm = {
  /** The Branvil member saved for this member; "" for none yet. */
  stored: string;
  storedToken: boolean;
  /** For an agent without one: a new agent named after the member, or the
   * agent chosen. */
  choice: "new" | "existing";
  chosen: string;
  reissue: boolean;
};

const initial = (): BranvilForm => ({
  stored: "",
  storedToken: false,
  choice: "new",
  chosen: "",
  reissue: false,
});

function errors(
  form: BranvilForm,
  context: MemberContext,
  t: Parameters<MemberAccountUi<BranvilForm>["errors"]>[2],
): FieldErrors {
  return !context.human && !form.stored && form.choice === "existing" && !form.chosen
    ? { chosen: t("setup.branvil.member.chooseRequired") }
    : {};
}

function AgentChoice({
  form,
  set,
  context,
  error,
}: {
  form: BranvilForm;
  set(fields: Partial<BranvilForm>): void;
  context: MemberContext;
  error?: string;
}) {
  const { t } = useTranslation();
  const connected = useConnection().data?.status === "connected";
  const agents = useQuery({
    queryKey: branvilKey("agents"),
    queryFn: listAgents,
    enabled: connected,
  });
  return (
    <Radio.Group
      label={t("setup.branvil.member.label")}
      value={form.choice}
      onChange={(choice) => set({ choice: choice === "existing" ? "existing" : "new" })}
    >
      <Stack gap="xs" mt="xs">
        <Radio
          value="new"
          label={t("setup.branvil.member.new", { name: context.personId.trim() || "…" })}
        />
        <Radio value="existing" label={t("setup.branvil.member.existing")} />
        {form.choice === "existing" ? (
          <Select
            aria-label={t("setup.branvil.member.choose")}
            placeholder={t("setup.branvil.member.choose")}
            data={agents.data?.agents ?? []}
            value={form.chosen || null}
            onChange={(chosen) => set({ chosen: chosen ?? "" })}
            error={error}
          />
        ) : null}
      </Stack>
    </Radio.Group>
  );
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
  return (
    <Stack>
      <BranvilConnection />
      {context.human ? (
        <Text size="sm">
          {form.stored
            ? t("setup.branvil.member.owner", { name: form.stored })
            : t("setup.branvil.member.ownerPending")}
        </Text>
      ) : form.stored ? (
        <Stack gap="xs">
          <Text size="sm">{t("setup.branvil.member.stored", { name: form.stored })}</Text>
          <Text size="xs" c="dimmed">
            {form.storedToken
              ? t("setup.branvil.member.tokenSet")
              : t("setup.branvil.member.tokenMissing")}
          </Text>
          <Checkbox
            label={t("setup.branvil.member.reissue")}
            checked={form.reissue}
            onChange={(event) => set({ reissue: event.currentTarget.checked })}
          />
        </Stack>
      ) : (
        <AgentChoice form={form} set={set} context={context} error={fieldErrors.chosen} />
      )}
    </Stack>
  );
}

export const branvilAccount: MemberAccountUi<BranvilForm> = {
  initial,
  fromView(view) {
    const stored = (view ?? {}) as Partial<AccountView>;
    return { ...initial(), stored: stored.username ?? "", storedToken: Boolean(stored.has_token) };
  },
  request: (form, human) =>
    human
      ? { username: form.stored }
      : {
          username: form.stored || (form.choice === "existing" ? form.chosen : ""),
          reissue: form.reissue,
        },
  errors,
  Panel: AccountPanel,
  // The save named the member on Branvil: as chosen, after the member, or,
  // for a human, after the owner.
  saved(form, { failure, context, queryClient }) {
    if (failure !== undefined) return form;
    void queryClient.invalidateQueries({ queryKey: branvilKey("agents") });
    const named = context.human
      ? (heldConnection(queryClient)?.owner ?? "")
      : form.choice === "existing"
        ? form.chosen
        : context.personId.trim();
    return {
      ...form,
      stored: form.stored || named,
      storedToken: !context.human,
      reissue: false,
    };
  },
};
