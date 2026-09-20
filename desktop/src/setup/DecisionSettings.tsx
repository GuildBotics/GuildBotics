import { useState, type ReactNode } from "react";
import { Alert, Button, Card, Group, PasswordInput, Stack, Text } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  checkDecision,
  getDecisionOptions,
  saveDecisionCredential,
  type ChatDecisionSelection,
} from "../api/client";

export function DecisionSettings({
  personId,
  unsaved,
  selection,
  children,
}: {
  personId?: string;
  unsaved: boolean;
  selection?: ChatDecisionSelection | null;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [key, setKey] = useState("");
  const options = useQuery({ queryKey: ["decision-options"], queryFn: getDecisionOptions });
  const check = useMutation({
    mutationFn: () => checkDecision({ brain: "chat_decision" }, personId),
  });
  const credential = useMutation({
    mutationFn: saveDecisionCredential,
    onSuccess: async () => {
      setKey("");
      check.reset();
      await client.invalidateQueries({ queryKey: ["decision-options"] });
    },
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
        {unsaved && <Text size="sm">{t("decision.saveBeforeCheck")}</Text>}
        {!unsaved && check.data && (
          <Alert color={check.data.state === "verified" ? "blue" : "orange"}>
            {t(`decision.states.${check.data.state}`)} {check.data.model}
          </Alert>
        )}
        {(options.isError || check.isError || credential.isError) && (
          <Alert color="red">{t("decision.failed")}</Alert>
        )}
        <Button
          variant="light"
          disabled={unsaved || !selection?.resolved}
          loading={check.isPending}
          onClick={() => check.mutate()}
        >
          {t("decision.check")}
        </Button>
        <Text size="sm">
          {t(options.data?.credential_present ? "decision.keyPresent" : "decision.keyMissing")}
        </Text>
        <Group align="end">
          <PasswordInput
            label={t("decision.key")}
            value={key}
            onChange={(event) => setKey(event.currentTarget.value)}
            autoComplete="off"
          />
          <Button
            disabled={!key.trim()}
            loading={credential.isPending}
            onClick={() => credential.mutate(key)}
          >
            {t("decision.register")}
          </Button>
        </Group>
        <Text size="xs" c="dimmed">
          {t("decision.fallback")}
        </Text>
      </Stack>
    </Card>
  );
}
