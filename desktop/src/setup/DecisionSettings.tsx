import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Group,
  PasswordInput,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import {
  checkDecision,
  getDecisionOptions,
  getDecisionStatus,
  saveDecisionCredential,
  type DecisionConfig,
} from "../api/client";

export function DecisionSettings({
  value,
  personId,
  disabled,
  onChange,
  onValidity,
}: {
  value?: DecisionConfig;
  personId?: string;
  disabled?: boolean;
  onChange: (value: DecisionConfig) => void;
  onValidity: (valid: boolean) => void;
}) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [key, setKey] = useState("");
  const options = useQuery({
    queryKey: ["decision-options", personId],
    queryFn: () => getDecisionOptions(personId),
  });
  const status = useQuery({
    queryKey: ["decision-status", personId, value],
    queryFn: () => getDecisionStatus(value!, personId),
    enabled: Boolean(value),
  });
  const check = useMutation({
    mutationFn: (input: { config: DecisionConfig; personId?: string }) =>
      checkDecision(input.config, input.personId),
    onSuccess: (result, input) => {
      client.setQueryData(["decision-status", input.personId, input.config], result.status);
      void client.invalidateQueries({ queryKey: ["decision-options"] });
    },
  });
  const credential = useMutation({
    mutationFn: saveDecisionCredential,
    onSuccess: async () => {
      setKey("");
      check.reset();
      await Promise.all([
        client.invalidateQueries({ queryKey: ["decision-options"] }),
        client.invalidateQueries({ queryKey: ["decision-status"] }),
      ]);
    },
  });
  const state = status.data;
  useEffect(() => {
    onValidity(disabled || !value || state?.available === true);
  }, [disabled, value, state, onValidity]);
  const update = (config: DecisionConfig) => {
    check.reset();
    onChange(config);
  };
  return (
    <Card withBorder id="decision-settings">
      <Stack gap="sm">
        <Text fw={700}>{t("decision.title")}</Text>
        <Text size="sm">{t(personId ? "decision.member" : "decision.team")}</Text>
        {value && (
          <Text size="sm">
            {t("decision.current", { engine: value.engine, model: value.model || "—" })}
          </Text>
        )}
        <Select
          label={t("decision.engine")}
          disabled={disabled}
          value={value ? `${value.engine}:${value.provider}` : null}
          data={(options.data?.options ?? []).map((option) => ({
            value: `${option.engine}:${option.provider}`,
            label:
              option.engine === "jev"
                ? "Jev"
                : `${option.engine === "agno" ? "Agno" : "AI CLI"} / ${option.provider}`,
            disabled: !option.available,
          }))}
          onChange={(selection) => {
            if (selection) {
              const [engine, provider] = selection.split(":");
              const option = options.data?.options.find(
                (item) => item.engine === engine && item.provider === provider,
              );
              update({
                engine: engine as DecisionConfig["engine"],
                provider,
                model: option?.models[0] ?? "",
              });
            }
          }}
        />
        {(options.data?.options ?? [])
          .filter((option) => !option.available)
          .map((option) => (
            <Text size="xs" c="dimmed" key={`${option.engine}:${option.provider}`}>
              {option.engine} {option.provider}: {option.reason}
            </Text>
          ))}
        {value?.engine === "jev" ? (
          <Select
            label={t("decision.model")}
            disabled={disabled}
            value={value.model}
            data={[
              ...new Set([
                value.model,
                ...(options.data?.options.find(
                  (option) => option.engine === value.engine && option.provider === value.provider,
                )?.models ?? []),
                ...(check.data?.models ?? []),
              ]),
            ].filter(Boolean)}
            onChange={(model) => update({ ...value, model: model ?? "" })}
          />
        ) : (
          value && (
            <TextInput
              label={t("decision.model")}
              disabled={disabled}
              value={value.model}
              onChange={(event) => update({ ...value, model: event.currentTarget.value })}
            />
          )
        )}
        {state && (
          <Alert color={state.available ? "blue" : "orange"}>
            {t(`decision.states.${state.state}`)} — {state.reason}
          </Alert>
        )}
        {(options.isError || status.isError || check.isError || credential.isError) && (
          <Alert color="red">{t("decision.failed")}</Alert>
        )}
        <Group>
          <Button
            variant="light"
            disabled={!value?.model}
            loading={check.isPending}
            onClick={() => check.mutate({ config: value!, personId })}
          >
            {t("decision.check")}
          </Button>
          <Button
            variant="subtle"
            onClick={() => {
              check.reset();
              void options.refetch();
              void status.refetch();
            }}
          >
            {t("decision.retry")}
          </Button>
        </Group>
        <Text size="sm">{t("decision.recovery")}</Text>
        <Group>
          <Button
            component={Link}
            to="/setup?section=intelligence&focus=llm-api-settings"
            variant="subtle"
            onClick={(event) => {
              const target = document.getElementById("llm-api-settings");
              if (target) {
                event.preventDefault();
                target.scrollIntoView({ behavior: "smooth" });
              }
            }}
          >
            {t("decision.providerCredentials")}
          </Button>
          <Button
            component={Link}
            to="/setup?section=intelligence&focus=agent-environment"
            variant="subtle"
            onClick={(event) => {
              const target = document.getElementById("agent-environment");
              if (target) {
                event.preventDefault();
                target.scrollIntoView({ behavior: "smooth" });
              }
            }}
          >
            {t("decision.cliEnvironment")}
          </Button>
        </Group>
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
        <Text size="xs" c="dimmed">
          {t("decision.fallback")}
        </Text>
      </Stack>
    </Card>
  );
}
