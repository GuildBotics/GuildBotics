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

/** Whether ``path`` is names joined by ``/``, at least ``least`` of them. */
function isPath(path: string, least: number): boolean {
  const names = path.split("/");
  return (
    names.length >= least && names.every((name) => NAME.test(name) && name !== "." && name !== "..")
  );
}

/** What the other kind already says of the same Branvil, when it is one. */
function shared(services: ProjectServices): Record<string, unknown> {
  const other = [services.code_hosting_service, services.ticket_service].find(
    (service) => service?.name === "branvil",
  );
  return {
    base_url: String(other?.base_url ?? BRANVIL_BASE_URL),
    owner: String(other?.owner ?? ""),
  };
}

function errors(value: ServiceSelection, t: Translate, board: boolean): FieldErrors {
  const result: FieldErrors = {};
  const baseUrl = String(value.base_url ?? "").trim();
  if (baseUrl && !ORIGIN.test(baseUrl)) {
    result.base_url = t("setup.branvil.baseUrlInvalid");
  }
  const owner = String(value.owner ?? "").trim();
  if (!owner) {
    result.owner = t("setup.branvil.ownerRequired");
  } else if (!isPath(owner, 1)) {
    result.owner = t("setup.branvil.ownerInvalid");
  }
  const boardPath = String(value.board ?? "").trim();
  if (board && boardPath && !isPath(boardPath, 2)) {
    result.board = t("setup.branvil.boardInvalid");
  }
  return result;
}

function Fields({
  value,
  onChange,
  errors: fieldErrors,
  board,
}: {
  value: ServiceSelection;
  onChange(value: ServiceSelection): void;
  errors: FieldErrors;
  board: boolean;
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
      {field("owner", t("setup.branvil.owner"), t("setup.branvil.ownerHint"), true)}
      {board ? field("board", t("setup.branvil.board"), t("setup.branvil.boardHint")) : null}
    </Stack>
  );
}

export const branvilCodeHosting: ServiceUi = {
  initial: shared,
  errors: (value, t) => errors(value, t, false),
  Fields: (props) => <Fields {...props} board={false} />,
};

export const branvilTickets: ServiceUi = {
  initial: (services) => ({ ...shared(services), board: "" }),
  errors: (value, t) => errors(value, t, true),
  Fields: (props) => <Fields {...props} board />,
};
