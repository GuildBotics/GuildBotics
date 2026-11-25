import { Button, Select, Stack, Text } from "@mantine/core";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { normalizeLanguage, setAppLanguage } from "../../i18n";
import { useT } from "../i18n";
import type { CurrentWorkspace } from "../workspace";
import { NewWorkspaceModal } from "./NewWorkspace";

const NEW_WORKSPACE = "\u0000new";

export function WorkspaceSelect({
  current,
  available,
  onWorkspaceChange,
  children,
}: {
  current: CurrentWorkspace;
  available: boolean;
  onWorkspaceChange: (name: string) => void;
  children: ReactNode;
}) {
  const t = useT();
  const { t: appT, i18n } = useTranslation();
  const [creating, setCreating] = useState(false);
  const { name, names, query } = current;
  const options = [...(names ?? [])];
  if (name && !options.includes(name)) options.unshift(name);
  return (
    <>
      {available && (
        <Stack gap={6}>
          <Text component="label" htmlFor="workspace-switch" className="sidebar-field-label">
            {t("workspace.label")}
          </Text>
          <Select
            id="workspace-switch"
            classNames={{ input: "sidebar-input" }}
            value={name}
            disabled={query.isPending || query.isError}
            placeholder={query.isPending ? t("common.loading") : undefined}
            data={[
              ...options,
              { group: " ", items: [{ value: NEW_WORKSPACE, label: t("workspace.newMenu") }] },
            ]}
            onChange={(next) => {
              if (next === NEW_WORKSPACE) setCreating(true);
              else if (next && next !== name) onWorkspaceChange(next);
            }}
            allowDeselect={false}
          />
          {query.isError && (
            <>
              <Text size="sm" role="alert">
                {t("workspace.listFailed")}
              </Text>
              <Button
                size="compact-sm"
                variant="subtle"
                disabled={query.isFetching}
                onClick={() => void query.refetch()}
              >
                {appT("common.retry")}
              </Button>
            </>
          )}
        </Stack>
      )}
      {children}
      <Stack gap={6} className="sidebar-language">
        <Text component="label" htmlFor="language-switch" className="sidebar-field-label">
          {t("app.language.label")}
        </Text>
        <Select
          id="language-switch"
          classNames={{ input: "sidebar-input" }}
          value={normalizeLanguage(i18n.resolvedLanguage ?? i18n.language) ?? "en"}
          data={[
            { value: "ja", label: "日本語" },
            { value: "en", label: "English" },
          ]}
          onChange={(value) => {
            if (value === "en" || value === "ja") void setAppLanguage(value);
          }}
          allowDeselect={false}
        />
      </Stack>
      <NewWorkspaceModal opened={creating} onClose={() => setCreating(false)} />
    </>
  );
}
