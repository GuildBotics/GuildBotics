import { Stack, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";

import type { CommandRequirement } from "../api/client";

/**
 * The requirements a command still misses, each with the backend's reason
 * when it has one (the agent environment's refusal for an AI CLI tool, in the
 * same words its status card and alert show).
 */
export function UnmetRequirements({
  requirements,
  size,
}: {
  requirements: CommandRequirement[];
  size: "xs" | "sm";
}) {
  const { t } = useTranslation();
  return (
    <Stack gap={2}>
      {requirements.map((requirement) => {
        const label = t(`commands.requirements.${requirement.kind}`);
        return (
          <Text key={requirement.kind} size={size}>
            {requirement.message
              ? t("commands.requirementUnmet", { requirement: label, reason: requirement.message })
              : label}
          </Text>
        );
      })}
    </Stack>
  );
}
