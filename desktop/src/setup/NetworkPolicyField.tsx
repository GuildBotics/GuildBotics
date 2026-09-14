import { Alert, Fieldset, Select, Stack, Switch, TagsInput, Text } from "@mantine/core";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { type NetworkMode, type NetworkPolicy } from "../api/client";

const MODES: NetworkMode[] = ["deny", "allowlist", "unrestricted"];

type Props = {
  value: NetworkPolicy;
  onChange: (value: NetworkPolicy) => void;
  onValidityChange?: (valid: boolean) => void;
};

function isDomain(domain: string): boolean {
  return domain.trim().length > 0 && !/\s/.test(domain) && !domain.includes("/");
}

/** The workspace-wide network rule enforced for every AI CLI turn. */
export function NetworkPolicyField({ value, onChange, onValidityChange }: Props) {
  const { t } = useTranslation();
  const [rejected, setRejected] = useState<string>();
  const emptyAllowlist = value.mode === "allowlist" && value.allowed_domains.length === 0;
  useEffect(() => {
    onValidityChange?.(!emptyAllowlist && !rejected);
  }, [emptyAllowlist, onValidityChange, rejected]);

  const setPolicy = (patch: Partial<NetworkPolicy>) => onChange({ ...value, ...patch });
  const setDomains = (domains: string[]) => {
    const invalid = domains.find((domain) => !isDomain(domain));
    setRejected(invalid ? t("setup.intelligence.network.invalidDomain") : undefined);
    setPolicy({ allowed_domains: domains.filter(isDomain) });
  };

  return (
    <Fieldset legend={t("setup.intelligence.network.title")} data-testid="network:environment">
      <Stack gap="sm">
        <Text size="xs" c="dimmed">
          {t("setup.intelligence.network.description")}
        </Text>
        <Select
          label={t("setup.intelligence.network.mode")}
          size="xs"
          data={MODES.map((mode) => ({
            value: mode,
            label: t(`setup.intelligence.network.modes.${mode}`),
          }))}
          value={value.mode}
          onChange={(mode) => {
            if (!mode) return;
            const next = mode as NetworkMode;
            setPolicy({
              mode: next,
              allowed_domains: next === "allowlist" ? value.allowed_domains : [],
            });
          }}
        />
        {value.mode === "allowlist" ? (
          <TagsInput
            label={t("setup.intelligence.network.allowedDomains")}
            placeholder={t("setup.intelligence.network.allowedDomainsPlaceholder")}
            size="xs"
            value={value.allowed_domains}
            onChange={setDomains}
            error={
              rejected ??
              (emptyAllowlist ? t("setup.intelligence.network.emptyAllowlist") : undefined)
            }
          />
        ) : null}
        {value.mode === "unrestricted" ? (
          <Alert color="warning">{t("setup.intelligence.network.unrestrictedWarning")}</Alert>
        ) : null}
        <Switch
          label={t("setup.intelligence.network.allowLocalNetwork")}
          size="xs"
          checked={value.allow_local_network}
          onChange={(event) => setPolicy({ allow_local_network: event.currentTarget.checked })}
        />
      </Stack>
    </Fieldset>
  );
}
