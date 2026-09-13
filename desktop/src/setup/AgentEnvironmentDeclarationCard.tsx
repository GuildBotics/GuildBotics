import { Card, Select, Stack, TagsInput, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { getAgentEnvironmentImages, type AgentEnvironmentDeclaration } from "../api/client";

/** Anchor for a system alert to scroll to. */
export const AGENT_ENVIRONMENT_DECLARATION_ID = "agent-environment-declaration";

type PackageManager = keyof AgentEnvironmentDeclaration["packages"];
const PACKAGE_MANAGERS: PackageManager[] = ["apt", "npm", "uv"];
const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

/** A package spec is one argument to its manager: never an option, never two. */
function isPackageSpec(spec: string): boolean {
  return spec.length > 0 && !/\s/.test(spec) && !spec.startsWith("-");
}

/** How much of a digest a person is shown: enough to tell two apart. */
function shortDigest(digest: string): string {
  return digest.slice(0, "sha256:".length + 12);
}

/**
 * The shared declaration of the agent environment: the base image every
 * device builds from, the packages it adds on top, and the resolvers the
 * environment forwards DNS to. It is saved with the rest of the intelligence
 * settings, so the card only edits; what it rejects never reaches the draft,
 * and an empty resolver list is reported so the save button can wait for it.
 * The image is picked from those loaded on this device, so the declaration
 * carries the digest of what will actually be built from; a declared image
 * this device lacks stays selectable as declared.
 */
export function AgentEnvironmentDeclarationCard({
  value,
  defaultImage,
  onChange,
  onValidityChange,
}: {
  value: AgentEnvironmentDeclaration;
  /** The image GuildBotics builds from when the declaration names none. */
  defaultImage: string;
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
  const images = useQuery({
    queryKey: ["agent-environment-images"],
    queryFn: getAgentEnvironmentImages,
  });
  const architecture = images.data?.architecture ?? "";
  const held = images.data?.images ?? [];
  const declared = value.image ?? null;
  const declaredHere = declared?.digests[architecture];
  // An option is one image: reference and digest, so a reference loaded
  // again under a new digest is a new choice beside the declared one.
  const key = (reference: string, digest: string) => `${reference}@${digest}`;
  const selected = declared && declaredHere ? key(declared.reference, declaredHere) : "";
  const imageOptions = [
    {
      value: "",
      label: t("setup.intelligence.environment.declaration.imageDefault", {
        reference: defaultImage,
      }),
    },
    ...held.map((image) => ({
      value: key(image.reference, image.digest),
      label: `${image.reference} (${shortDigest(image.digest)})`,
    })),
    ...(declared && declaredHere && !held.some((image) => image.digest === declaredHere)
      ? [
          {
            value: selected,
            label: `${declared.reference} (${shortDigest(declaredHere)}) — ${t("setup.intelligence.environment.declaration.imageNotHere")}`,
            disabled: true,
          },
        ]
      : []),
  ];
  // The digest this device picks is its architecture's; what other
  // architectures declared for the same reference stays, since their images
  // are built from the same source on their own devices.
  const setImage = (option: string | null) => {
    const image = held.find((candidate) => key(candidate.reference, candidate.digest) === option);
    onChange({
      ...value,
      image: image
        ? {
            reference: image.reference,
            digests: {
              ...(declared?.reference === image.reference ? declared.digests : {}),
              [architecture]: image.digest,
            },
          }
        : null,
    });
  };
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
        <Select
          size="xs"
          label={t("setup.intelligence.environment.declaration.image")}
          description={t("setup.intelligence.environment.declaration.imageHint")}
          data={imageOptions}
          value={selected}
          onChange={setImage}
          error={
            images.data?.problem
              ? t("setup.intelligence.environment.declaration.imageUnavailable", {
                  problem: images.data.problem,
                })
              : declared && architecture && !declaredHere
                ? t("setup.intelligence.environment.declaration.imageNotDeclaredHere", {
                    reference: declared.reference,
                    architecture,
                    declared: Object.keys(declared.digests).sort().join(", "),
                  })
                : undefined
          }
          allowDeselect={false}
        />
        {declared ? (
          <Text size="xs" c="dimmed">
            {t("setup.intelligence.environment.declaration.imageDeclared", {
              digests: Object.entries(declared.digests)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([arch, digest]) => `${arch}=${shortDigest(digest)}`)
                .join(" "),
            })}
          </Text>
        ) : null}
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
