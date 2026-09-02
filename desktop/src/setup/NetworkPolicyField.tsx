import {
  Alert,
  Button,
  Fieldset,
  Group,
  Select,
  Stack,
  Switch,
  TagsInput,
  Text,
} from "@mantine/core";
import { useTranslation } from "react-i18next";

import {
  CLOSED_NETWORK_POLICY,
  type CliAgentNetworkSupport,
  type NetworkMode,
  type NetworkPolicy,
} from "../api/client";

const MODES: NetworkMode[] = ["deny", "allowlist", "unrestricted"];

type Props = {
  /** Anchor for a system alert to scroll to. */
  id?: string;
  /** The slot's own block, or null when it inherits `inherited`. */
  value: NetworkPolicy | null;
  inherited: NetworkPolicy;
  tool: string;
  toolLabel: string;
  support: CliAgentNetworkSupport | undefined;
  /**
   * Whether this is the tool's own default definition. It has nothing to
   * inherit from but the packaged defaults, so it is always edited directly;
   * only a custom slot chooses between inheriting and stating its own block.
   */
  isToolDefault: boolean;
  onChange: (value: NetworkPolicy | null) => void;
};

/**
 * The `network` block of an AI CLI tool definition.
 *
 * A slot either inherits its tool's block whole or states its own whole:
 * there is no per-field merge, so the editor works on a complete copy. Modes
 * the tool cannot enforce anywhere are disabled with the reason, so a saved
 * definition is one the backend accepts; modes it cannot enforce on this
 * device stay selectable (the definition is shared) but say so.
 */
export function NetworkPolicyField({
  id,
  value,
  inherited,
  tool,
  toolLabel,
  support,
  isToolDefault,
  onChange,
}: Props) {
  const { t } = useTranslation();
  const policy = value ?? inherited ?? CLOSED_NETWORK_POLICY;
  const editable = isToolDefault || value !== null;

  const update = (next: NetworkPolicy) => onChange(next);

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
        {support && !support.contract_applied ? (
          <Alert color="warning" variant="light">
            {t("setup.intelligence.network.contractPending", { tool: toolLabel })}
          </Alert>
        ) : null}
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
        <RouteFields
          route="command"
          policy={policy}
          editable={editable}
          toolLabel={toolLabel}
          modesHere={support?.command_modes ?? MODES}
          modesAnywhere={support?.command_modes_anywhere ?? MODES}
          localNetworkModes={support?.local_network_modes ?? []}
          onChange={update}
        />
        <RouteFields
          route="web"
          policy={policy}
          editable={editable}
          toolLabel={toolLabel}
          modesHere={support?.web_modes ?? MODES}
          modesAnywhere={support?.web_modes ?? MODES}
          localNetworkModes={[]}
          onChange={update}
        />
      </Stack>
    </Fieldset>
  );
}

function RouteFields({
  route,
  policy,
  editable,
  toolLabel,
  modesHere,
  modesAnywhere,
  localNetworkModes,
  onChange,
}: {
  route: "command" | "web";
  policy: NetworkPolicy;
  editable: boolean;
  toolLabel: string;
  modesHere: NetworkMode[];
  modesAnywhere: NetworkMode[];
  localNetworkModes: NetworkMode[];
  onChange: (next: NetworkPolicy) => void;
}) {
  const { t } = useTranslation();
  const current = policy[route];
  const setRoute = (patch: Partial<NetworkPolicy["command"]>) =>
    onChange({ ...policy, [route]: { ...current, ...patch } });
  const modeDescription = (mode: NetworkMode) => {
    if (!modesAnywhere.includes(mode)) {
      return t("setup.intelligence.network.unsupportedAnywhere", { tool: toolLabel });
    }
    if (!modesHere.includes(mode)) {
      return t("setup.intelligence.network.unsupportedHere", { tool: toolLabel });
    }
    return "";
  };
  const selectedDescription = modeDescription(current.mode);
  const localAllowed = localNetworkModes.includes(current.mode);
  return (
    <Stack gap="xs">
      <div>
        <Text size="sm" fw={600}>
          {t(`setup.intelligence.network.${route}`)}
        </Text>
        <Text size="xs" c="dimmed">
          {t(`setup.intelligence.network.${route}Description`)}
        </Text>
      </div>
      <Select
        label={t("setup.intelligence.network.mode")}
        size="xs"
        data={MODES.map((mode) => ({
          value: mode,
          label: t(`setup.intelligence.network.modes.${mode}`),
          disabled: !modesAnywhere.includes(mode),
        }))}
        value={current.mode}
        disabled={!editable}
        description={selectedDescription || undefined}
        error={
          selectedDescription && !modesHere.includes(current.mode) ? selectedDescription : undefined
        }
        onChange={(mode) => {
          if (!mode) return;
          const next = mode as NetworkMode;
          setRoute({
            mode: next,
            allowed_domains: next === "allowlist" ? current.allowed_domains : [],
            ...(route === "command" && !localNetworkModes.includes(next)
              ? { allow_local_network: false }
              : {}),
          });
        }}
      />
      {current.mode === "allowlist" ? (
        <TagsInput
          label={t("setup.intelligence.network.allowedDomains")}
          placeholder={t("setup.intelligence.network.allowedDomainsPlaceholder")}
          size="xs"
          value={current.allowed_domains}
          disabled={!editable}
          onChange={(allowed_domains) => setRoute({ allowed_domains })}
        />
      ) : null}
      {route === "command" ? (
        <Switch
          label={t("setup.intelligence.network.allowLocalNetwork")}
          size="xs"
          checked={policy.command.allow_local_network}
          disabled={!editable || !localAllowed}
          description={
            !localAllowed
              ? t("setup.intelligence.network.allowLocalNetworkUnsupported", { tool: toolLabel })
              : undefined
          }
          onChange={(event) => setRoute({ allow_local_network: event.currentTarget.checked })}
        />
      ) : null}
    </Stack>
  );
}
