import { Stack, TextInput } from "@mantine/core";
import { useTranslation } from "react-i18next";

import type { ProjectServices, ServiceSelection } from "../../../api/client";
import { RequiredLabel } from "../../formParts";
import type { FieldErrors, ServiceUi } from "../types";

/** Where Branvil is reached unless the project says otherwise: the same on
 * every machine, through Branvil's tunnel on those other than the Hub. */
export const BRANVIL_BASE_URL = "http://127.0.0.1:7878";

const ORIGIN = /^https?:\/\/[^/\s?#@]+\/?$/;
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

function errors(value: ServiceSelection, t: Translate): FieldErrors {
  const result: FieldErrors = {};
  const baseUrl = String(value.base_url ?? "").trim();
  if (baseUrl && !ORIGIN.test(baseUrl)) {
    result.base_url = t("setup.branvil.baseUrlInvalid");
  }
  const workspace = String(value.workspace ?? "").trim();
  if (!workspace) {
    result.workspace = t("setup.branvil.workspaceRequired");
  } else if (!NAME.test(workspace) || workspace === "." || workspace === "..") {
    result.workspace = t("setup.branvil.workspaceInvalid");
  }
  return result;
}

function Fields({
  value,
  onChange,
  errors: fieldErrors,
}: {
  value: ServiceSelection;
  onChange(value: ServiceSelection): void;
  errors: FieldErrors;
}) {
  const { t } = useTranslation();
  const field = (key: string, label: string, hint: string, required = false) => (
    <TextInput
      label={required ? <RequiredLabel text={label} /> : label}
      aria-label={label}
      aria-required={required}
      description={hint}
      value={String(value[key] ?? "")}
      onChange={(event) => onChange({ ...value, [key]: event.currentTarget.value })}
      error={fieldErrors[key]}
    />
  );
  return (
    <Stack>
      {field("base_url", t("setup.branvil.baseUrl"), t("setup.branvil.baseUrlHint"))}
      {field("workspace", t("setup.branvil.workspace"), t("setup.branvil.workspaceHint"), true)}
    </Stack>
  );
}

/** The code host and the tickets name the same items: the Branvil and the
 * workspace, whose repositories members write to and whose board they take
 * tickets from. */
export const branvilCodeHosting: ServiceUi = { initial: shared, errors, Fields };

export const branvilTickets = branvilCodeHosting;
