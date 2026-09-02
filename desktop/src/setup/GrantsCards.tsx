import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useQuery } from "@tanstack/react-query";
import { FolderOpen, Plus, Trash2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  evaluateGrant,
  type DocumentGrant,
  type GrantAccess,
  type GrantScope,
  type LocalGrants,
  type SandboxAccessStatus,
  type SharedGrants,
} from "../api/client";

/** What a row or a new entry may be granted: the two accesses, or closed. */
type Access = GrantAccess | "deny";
const GRANT_ACCESSES: Access[] = ["read", "read_write"];
const DEVICE_ACCESSES: Access[] = ["read", "read_write", "deny"];

/** Keystrokes settle for this long before the device is asked about the text. */
const LOOKUP_DEBOUNCE_MS = 300;

/**
 * What an agent gets beyond its working directory, as two cards of the same
 * shape: the directories the workspace shares, and what this device opens or
 * closes. The lists are the setting; everything else only judges.
 */
export function GrantsCards({
  shared,
  local,
  status,
  onSharedChange,
  onLocalChange,
}: {
  shared: SharedGrants;
  local: LocalGrants;
  /** How the saved grants resolve on this device, once the preview has them. */
  status?: SandboxAccessStatus;
  onSharedChange: (shared: SharedGrants) => void;
  onLocalChange: (local: LocalGrants) => void;
}) {
  return (
    <>
      <DocumentsCard
        documents={shared.documents}
        status={status}
        onChange={(documents) => onSharedChange({ ...shared, documents })}
      />
      <DeviceAccessCard local={local} status={status} onChange={onLocalChange} />
    </>
  );
}

/** A saved path as the device shows it: `$HOME/x` for a home-relative `x`. */
function shown(path: string): string {
  return path.startsWith("/") ? path : `$HOME/${path}`;
}

/** The saved form of a path the device showed: home-relative when under it. */
function saved(path: string): string {
  return path.startsWith("$HOME/") ? path.slice("$HOME/".length) : path;
}

/** A picked absolute path, relative to the home directory when under it. */
function homeRelative(path: string, home: string): string {
  return path.startsWith(`${home}/`) ? path.slice(home.length + 1) : path;
}

function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** The workspace's shared directories under the home. */
function DocumentsCard({
  documents,
  status,
  onChange,
}: {
  documents: DocumentGrant[];
  status?: SandboxAccessStatus;
  onChange: (documents: DocumentGrant[]) => void;
}) {
  const { t } = useTranslation();
  const present = (path: string) =>
    status?.documents.find((row) => row.path === `$HOME/${path}`)?.present;
  return (
    <AccessCard
      id="grants-documents"
      testId="grants:document"
      title={t("setup.intelligence.documents.title")}
      description={t("setup.intelligence.documents.description")}
      empty={t("setup.intelligence.documents.empty")}
      placeholder={t("setup.intelligence.documents.pathPlaceholder")}
      accesses={GRANT_ACCESSES}
      scopeOf={() => "document"}
      withinHome
      taken={(path) => documents.some((grant) => grant.path === path)}
      onAdd={(path, access) => onChange([...documents, { path, access: access as GrantAccess }])}
      rows={documents.map((grant) => ({
        key: grant.path,
        name: grant.path,
        path: (
          <Group gap="xs">
            <Text size="sm" ff="monospace">
              {grant.path}
            </Text>
            <PresenceBadge present={present(grant.path)} />
          </Group>
        ),
        access: (
          <AccessSelect
            path={grant.path}
            accesses={GRANT_ACCESSES}
            value={grant.access}
            onChange={(access) =>
              onChange(
                documents.map((g) =>
                  g.path === grant.path ? { ...g, access: access as GrantAccess } : g,
                ),
              )
            }
          />
        ),
        onRemove: () => onChange(documents.filter((g) => g.path !== grant.path)),
      }))}
    />
  );
}

/**
 * Every directory this device opens or closes for agents: the paths and
 * denies the user added (one three-way access each, so a deny can become a
 * grant in place), the install locations its PATH leads to (readable unless
 * denied), and what stays closed regardless -- the built-in credential
 * directories and PATH entries that fall inside them.
 */
