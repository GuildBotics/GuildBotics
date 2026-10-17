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
import { branvilKey, createWorkspace, listWorkspaces } from "./api";
import { BranvilConnection, useConnection } from "./BranvilConnection";

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

/** The workspace, chosen from the connected Branvil's or created there. */
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
  const connected = useConnection().data?.status === "connected";
  const key = branvilKey("workspaces");
  const listed = useQuery({
    queryKey: key,
    queryFn: listWorkspaces,
    enabled: connected,
  });
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

/** The items of one kind: the connection to Branvil, shown with the first kind
 * that names Branvil (it is the identities', the same for every kind and every
 * workspace), and the workspace whose repositories members write to and whose
 * board they take tickets from. */
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
        {first ? <BranvilConnection /> : null}
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
