import {
  Badge,
  Box,
  Button,
  Fieldset,
  Group,
  NumberInput,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { EffortFieldSpec, EffortOverlay } from "../api/client";
import { JsonObjectField } from "./JsonObjectField";

/**
 * Levels an effort mapping can configure, in ascending order.
 *
 * `default` is absent by definition rather than by omission: it means "do not
 * intervene", so a mapping for it could never be applied. It is a meaningful
 * *request* (a command's frontmatter or a runtime parameter may ask for it),
 * but it is not something a provider's settings can describe, so it has no row
 * here. The backend rejects it as a mapping key for the same reason.
 */
const EDITABLE_LEVELS = ["low", "high"] as const;

/** A control belongs to an effort level, or to the settings that always apply. */
type Level = (typeof EDITABLE_LEVELS)[number] | "always";

/** Read a dotted path (`thinking.budget_tokens`) out of a settings block. */
function readPath(settings: Record<string, unknown>, key: string): unknown {
  return key.split(".").reduce<unknown>((current, part) => {
    if (current === null || typeof current !== "object") return undefined;
    return (current as Record<string, unknown>)[part];
  }, settings);
}

/** Return a copy of `settings` with the dotted path set, or removed when empty. */
function writePath(
  settings: Record<string, unknown>,
  key: string,
  next: unknown,
): Record<string, unknown> {
  const [head, ...rest] = key.split(".");
  const copy = { ...settings };
  if (rest.length === 0) {
    if (next === undefined) delete copy[head];
    else copy[head] = next;
    return copy;
  }
  const child = copy[head];
  const nested = writePath(
    child !== null && typeof child === "object" ? (child as Record<string, unknown>) : {},
    rest.join("."),
    next,
  );
  // An emptied nested object is removed rather than left as `{}`, so a cleared
  // field does not persist a setting the provider would still act on.
  if (Object.keys(nested).length === 0) delete copy[head];
  else copy[head] = nested;
  return copy;
}

function summarize(settings: Record<string, unknown>, fields: EffortFieldSpec[]): string {
  const described = fields.length > 0 ? fields.map((field) => field.key) : Object.keys(settings);
  const parts = described
    .map((key) => [key, readPath(settings, key)] as const)
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([key, value]) => `${key} = ${String(value)}`);
  return parts.join(", ");
}

function FieldControl({
  field,
  level,
  value,
  modelOptions,
  onChange,
}: {
  field: EffortFieldSpec;
  level: Level;
  value: unknown;
  modelOptions: string[];
  onChange: (next: unknown) => void;
}) {
  const { t } = useTranslation();
  const always = level === "always";
  // Only the *label* depends on the field type: a provider names its model key
  // `id` or `model`, and neither says what it does, so the type supplies the
  // wording without this editor learning any provider's vocabulary.
  const label =
    field.type === "model_id"
      ? t(`setup.intelligence.effort.${always ? "modelAlwaysLabel" : "modelLevelLabel"}`)
      : field.key;
  // What an empty value means belongs to the row, not to the field: on the
  // always-applied row it defers to the tool, on a level row it changes
  // nothing. Deciding this per field type made `model` say one thing and
  // `effort` another where both mean the same.
  const placeholder = t(
    `setup.intelligence.effort.${always ? "emptyIsToolDefault" : "emptyChangesNothing"}`,
  );
  // Repeating one sentence under every control unbalances the row; the group
  // states it once instead.
  const name = `${level} ${label}`;

  if (field.type === "enum") {
    return (
      <Select
        label={label}
        aria-label={name}
        size="xs"
        clearable
        placeholder={placeholder}
        data={field.values}
        value={value === undefined ? null : String(value)}
        onChange={(next) => onChange(next ?? undefined)}
      />
    );
  }
  if (field.type === "integer") {
    return (
      <NumberInput
        label={label}
        aria-label={name}
        size="xs"
        allowDecimal={false}
        placeholder={placeholder}
        min={field.minimum ?? undefined}
        max={field.maximum ?? undefined}
        value={typeof value === "number" ? value : ""}
        onChange={(next) => onChange(next === "" ? undefined : Number(next))}
      />
    );
  }
  if (field.type === "boolean") {
    return (
      <Switch
        label={label}
        aria-label={name}
        size="xs"
        checked={value === true}
        onChange={(event) => onChange(event.currentTarget.checked ? true : undefined)}
      />
    );
  }
  if (field.type === "model_id" && modelOptions.length > 0) {
    return (
      <Select
        label={label}
        aria-label={name}
        size="xs"
        clearable
        searchable
        placeholder={placeholder}
        data={modelOptions}
        value={value === undefined ? null : String(value)}
        onChange={(next) => onChange(next ?? undefined)}
      />
    );
  }
  return (
    <TextInput
      label={label}
      aria-label={name}
      size="xs"
      placeholder={placeholder}
      value={value === undefined ? "" : String(value)}
      onChange={(event) => onChange(event.currentTarget.value || undefined)}
    />
  );
}

/**
 * Edit the settings a slot always runs with.
 *
 * These are not effort settings -- they apply whatever effort was asked for --
 * so they sit beside the slot's provider or tool rather than inside the effort
 * block, mirroring where a model slot has always shown its model.
 */
