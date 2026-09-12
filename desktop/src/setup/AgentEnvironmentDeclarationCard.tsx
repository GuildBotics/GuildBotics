import { Card, Select, Stack, TagsInput, Text } from "@mantine/core";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { AgentEnvironmentDeclaration } from "../api/client";

/** Anchor for a system alert to scroll to. */
export const AGENT_ENVIRONMENT_DECLARATION_ID = "agent-environment-declaration";

type PackageManager = keyof AgentEnvironmentDeclaration["packages"];
const PACKAGE_MANAGERS: PackageManager[] = ["apt", "npm", "uv"];
const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

/** A package spec is one argument to its manager: never an option, never two. */
function isPackageSpec(spec: string): boolean {
  return spec.length > 0 && !/\s/.test(spec) && !spec.startsWith("-");
}

/**
 * The shared declaration of the agent environment: the packages every device
 * adds on top of the base image, and the resolvers the environment forwards
 * DNS to. It is saved with the rest of the intelligence settings, so the card
 * only edits; what it rejects never reaches the draft, and an empty resolver
 * list is reported so the save button can wait for it.
 */
export function AgentEnvironmentDeclarationCard({
  value,
  onChange,
  onValidityChange,
}: {
  value: AgentEnvironmentDeclaration;
  onChange: (value: AgentEnvironmentDeclaration) => void;
  onValidityChange?: (valid: boolean) => void;
}) {
  const { t } = useTranslation();
  const [rejected, setRejected] = useState<Partial<Record<PackageManager | "dns", string>>>({});
  const nameservers = Array.isArray(value.dns.nameservers) ? value.dns.nameservers : [];
  const useHost = !Array.isArray(value.dns.nameservers);
  const emptyList = !useHost && nameservers.length === 0;
  useEffect(() => {
    onValidityChange?.(!emptyList);
  }, [emptyList, onValidityChange]);

  const setPackages = (manager: PackageManager, specs: string[]) => {
    const invalid = specs.find((spec) => !isPackageSpec(spec));
    setRejected((current) => ({
      ...current,
      [manager]: invalid
        ? t("setup.intelligence.environment.declaration.invalidPackage")
        : undefined,
    }));
    onChange({
      ...value,
      packages: { ...value.packages, [manager]: specs.filter(isPackageSpec) },
    });
  };
  const setNameservers = (entries: string[]) => {
    const invalid = entries.find((entry) => !IPV4.test(entry));
    setRejected((current) => ({
      ...current,
      dns: invalid ? t("setup.intelligence.environment.declaration.invalidNameserver") : undefined,
    }));
    onChange({ ...value, dns: { nameservers: entries.filter((entry) => IPV4.test(entry)) } });
  };

  return (
    <Card
      id={AGENT_ENVIRONMENT_DECLARATION_ID}
      withBorder
      radius="sm"
      p="md"
      data-testid="agent-environment-declaration"
    >
      <Stack gap="md">
        <div>
          <Text fw={700} size="sm">
            {t("setup.intelligence.environment.declaration.title")}
          </Text>
          <Text size="sm" c="dimmed">
            {t("setup.intelligence.environment.declaration.description")}
          </Text>
        </div>
        {PACKAGE_MANAGERS.map((manager) => (
          <TagsInput
            key={manager}
            size="xs"
            label={t(`setup.intelligence.environment.declaration.${manager}`)}
            description={t("setup.intelligence.environment.declaration.pinHint")}
            placeholder={t(`setup.intelligence.environment.declaration.${manager}Placeholder`)}
            value={value.packages[manager]}
            onChange={(specs) => setPackages(manager, specs)}
            error={rejected[manager]}
            splitChars={[",", " "]}
          />
        ))}
        <Select
          size="xs"
          label={t("setup.intelligence.environment.declaration.nameservers")}
          data={[
            {
              value: "host",
              label: t("setup.intelligence.environment.declaration.nameserversHost"),
            },
            {
              value: "list",
              label: t("setup.intelligence.environment.declaration.nameserversList"),
            },
          ]}
          value={useHost ? "host" : "list"}
          onChange={(mode) =>
            onChange({ ...value, dns: { nameservers: mode === "host" ? "host" : nameservers } })
          }
          allowDeselect={false}
        />
        {useHost ? null : (
          <TagsInput
            size="xs"
            aria-label={t("setup.intelligence.environment.declaration.nameserversList")}
            placeholder={t("setup.intelligence.environment.declaration.nameserversPlaceholder")}
            value={nameservers}
            onChange={setNameservers}
            error={
              rejected.dns ??
              (emptyList
                ? t("setup.intelligence.environment.declaration.emptyNameservers")
                : undefined)
            }
            splitChars={[",", " "]}
          />
        )}
      </Stack>
    </Card>
  );
}
