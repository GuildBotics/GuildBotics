import { Badge, Button, Fieldset, Group, Select, Stack, Text, TextInput } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { ProjectServices, ServiceSelection } from "../../../api/client";
import { InfoCallout, RequiredLabel } from "../../formParts";
import type { FieldErrors, ServiceUi } from "../types";
import {
  DEFAULT_LANE_MAP,
  ensureAgentField,
  getAgentFieldState,
  getStatusOptions,
  type AgentFieldState,
  type BoardTarget,
  type LaneMap,
} from "./api";

/** A Projects board URL, as the backend accepts it. */
export function parseProjectUrl(projectUrl: string) {
  const match = /^https:\/\/github\.com\/(orgs|users)\/([^/\s]+)\/projects\/(\d+)\/?$/.exec(
    projectUrl.trim(),
  );
  return {
    valid: match !== null,
    owner: match?.[2] ?? "",
    // Only a project under an organization has one to create a GitHub App in.
    organization: match?.[1] === "orgs" ? match[2] : "",
  };
}

/** The board the project's ticket service names, when it is a GitHub board. */
export function boardTarget(services: ProjectServices): BoardTarget | null {
  const board = services.ticket_service;
  const url = typeof board?.url === "string" ? board.url : "";
  return board?.name === "github" && parseProjectUrl(url).valid ? { url } : null;
}

export const agentFieldQueryKey = (target: BoardTarget | null) => ["github", "agent-field", target];

function laneMap(value: ServiceSelection): LaneMap {
  return { ...DEFAULT_LANE_MAP, ...(value.lane_map as Partial<LaneMap> | undefined) };
}

export const githubBoard: ServiceUi = {
  initial: () => ({ url: "", lane_map: DEFAULT_LANE_MAP }),
  errors(value, t): FieldErrors {
    const errors: FieldErrors = {};
    const url = String(value.url ?? "");
    if (!url.trim()) {
      errors.url = t("setup.validation.githubProjectRequired");
    } else if (!parseProjectUrl(url).valid) {
      errors.url = t("setup.validation.githubProjectInvalid");
    }
    const lanes = laneMap(value);
    if (
      (lanes.ready.trim() || DEFAULT_LANE_MAP.ready) ===
      (lanes.done.trim() || DEFAULT_LANE_MAP.done)
    ) {
      errors.done = t("setup.validation.laneReadyDoneSame");
    }
    return errors;
  },
  Fields: BoardFields,
};

export const githubCodeHosting: ServiceUi = {
  // The repositories are usually the board's owner's.
  initial: (services) => ({
    owner: parseProjectUrl(String(services.ticket_service?.url ?? "")).owner,
  }),
  errors: (value, t): FieldErrors =>
    String(value.owner ?? "").trim() ? {} : { owner: t("setup.github.ownerRequired") },
  Fields: function CodeHostingFields({ value, onChange, errors }) {
    const { t } = useTranslation();
    return (
      <TextInput
        label={<RequiredLabel text={t("setup.github.owner")} />}
        aria-label={t("setup.github.owner")}
        aria-required
        description={t("setup.github.ownerHint")}
        value={String(value.owner ?? "")}
        onChange={(event) => onChange({ ...value, owner: event.currentTarget.value })}
        error={errors.owner}
      />
    );
  },
};

function BoardFields({
  value,
  onChange,
  errors,
}: {
  value: ServiceSelection;
  onChange(value: ServiceSelection): void;
  errors: FieldErrors;
}) {
  const { t } = useTranslation();
  const url = String(value.url ?? "");
  const target = parseProjectUrl(url).valid ? { url: url.trim() } : null;
  const lanes = laneMap(value);
  // Status options are read live for the URL being edited, so they appear
  // before the project is saved.
  const statusOptions = useQuery({
    queryKey: ["github", "status-options", target],
    queryFn: () => getStatusOptions(target as BoardTarget),
    enabled: target !== null,
  });
  const choices = target && statusOptions.data?.available ? statusOptions.data.statuses : [];
  const setLane = (lane: keyof LaneMap, name: string) =>
    onChange({ ...value, lane_map: { ...lanes, [lane]: name } });
  const laneLabels = {
    ready: t("setup.github.laneReady"),
    working: t("setup.github.laneWorking"),
    done: t("setup.github.laneDone"),
  };
  return (
    <Stack>
      <TextInput
        label={<RequiredLabel text={t("setup.github.projectUrl")} />}
        aria-label={t("setup.github.projectUrl")}
        aria-required
        description={t("setup.github.projectUrlHint")}
        value={url}
        onChange={(event) => onChange({ ...value, url: event.currentTarget.value })}
        error={errors.url}
      />
      {target ? (
        <>
          <Fieldset legend={t("setup.github.laneMapping")} radius="md">
            <Stack>
              <Text size="sm" c="dimmed">
                {choices.length > 0
                  ? t("setup.github.laneMappingHint")
                  : t("setup.github.laneMappingManualHint")}
              </Text>
              {(["ready", "working", "done"] as const).map((lane) => (
                <LaneField
                  key={lane}
                  label={laneLabels[lane]}
                  placeholder={DEFAULT_LANE_MAP[lane]}
                  description={lane === "working" ? t("setup.github.laneWorkingHint") : undefined}
                  choices={choices}
                  value={lanes[lane]}
                  onChange={(name) => setLane(lane, name)}
                  error={lane === "done" ? errors.done : undefined}
                />
              ))}
            </Stack>
          </Fieldset>
          <AgentFieldPanel target={target} />
        </>
      ) : null}
    </Stack>
  );
}

