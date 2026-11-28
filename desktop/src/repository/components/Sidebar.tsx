import {
  ActionIcon,
  Button,
  Menu,
  Select,
  Stack,
  Text,
  Tooltip,
  VisuallyHidden,
} from "@mantine/core";
import { Check, Languages, Tag } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { normalizeLanguage, setAppLanguage } from "../../i18n";
import { useSidebarCompact } from "../../ui/AppShell";
import type { HubStatus } from "../../ui/HubStatus";
import { useT } from "../i18n";
import type { CurrentWorkspace } from "../workspace";
import { NewWorkspaceModal } from "./NewWorkspace";

const NEW_WORKSPACE = "\u0000new";
const LANGUAGES = [
  { value: "ja", label: "日本語" },
  { value: "en", label: "English" },
];

/**
 * Up to two letters naming a workspace on the icon rail: the first letters of
 * its first two words (split at separators and capitals), or else its first two
 * characters.
 */
export function workspaceInitials(name: string): string {
  const words = name
    .split(/[\s._-]+/)
    .flatMap((part) => part.match(/[A-Z]?[^A-Z]*/g)?.filter(Boolean) ?? []);
  const letters =
    words.length > 1
      ? words.slice(0, 2).map((word) => Array.from(word)[0])
      : Array.from(words[0] ?? "").slice(0, 2);
  return letters.join("").toUpperCase();
}

/** A rail button that opens a menu of choices, naming itself in a tooltip. */
function RailMenu({
  label,
  target,
  items,
  current,
  onChoose,
  extra,
}: {
  label: string;
  target: ReactNode;
  items: { value: string; label: string }[];
  current: string | null | undefined;
  onChoose: (value: string) => void;
  extra?: ReactNode;
}) {
  return (
    <Menu position="right-start">
      <Tooltip label={label} position="right">
        <Menu.Target>
          <ActionIcon variant="light" size="lg" aria-label={label}>
            {target}
          </ActionIcon>
        </Menu.Target>
      </Tooltip>
      <Menu.Dropdown>
        {items.map((item) => (
          <Menu.Item
            key={item.value}
            onClick={() => onChoose(item.value)}
            rightSection={item.value === current ? <Check size={14} aria-hidden /> : null}
          >
            {item.label}
          </Menu.Item>
        ))}
        {extra}
      </Menu.Dropdown>
    </Menu>
  );
}

export function WorkspaceSelect({
  current,
  available,
  onWorkspaceChange,
  hubVersion,
  children,
}: {
  current: CurrentWorkspace;
  available: boolean;
  onWorkspaceChange: (name: string) => void;
  hubVersion?: HubStatus["current"];
  children: ReactNode;
}) {
  const t = useT();
  const { t: appT, i18n } = useTranslation();
  const [creating, setCreating] = useState(false);
  const { name, names, query } = current;
  const options = [...(names ?? [])];
  if (name && !options.includes(name)) options.unshift(name);
  const compact = useSidebarCompact();
  const language = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language) ?? "en";
  const chooseWorkspace = (next: string | null) => {
    if (next === NEW_WORKSPACE) setCreating(true);
    else if (next && next !== name) onWorkspaceChange(next);
  };
  const chooseLanguage = (value: string | null) => {
    if (value === "en" || value === "ja") void setAppLanguage(value);
  };
  return (
    <>
      {available && compact && (
        <RailMenu
          label={name ? t("workspace.current", { name }) : t("workspace.label")}
          target={
            <Text span fz={12} fw={700}>
              {name ? workspaceInitials(name) : "…"}
            </Text>
          }
          items={query.isError ? [] : options.map((option) => ({ value: option, label: option }))}
          current={name}
          onChoose={chooseWorkspace}
          extra={
            query.isError ? (
              <>
                <Menu.Label>{t("workspace.listFailed")}</Menu.Label>
                <Menu.Item onClick={() => void query.refetch()}>{appT("common.retry")}</Menu.Item>
              </>
            ) : (
              <>
                <Menu.Divider />
                <Menu.Item onClick={() => setCreating(true)}>{t("workspace.newMenu")}</Menu.Item>
              </>
            )
          }
        />
      )}
      {available && !compact && (
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
            onChange={chooseWorkspace}
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
      <Stack gap={6} align={compact ? "center" : undefined} className="sidebar-language">
        {compact ? (
          <RailMenu
            label={t("app.language.label")}
            target={<Languages size={18} />}
            items={LANGUAGES}
            current={language}
            onChoose={chooseLanguage}
          />
        ) : (
          <>
            <Text component="label" htmlFor="language-switch" className="sidebar-field-label">
              {t("app.language.label")}
            </Text>
            <Select
              id="language-switch"
              classNames={{ input: "sidebar-input" }}
              value={language}
              data={LANGUAGES}
              onChange={chooseLanguage}
              allowDeselect={false}
            />
          </>
        )}
        {hubVersion && <SidebarHubVersion version={hubVersion} compact={compact} />}
      </Stack>
      <NewWorkspaceModal opened={creating} onClose={() => setCreating(false)} />
    </>
  );
}

function SidebarHubVersion({
  version,
  compact,
}: {
  version: NonNullable<HubStatus["current"]>;
  compact: boolean;
}) {
  const { t } = useTranslation();
  const name = version.tag ?? version.sha.slice(0, 8);
  const label = t("hub.running");
  const identity = t("hub.versionLabel", { sha: version.sha, subject: version.subject });
  return (
    <Tooltip
      label={
        <>
          <div>{label}</div>
          {compact && <div>{name}</div>}
          {identity}
        </>
      }
      position={compact ? "right" : "top"}
      multiline
      w={280}
      style={{ overflowWrap: "anywhere" }}
      events={{ hover: true, focus: true, touch: false }}
    >
      <Text size="xs" c="dimmed" truncate tabIndex={0}>
        {compact ? (
          <>
            <Tag size={18} aria-hidden />
            <VisuallyHidden>{`${label} ${name}`}</VisuallyHidden>
          </>
        ) : (
          name
        )}
      </Text>
    </Tooltip>
  );
}