export function ToolSettingsField({
  value,
  fields,
  onChange,
  modelOptions = [],
}: {
  value: Record<string, unknown>;
  fields: EffortFieldSpec[];
  onChange: (value: Record<string, unknown>) => void;
  modelOptions?: string[];
}) {
  // The model comes first: it is what the slot runs, and the effort only
  // qualifies it. Descriptor order suits the effort block, where `effort` is
  // the subject, but reads backwards here.
  const ordered = [...fields].sort(
    (left, right) => Number(right.type === "model_id") - Number(left.type === "model_id"),
  );
  return (
    <Group gap="xs" align="flex-end" grow>
      {ordered.map((field) => (
        <FieldControl
          key={field.key}
          field={field}
          level="always"
          modelOptions={modelOptions}
          value={readPath(value, field.key)}
          onChange={(next) => onChange(writePath(value, field.key, next))}
        />
      ))}
    </Group>
  );
}

type EffortSettingsFieldProps = {
  /** This scope's own mapping. Empty means "inherit". */
  value: EffortOverlay;
  /** What applies while `value` is empty. */
  inherited: EffortOverlay;
  /** What this provider accepts. Empty falls back to raw JSON editing. */
  fields: EffortFieldSpec[];
  onChange: (value: EffortOverlay) => void;
  onValidityChange?: (valid: boolean) => void;
  /** Model ids to offer for `model_id` fields, when the provider knows them. */
  modelOptions?: string[];
  /** False when the tool has nowhere to apply these settings. */
  supported?: boolean;
};

/**
 * Edit how each effort level changes the settings a slot always runs with.
 *
 * Three layers, in the order most users meet them:
 *
 * 1. While the scope states nothing of its own, the inherited mapping is shown
 *    read-only. Most workspaces never need to go further than reading it.
 * 2. Customizing reveals one typed control per setting the provider declares.
 *    Which settings exist comes from the backend, so nothing here knows what
 *    any provider's keys mean.
 * 3. A provider that declares no settings, or a key outside the declared set,
 *    is still reachable through the raw JSON editor.
 */
export function EffortSettingsField({
  value,
  inherited,
  fields,
  onChange,
  onValidityChange,
  modelOptions = [],
  supported = true,
}: EffortSettingsFieldProps) {
  const { t } = useTranslation();
  const isInherited = Object.keys(value).length === 0;
  const [customizing, setCustomizing] = useState(!isInherited);
  const [showJson, setShowJson] = useState(false);
  const effective = isInherited ? inherited : value;

  const updateLevel = (level: Level, settings: Record<string, unknown>) => {
    // Editing an inherited mapping starts from what was on screen, so the first
    // keystroke does not silently discard the rest of the inherited settings.
    const base = isInherited ? inherited : value;
    const next: EffortOverlay = { ...base };
    if (Object.keys(settings).length === 0) delete next[level];
    else next[level] = settings;
    onChange(next);
  };

  // Offering an editor here would collect settings the tool provably drops,
  // which is the same dead configuration an unmappable level would be.
  if (!supported) {
    return (
      <Box>
        <Text fw={500} size="sm">
          {t("setup.intelligence.effort.title")}
        </Text>
        <Text size="xs" c="dimmed">
          {t("setup.intelligence.effort.unsupported")}
        </Text>
      </Box>
    );
  }

  if (!customizing) {
    return (
      <Box>
        <Group gap="xs" mb={4}>
          <Text fw={500} size="sm">
            {t("setup.intelligence.effort.title")}
          </Text>
          {isInherited ? (
            <Badge size="xs" variant="light">
              {t("setup.intelligence.effort.inherited")}
            </Badge>
          ) : null}
        </Group>
        <Stack gap={2}>
          {EDITABLE_LEVELS.map((level) => (
            <Text key={level} size="xs" c="dimmed">
              {t(`setup.intelligence.effort.level.${level}`)}:{" "}
              {summarize(effective[level] ?? {}, fields) ||
                t("setup.intelligence.effort.noIntervention")}
            </Text>
          ))}
        </Stack>
        <Button size="compact-xs" variant="subtle" mt={6} onClick={() => setCustomizing(true)}>
          {t("setup.intelligence.effort.customize")}
        </Button>
      </Box>
    );
  }

  return (
    <Box>
      <Text fw={500} size="sm" mb={4}>
        {t("setup.intelligence.effort.title")}
      </Text>
      {fields.length > 0 && !showJson ? (
        // One bounded group per level. A bare label above a row of inputs put
        // more space between a level and its own fields than between that level
        // and the next, so the groups could not be told apart.
        <Stack gap="sm">
          {EDITABLE_LEVELS.map((level) => (
            <Fieldset
              key={level}
              legend={t(`setup.intelligence.effort.level.${level}`)}
              radius="sm"
              p="xs"
            >
              <Text size="xs" c="dimmed" mb={6}>
                {t("setup.intelligence.effort.levelHint")}
              </Text>
              <Group gap="xs" align="flex-end" wrap="wrap">
                {fields.map((field) => (
                  <FieldControl
                    key={field.key}
                    field={field}
                    level={level}
                    modelOptions={modelOptions}
                    value={readPath(effective[level] ?? {}, field.key)}
                    onChange={(next) =>
                      updateLevel(level, writePath(effective[level] ?? {}, field.key, next))
                    }
                  />
                ))}
              </Group>
            </Fieldset>
          ))}
        </Stack>
      ) : (
        <JsonObjectField
          label={t("setup.intelligence.effortJson")}
          description={t("setup.intelligence.effortJsonDescription")}
          errorText={t("setup.intelligence.effortJsonError")}
          value={effective}
          onChange={(next) => onChange(next as EffortOverlay)}
          onValidityChange={onValidityChange}
          size="xs"
        />
      )}
      {fields.length > 0 ? (
        <Button size="compact-xs" variant="subtle" mt={6} onClick={() => setShowJson(!showJson)}>
          {showJson
            ? t("setup.intelligence.effort.showFields")
            : t("setup.intelligence.effort.showJson")}
        </Button>
      ) : null}
    </Box>
  );
}