function LaneField({
  label,
  placeholder,
  description,
  choices,
  value,
  onChange,
  error,
}: {
  label: string;
  placeholder: string;
  description?: string;
  choices: string[];
  value: string;
  onChange(value: string): void;
  error?: ReactNode;
}) {
  // When status options were read from the Project, pick strictly from them:
  // a non-searchable Select shows every option and disallows values that are
  // not real board lanes. Only without options is the name typed.
  if (choices.length > 0) {
    return (
      <Select
        label={label}
        aria-label={label}
        placeholder={placeholder}
        description={description}
        data={choices}
        searchable={false}
        allowDeselect={false}
        value={value || null}
        onChange={(next) => onChange(next ?? "")}
        error={error}
      />
    );
  }
  return (
    <TextInput
      label={label}
      aria-label={label}
      placeholder={placeholder}
      description={description}
      value={value}
      onChange={(event) => onChange(event.currentTarget.value)}
      error={error}
    />
  );
}

function AgentFieldMemberBadges({
  options,
  color,
  variant,
}: {
  options: AgentFieldState["options"];
  color: string;
  variant: "light" | "outline";
}) {
  return (
    <Group gap="xs">
      {options.map((option) => (
        <Badge key={option.name} color={color} variant={variant}>
          {option.description || option.name}
        </Badge>
      ))}
    </Group>
  );
}

function AgentFieldActionLabel({ state }: { state: AgentFieldState }) {
  const { t } = useTranslation();
  if (!state.exists) {
    return <>{t("setup.github.agentFieldCreate")}</>;
  }
  if (state.missing.length > 0) {
    return <>{t("setup.github.agentFieldAddMembers", { count: state.missing.length })}</>;
  }
  return <>{t("setup.github.agentFieldUpToDate")}</>;
}

function AgentFieldPanel({ target }: { target: BoardTarget }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const stateQuery = useQuery({
    queryKey: agentFieldQueryKey(target),
    queryFn: () => getAgentFieldState(target),
  });
  const ensureMutation = useMutation({
    mutationFn: () => ensureAgentField(target),
    onSuccess: (data) => {
      queryClient.setQueryData(agentFieldQueryKey(target), data);
    },
  });
  const state = stateQuery.data;

  return (
    <Fieldset legend={t("setup.github.agentField")} radius="md">
      <Stack>
        <Text size="sm" c="dimmed">
          {t("setup.github.agentFieldHint")}
        </Text>
        {stateQuery.isLoading ? (
          <Text size="sm" c="dimmed">
            {t("setup.github.agentFieldLoading")}
          </Text>
        ) : !state?.available ? (
          <InfoCallout title={t("setup.github.agentFieldUnavailableTitle")}>
            {t("setup.github.agentFieldUnavailableHint")}
          </InfoCallout>
        ) : (
          <>
            <Group gap="xs">
              <Text size="sm" fw={500}>
                {t("setup.github.agentFieldStatus")}
              </Text>
              <Badge color={state.exists ? "success" : "neutral"} variant="light">
                {state.exists
                  ? t("setup.github.agentFieldExists")
                  : t("setup.github.agentFieldMissing")}
              </Badge>
            </Group>
            {state.exists && (
              <div>
                <Text size="sm" fw={500} mb={4}>
                  {t("setup.github.agentFieldRegistered")}
                </Text>
                {state.options.length > 0 ? (
                  <AgentFieldMemberBadges options={state.options} color="neutral" variant="light" />
                ) : (
                  <Text size="sm" c="dimmed">
                    {t("setup.github.agentFieldNoMembers")}
                  </Text>
                )}
              </div>
            )}
            {state.missing.length > 0 && (
              <div>
                <Text size="sm" fw={500} mb={4}>
                  {t("setup.github.agentFieldMissingMembers")}
                </Text>
                <AgentFieldMemberBadges options={state.missing} color="warning" variant="outline" />
              </div>
            )}
            <Group>
              <Button
                onClick={() => ensureMutation.mutate()}
                loading={ensureMutation.isPending}
                disabled={state.exists && state.missing.length === 0}
              >
                <AgentFieldActionLabel state={state} />
              </Button>
            </Group>
            {ensureMutation.isError && (
              <Text size="sm" c="danger">
                {t("setup.github.agentFieldError")}
              </Text>
            )}
          </>
        )}
      </Stack>
    </Fieldset>
  );
}