function DeviceAccessCard({
  local,
  status,
  onChange,
}: {
  local: LocalGrants;
  status?: SandboxAccessStatus;
  onChange: (local: LocalGrants) => void;
}) {
  const { t } = useTranslation();
  const trees = status?.trees ?? [];
  const treePaths = new Set(trees.map((tree) => tree.path));
  const denied = (displayed: string) => local.deny.some((entry) => shown(entry) === displayed);
  const own: { path: string; access: Access }[] = [
    ...local.paths,
    ...local.deny
      .filter((entry) => !treePaths.has(shown(entry)))
      .map((entry) => ({ path: entry, access: "deny" as const })),
  ];
  const setOwn = (target: string, access: Access | null) =>
    onChange({
      paths: [
        ...local.paths.filter((g) => g.path !== target),
        ...(access && access !== "deny" ? [{ path: target, access }] : []),
      ],
      deny: [
        ...local.deny.filter((entry) => entry !== target),
        ...(access === "deny" ? [target] : []),
      ],
    });
  const setTree = (displayed: string, deny: boolean) =>
    onChange({
      ...local,
      deny: deny
        ? [...local.deny, saved(displayed)]
        : local.deny.filter((entry) => shown(entry) !== displayed),
    });
  const closed = [
    ...(status?.denied.filter((d) => d.builtin) ?? []).map((d) => ({
      path: d.path,
      note: t("setup.intelligence.deviceAccess.builtin"),
    })),
    ...(status?.excluded ?? []).map((tree) => ({ path: tree.path, note: tree.reason })),
  ];
  const mono = (text: string, dimmed = false) => (
    <Text size="sm" ff="monospace" c={dimmed ? "dimmed" : undefined}>
      {text}
    </Text>
  );

  return (
    <AccessCard
      id="grants-device"
      testId="grants:device"
      title={t("setup.intelligence.deviceAccess.title")}
      description={t("setup.intelligence.deviceAccess.description")}
      empty={t("setup.intelligence.deviceAccess.empty")}
      placeholder={t("setup.intelligence.deviceAccess.pathPlaceholder")}
      accesses={DEVICE_ACCESSES}
      scopeOf={(access) => (access === "deny" ? "deny" : "local")}
      taken={(path) => own.some((row) => row.path === path)}
      onAdd={(path, access) => setOwn(path, access)}
      rows={[
        ...own.map((row) => ({
          key: `own:${row.path}`,
          name: row.path,
          path: (
            <Group gap="xs">
              {mono(row.path)}
              {status?.paths.find((g) => g.path === shown(row.path))?.present === false ? (
                <Badge color="danger" variant="light" size="xs">
                  {t("setup.intelligence.grants.absentHere")}
                </Badge>
              ) : null}
            </Group>
          ),
          access: (
            <AccessSelect
              path={row.path}
              accesses={DEVICE_ACCESSES}
              value={row.access}
              onChange={(access) => setOwn(row.path, access)}
            />
          ),
          onRemove: () => setOwn(row.path, null),
        })),
        ...trees.map((tree) => ({
          key: `tree:${tree.path}`,
          name: tree.path,
          path: (
            <>
              {mono(tree.path)}
              <Text size="xs" c="dimmed" ff="monospace">
                {t("setup.intelligence.deviceAccess.source", { sources: tree.sources.join(", ") })}
              </Text>
            </>
          ),
          access: (
            <AccessSelect
              path={tree.path}
              accesses={["read", "deny"]}
              value={denied(tree.path) ? "deny" : "read"}
              onChange={(access) => setTree(tree.path, access === "deny")}
            />
          ),
        })),
        ...closed.map((row) => ({
          key: `closed:${row.path}`,
          name: row.path,
          path: (
            <Group gap="xs">
              {mono(row.path, true)}
              <Badge color="gray" variant="light" size="xs">
                {row.note}
              </Badge>
            </Group>
          ),
          access: (
            <Badge color="gray" variant="light" size="sm">
              {t("setup.intelligence.deviceAccess.deny")}
            </Badge>
          ),
        })),
      ]}
    />
  );
}

