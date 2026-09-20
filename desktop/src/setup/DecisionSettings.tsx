import { type ReactNode } from "react";
import { Alert, Card, PasswordInput, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { getDecisionOptions } from "../api/client";
import { MASKED_SECRET_PLACEHOLDER } from "./secretInput";

export function DecisionSettings({
  personId,
  engine,
  apiKey,
  onApiKeyChange,
  credentialError,
  children,
}: {
  personId?: string;
  engine?: "llm" | "cli" | "jev";
  apiKey: string;
  onApiKeyChange: (value: string) => void;
  credentialError: boolean;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const options = useQuery({
    queryKey: ["decision-options"],
    queryFn: getDecisionOptions,
    enabled: engine === "jev",
  });
  return (
    <Card withBorder id="decision-settings">
      <Stack gap="sm">
        <Text fw={700}>{t("decision.title")}</Text>
        <Text size="sm">{t("decision.assignmentHint")}</Text>
        <Text size="sm">{t(personId ? "decision.member" : "decision.team")}</Text>
        {!engine && <Alert color="yellow">{t("decision.unconfigured")}</Alert>}
        {children}
        {engine === "jev" && (
          <>
            {(options.isError || credentialError) && (
              <Alert color="red">{t("decision.failed")}</Alert>
            )}
            {options.data?.credential_present === false && (
              <Text size="sm">{t("decision.keyMissing")}</Text>
            )}
            <PasswordInput
              label={t("decision.key")}
              description={t("decision.keySaveHint")}
              placeholder={options.data?.credential_present ? MASKED_SECRET_PLACEHOLDER : undefined}
              value={apiKey}
              onChange={(event) => onApiKeyChange(event.currentTarget.value)}
              autoComplete="off"
              w="100%"
            />
          </>
        )}
        <Text size="xs" c="dimmed">
          {t("decision.fallback")}
        </Text>
      </Stack>
    </Card>
  );
}
