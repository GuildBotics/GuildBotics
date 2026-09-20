import { type ReactNode } from "react";
import { Alert, Card, PasswordInput, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { getDecisionOptions, type ChatDecisionSelection } from "../api/client";

export function DecisionSettings({
  personId,
  engine,
  apiKey,
  onApiKeyChange,
  credentialError,
  selection,
  children,
}: {
  personId?: string;
  engine?: "llm" | "cli" | "jev";
  apiKey: string;
  onApiKeyChange: (value: string) => void;
  credentialError: boolean;
  selection?: ChatDecisionSelection | null;
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
        {children}
        <Stack gap={4}>
          <Text size="sm" fw={600}>
            {t("decision.savedSelection")}
          </Text>
          {selection ? (
            <>
              <Text size="sm">
                {t("decision.savedEngine", {
                  value:
                    selection.engine === "jev"
                      ? "Jev"
                      : `${selection.engine === "llm" ? "LLM" : "AI CLI"} / ${selection.provider || t("decision.notConfigured")}`,
                })}
              </Text>
              {selection.slot && (
                <Text size="sm">{t("decision.savedSlot", { value: selection.slot })}</Text>
              )}
              <Text size="sm">
                {t("decision.savedModel", {
                  value:
                    selection.model ||
                    t(
                      selection.engine === "cli" && selection.resolved
                        ? "decision.cliDefaultModel"
                        : "decision.notConfigured",
                    ),
                })}
              </Text>
              {personId && (
                <Text size="sm">
                  {t(
                    selection.assignment_inherited
                      ? "decision.inheritedAssignment"
                      : "decision.memberAssignment",
                  )}
                </Text>
              )}
              {!selection.resolved && (
                <Text size="sm" c="orange">
                  {t("decision.unresolvedSlot")}
                </Text>
              )}
            </>
          ) : (
            <Text size="sm">{t("decision.notConfigured")}</Text>
          )}
        </Stack>
        {engine === "jev" && (
          <>
            {(options.isError || credentialError) && (
              <Alert color="red">{t("decision.failed")}</Alert>
            )}
            <Text size="sm">
              {t(options.data?.credential_present ? "decision.keyPresent" : "decision.keyMissing")}
            </Text>
            <PasswordInput
              label={t("decision.key")}
              description={t("decision.keySaveHint")}
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
