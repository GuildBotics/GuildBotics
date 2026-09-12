import { Button, Fieldset, Group, Select, Stack, Switch, TagsInput, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";

import { CLOSED_NETWORK_POLICY, type NetworkMode, type NetworkPolicy } from "../api/client";

const MODES: NetworkMode[] = ["deny", "allowlist", "unrestricted"];

type Props = {
  /** Anchor for a system alert to scroll to. */
  id?: string;
  /** The slot's own block, or null when it inherits `inherited`. */
  value: NetworkPolicy | null;
  inherited: NetworkPolicy;
  tool: string;
  /**
   * Whether this is the tool's own default definition. It has nothing to
   * inherit from but the packaged defaults, so it is always edited directly;
   * only a custom slot chooses between inheriting and stating its own block.
   */
  isToolDefault: boolean;
  onChange: (value: NetworkPolicy | null) => void;
};

/**
 * The `network` block of an AI CLI tool definition: one rule for the
 * commands the tool runs and its own web tools alike.
 *
 * A slot either inherits its tool's block whole or states its own whole:
 * there is no per-field merge, so the editor works on a complete copy. The
 * isolated agent environment enforces every mode the same way on every
 * device, so nothing here depends on the tool or the OS.
 */
export function NetworkPolicyField({ id, value, inherited, tool, isToolDefault, onChange }: Props) {
  const { t } = useTranslation();
  const policy = value ?? inherited ?? CLOSED_NETWORK_POLICY;
  const editable = isToolDefault || value !== null;

  const setPolicy = (patch: Partial<NetworkPolicy>) => onChange({ ...policy, ...patch });

  return (
    <Fieldset
      id={id}
      legend={t("setup.intelligence.network.title")}
      data-testid={`network:${tool}`}
    >
      <Stack gap="sm">
        <Text size="xs" c="dimmed">
          {t("setup.intelligence.network.description")}
        </Text>
        <Group gap="xs" align="center">
          {isToolDefault ? (
            <Button
              size="xs"
              variant="subtle"
              disabled={JSON.stringify(policy) === JSON.stringify(CLOSED_NETWORK_POLICY)}
              onClick={() => onChange(structuredClone(CLOSED_NETWORK_POLICY))}
            >
              {t("setup.intelligence.network.resetToPackaged")}
            </Button>
          ) : editable ? (
            <Button size="xs" variant="subtle" onClick={() => onChange(null)}>
              {t("setup.intelligence.network.useDefault")}
            </Button>
          ) : (
            <>
              <Text size="xs" c="dimmed">
                {t("setup.intelligence.network.inherited")}
              </Text>
              <Button size="xs" variant="light" onClick={() => onChange(structuredClone(policy))}>
                {t("setup.intelligence.network.customize")}
              </Button>
            </>
          )}
        </Group>
        <Select
          label={t("setup.intelligence.network.mode")}
          size="xs"
          data={MODES.map((mode) => ({
            value: mode,
            label: t(`setup.intelligence.network.modes.${mode}`),
          }))}
          value={policy.mode}
          disabled={!editable}
          onChange={(mode) => {
            if (!mode) return;
            const next = mode as NetworkMode;
            setPolicy({
              mode: next,
              allowed_domains: next === "allowlist" ? policy.allowed_domains : [],
            });
          }}
        />
        {policy.mode === "allowlist" ? (
          <TagsInput
            label={t("setup.intelligence.network.allowedDomains")}
            placeholder={t("setup.intelligence.network.allowedDomainsPlaceholder")}
            size="xs"
            value={policy.allowed_domains}
            disabled={!editable}
            onChange={(allowed_domains) => setPolicy({ allowed_domains })}
          />
        ) : null}
        <Switch
          label={t("setup.intelligence.network.allowLocalNetwork")}
          size="xs"
          checked={policy.allow_local_network}
          disabled={!editable}
          onChange={(event) => setPolicy({ allow_local_network: event.currentTarget.checked })}
        />
      </Stack>
    </Fieldset>
  );
}
