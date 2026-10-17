import { Button, Group, Select, Stack, TextInput } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  SERVICE_KINDS,
  type ProjectServices,
  type ServiceKind,
  type ServiceSelection,
} from "../../../api/client";
import { RequiredLabel } from "../../formParts";
import type { FieldErrors, ServiceUi } from "../types";
import { BRANVIL_BASE_URL, ORIGIN, branvilKey, createWorkspace, listWorkspaces } from "./api";
import { BranvilConnection, useConnection } from "./BranvilConnection";

const NAME = /^[A-Za-z0-9_.-]+$/;

type Translate = Parameters<ServiceUi["errors"]>[1];

/** What the other kind already says of the same Branvil, when it is one. */
function shared(services: ProjectServices): Record<string, unknown> {
  const other = [services.code_hosting_service, services.ticket_service].find(
    (service) => service?.name === "branvil",
  );
  return {
    base_url: String(other?.base_url ?? BRANVIL_BASE_URL),
    workspace: String(other?.workspace ?? ""),
  };
}

function isWorkspace(name: string): boolean {
  return NAME.test(name) && name !== "." && name !== "..";
}

function errors(value: ServiceSelection, t: Translate): FieldErrors {
  const result: FieldErrors = {};
  const baseUrl = String(value.base_url ?? "").trim();
  if (baseUrl && !ORIGIN.test(baseUrl)) {
    result.base_url = t("setup.branvil.baseUrlInvalid");
  }
  const workspace = String(value.workspace ?? "").trim();
  if (!workspace) {
    result.workspace = t("setup.branvil.workspaceRequired");
  } else if (!isWorkspace(workspace)) {
    result.workspace = t("setup.branvil.workspaceInvalid");
  }
  return result;
}

/** The workspace, chosen from the connected Branvil's or created there. */
function WorkspaceField({
  baseUrl,
  value,
  onChange,
  error,
}: {
  baseUrl: string;
  value: string;
  onChange(workspace: string): void;
  error?: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const connected = useConnection(baseUrl).data?.status === "connected";
  const key = branvilKey("workspaces", baseUrl);
  const listed = useQuery({
    queryKey: key,
    queryFn: () => listWorkspaces(baseUrl),
    enabled: connected,
  });
  const create = useMutation({
    mutationFn: () => createWorkspace(baseUrl, name.trim()),
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
        description={
          connected ? t("setup.branvil.workspaceHint") : t("setup.branvil.workspaceConnect")
        }
        data={workspaces}
        value={value || null}
        onChange={(chosen) => onChange(chosen ?? "")}
        error={error}
      />
      {connected ? (
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
      ) : null}
    </Stack>
  );
}

/** The items of one kind: the Branvil and the connection to it, shown with
 * the first kind that names Branvil (the others follow its base URL), and
 * the workspace whose repositories members write to and whose board they
 * take tickets from. */
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
    const { t } = useTranslation();
    const baseUrl = String(value.base_url ?? "").trim() || BRANVIL_BASE_URL;
    const first = SERVICE_KINDS.find((other) => services[other]?.name === "branvil") === kind;
    return (
      <Stack>
        {first ? (
          <>
            <TextInput
              label={t("setup.branvil.baseUrl")}
              aria-label={t("setup.branvil.baseUrl")}
              description={t("setup.branvil.baseUrlHint")}
              value={String(value.base_url ?? "")}
              onChange={(event) => onChange({ ...value, base_url: event.currentTarget.value })}
              error={fieldErrors.base_url}
            />
            <BranvilConnection baseUrl={baseUrl} />
          </>
        ) : null}
        <WorkspaceField
          baseUrl={baseUrl}
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