type AccessRow = {
  key: string;
  /** The path as saved, naming the row's controls. */
  name: string;
  path: ReactNode;
  access: ReactNode;
  /** Absent for rows the user cannot remove (derived or built in). */
  onRemove?: () => void;
};

/**
 * One card of the settings: a description, a form that judges a path before
 * it is added, and the table of rows. Both cards are this, so they cannot
 * drift apart in layout.
 */
function AccessCard({
  id,
  testId,
  title,
  description,
  empty,
  placeholder,
  accesses,
  scopeOf,
  withinHome = false,
  taken,
  onAdd,
  rows,
}: {
  /** Anchor for a system alert to scroll to. */
  id: string;
  testId: string;
  title: string;
  description: string;
  empty: string;
  placeholder: string;
  accesses: Access[];
  /** Which judgement a path with this access needs. */
  scopeOf: (access: Access) => GrantScope;
  withinHome?: boolean;
  taken: (path: string) => boolean;
  onAdd: (path: string, access: Access) => void;
  rows: AccessRow[];
}) {
  const { t } = useTranslation();
  const [path, setPath] = useState("");
  const [access, setAccess] = useState<Access>("read");
  const scope = scopeOf(access);
  const { typed, judged } = useGrantJudgement(scope, path, access);
  // Judged against what the field shows now, not the debounced text: right
  // after an add the field is empty while the old text is still settling.
  const duplicate = taken(path.trim());
  const canAdd = Boolean(typed) && !duplicate && judged?.valid === true;
  const narrow = { whiteSpace: "nowrap" as const, width: 1 };

  return (
    <Card id={id} withBorder radius="sm" p="md" data-testid={testId}>
      <Stack gap="md">
        <div>
          <Text fw={700} size="sm">
            {title}
          </Text>
          <Text size="sm" c="dimmed">
            {description}
          </Text>
        </div>
        <Group align="flex-end" gap="xs" wrap="wrap">
          <PathInput
            placeholder={placeholder}
            value={path}
            withinHome={withinHome}
            onChange={setPath}
            error={
              duplicate
                ? t("setup.intelligence.grants.duplicate")
                : judged && !judged.valid
                  ? judged.reason
                  : undefined
            }
          />
          <Select
            label={t("setup.intelligence.grants.access")}
            size="xs"
            data={accesses.map((value) => ({ value, label: accessLabel(t, value) }))}
            value={access}
            onChange={(value) => setAccess((value as Access) ?? "read")}
            allowDeselect={false}
            style={{ width: 160 }}
          />
          <Button
            size="xs"
            leftSection={<Plus size={14} />}
            disabled={!canAdd}
            onClick={() => {
              onAdd(typed, access);
              setPath("");
            }}
          >
            {t("setup.intelligence.grants.add")}
          </Button>
        </Group>
        {judged?.valid && judged.sensitive ? (
          <Alert
            color="warning"
            variant="light"
            title={t("setup.intelligence.grants.sensitiveTitle")}
          >
            {t("setup.intelligence.grants.sensitiveBody", {
              path: typed,
              reason: judged.sensitive,
            })}
          </Alert>
        ) : null}
        {judged?.valid && !judged.present && scope === "document" ? (
          <Text size="xs" c="dimmed">
            {t("setup.intelligence.grants.absentHere")}
          </Text>
        ) : null}
        {rows.length === 0 ? (
          <Text size="sm" c="dimmed">
            {empty}
          </Text>
        ) : (
          <Table withTableBorder={false} verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("setup.intelligence.grants.path")}</Table.Th>
                <Table.Th style={narrow}>{t("setup.intelligence.grants.access")}</Table.Th>
                <Table.Th style={narrow} />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.map((row) => (
                <Table.Tr key={row.key}>
                  <Table.Td>{row.path}</Table.Td>
                  <Table.Td style={narrow}>{row.access}</Table.Td>
                  <Table.Td style={narrow}>
                    {row.onRemove ? (
                      <ActionIcon
                        aria-label={t("setup.intelligence.grants.remove", { path: row.name })}
                        color="danger"
                        variant="subtle"
                        size="sm"
                        onClick={row.onRemove}
                      >
                        <Trash2 size={14} />
                      </ActionIcon>
                    ) : null}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Stack>
    </Card>
  );
}

function accessLabel(t: (key: string) => string, value: Access): string {
  return value === "deny"
    ? t("setup.intelligence.deviceAccess.deny")
    : t(`setup.intelligence.grants.accessLabels.${value}`);
}

function AccessSelect({
  path,
  accesses,
  value,
  onChange,
}: {
  path: string;
  accesses: Access[];
  value: Access;
  onChange: (value: Access) => void;
}) {
  const { t } = useTranslation();
  return (
    <Select
      size="xs"
      aria-label={t("setup.intelligence.grants.accessFor", { path })}
      data={accesses.map((option) => ({ value: option, label: accessLabel(t, option) }))}
      value={value}
      onChange={(next) => next && onChange(next as Access)}
      allowDeselect={false}
      style={{ width: 130 }}
    />
  );
}

/** Ask the device what a typed path would mean, once the typing settles. */
function useGrantJudgement(scope: GrantScope, path: string, access: Access) {
  const [typed] = useDebouncedValue(path.trim(), LOOKUP_DEBOUNCE_MS);
  const settled = typed === path.trim();
  const grantAccess = access === "deny" ? undefined : access;
  const evaluation = useQuery({
    queryKey: ["grant-evaluation", scope, typed, grantAccess ?? ""],
    queryFn: () => evaluateGrant({ scope, path: typed, access: grantAccess }),
    enabled: typed.length > 0,
  });
  // The judgement is about the debounced text; while newer keystrokes are
  // pending it would describe something the field no longer shows.
  return { typed, judged: typed && settled ? evaluation.data : undefined };
}

/**
 * A path field with a directory picker. A choice under the home directory is
 * shown relative to it, which is the form both grant files save; `withinHome`
 * starts the dialog there for the shared documents, whose paths cannot leave it.
 */
function PathInput({
  placeholder,
  value,
  withinHome,
  error,
  onChange,
}: {
  placeholder: string;
  value: string;
  withinHome: boolean;
  error?: string;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  const [picking, setPicking] = useState(false);
  const pickDirectory = async () => {
    setPicking(true);
    try {
      if (!isTauriRuntime()) return;
      const [{ open }, { homeDir }] = await Promise.all([
        import("@tauri-apps/plugin-dialog"),
        import("@tauri-apps/api/path"),
      ]);
      const home = (await homeDir()).replace(/[\\/]+$/, "");
      const selected = await open({
        directory: true,
        multiple: false,
        defaultPath: withinHome ? home : undefined,
      });
      if (typeof selected === "string") onChange(homeRelative(selected, home));
    } finally {
      setPicking(false);
    }
  };
  return (
    <TextInput
      label={t("setup.intelligence.grants.path")}
      placeholder={placeholder}
      size="xs"
      value={value}
      onChange={(event) => onChange(event.currentTarget.value)}
      style={{ flex: 1, minWidth: 220 }}
      rightSectionWidth={30}
      rightSection={
        <ActionIcon
          aria-label={t("setup.intelligence.grants.choose")}
          variant="subtle"
          size="sm"
          loading={picking}
          onClick={pickDirectory}
        >
          <FolderOpen size={14} />
        </ActionIcon>
      }
      error={error}
    />
  );
}

function PresenceBadge({ present }: { present: boolean | undefined }) {
  const { t } = useTranslation();
  if (present === undefined) return null;
  return present ? (
    <Badge color="success" variant="light" size="xs">
      {t("setup.intelligence.grants.presentHere")}
    </Badge>
  ) : (
    <Badge color="gray" variant="light" size="xs">
      {t("setup.intelligence.grants.absentHere")}
    </Badge>
  );
}
