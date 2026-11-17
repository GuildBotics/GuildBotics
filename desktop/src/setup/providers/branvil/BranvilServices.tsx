import { Button, Code, CopyButton, Group, Select, Stack, Text, TextInput } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  SERVICE_KINDS,
  type ProjectServices,
  type ServiceKind,
  type ServiceSelection,
} from "../../../api/client";
import { RequiredLabel } from "@guildbotics/ui/formParts";
import type { FieldErrors, ServiceUi } from "../types";
import { branvilKey, createWorkspace, getGitConfig, listWorkspaces } from "./api";

const NAME = /^[A-Za-z0-9_.-]+$/;

type Translate = Parameters<ServiceUi["errors"]>[1];

/** The workspace the other kind already names, when it is Branvil too. */
function shared(services: ProjectServices): Record<string, unknown> {
  const other = [services.code_hosting_service, services.ticket_service].find(
    (service) => service?.name === "branvil",
  );
  return { workspace: String(other?.workspace ?? "") };
}

function isWorkspace(name: string): boolean {
  return NAME.test(name) && name !== "." && name !== "..";
}

function errors(value: ServiceSelection, t: Translate): FieldErrors {
  const result: FieldErrors = {};
  const workspace = String(value.workspace ?? "").trim();
  if (!workspace) {
    result.workspace = t("setup.branvil.workspaceRequired");
  } else if (!isWorkspace(workspace)) {
    result.workspace = t("setup.branvil.workspaceInvalid");
  }
  return result;
}

/** The workspace, chosen from Branvil's or created there. */
function WorkspaceField({
  value,
  onChange,
  error,
}: {
  value: string;
  onChange(workspace: string): void;
  error?: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const key = branvilKey("workspaces");
  const listed = useQuery({ queryKey: key, queryFn: listWorkspaces });
  const create = useMutation({
    mutationFn: () => createWorkspace(name.trim()),
    onSuccess: (created) => {
      queryClient.setQueryData(key, created);
      onChange(name.trim());
      setName("");
    },
  });
  const workspaces = [...new Set([...(listed.data?.workspaces ?? []), value].filter(Boolean))];
  const newName = name.trim();
  return (
    <Stack gap={4}>
      <Select
        label={<RequiredLabel text={t("setup.branvil.workspace")} />}
        aria-label={t("setup.branvil.workspace")}
        aria-required
        description={t("setup.branvil.workspaceHint")}
        data={workspaces}
        value={value || null}
        onChange={(chosen) => onChange(chosen ?? "")}
        error={error}
      />
      <Group align="flex-start" gap="xs">
        <TextInput
          aria-label={t("setup.branvil.newWorkspace")}
          placeholder={t("setup.branvil.newWorkspace")}
          size="xs"
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
          error={
            create.error?.message ||
            (newName && !isWorkspace(newName) ? t("setup.branvil.workspaceInvalid") : "")
          }
        />
        <Button
          size="xs"
          variant="default"
          loading={create.isPending}
          disabled={!isWorkspace(newName)}
          onClick={() => create.mutate()}
        >
          {t("setup.branvil.createWorkspace")}
        </Button>
      </Group>
    </Stack>
  );
}

/** The git configuration that lets the owner's own checkouts on this machine
 * reach Branvil: git asks GuildBotics for this machine's token instead of a
 * name and a password. */
function GitConfig() {
  const { t } = useTranslation();
  const config = useQuery({ queryKey: branvilKey("git_config"), queryFn: getGitConfig }).data
    ?.git_config;
  if (!config) {
    return null;
  }
  return (
    <Stack gap={4}>
      <Text size="sm" fw={500}>
        {t("setup.branvil.git.title")}
      </Text>
      <Text size="xs" c="dimmed">
        {t("setup.branvil.git.hint")}
      </Text>
      <Code block aria-label={t("setup.branvil.git.title")}>
        {config}
      </Code>
      <Group>
        <CopyButton value={config}>
          {({ copied, copy }) => (
            <Button size="compact-sm" variant="light" onClick={copy}>
              {t(copied ? "setup.branvil.git.copied" : "setup.branvil.git.copy")}
            </Button>
          )}
        </CopyButton>
      </Group>
    </Stack>
  );
}

/** The items of one kind: the git configuration of the owner's own checkouts,
 * shown with the first kind that names Branvil (it is the same for every kind
 * and every workspace), and the workspace whose repositories members write to
 * and whose board they take tickets from. */
function service(kind: ServiceKind): ServiceUi {
  function Fields({
    value,
    onChange,
    errors: fieldErrors,
    services,
  }: {
    value: ServiceSelection;
    onChange(value: ServiceSelection): void;
    errors: FieldErrors;
    services: ProjectServices;
  }) {
    const first = SERVICE_KINDS.find((other) => services[other]?.name === "branvil") === kind;
    return (
      <Stack>
        {first ? <GitConfig /> : null}
        <WorkspaceField
          value={String(value.workspace ?? "")}
          onChange={(workspace) => onChange({ ...value, workspace })}
          error={fieldErrors.workspace}
        />
      </Stack>
    );
  }
  return { initial: shared, errors, Fields };
}

export const branvilCodeHosting = service("code_hosting_service");

export const branvilTickets = service("ticket_service");
