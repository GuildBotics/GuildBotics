import { Alert, Badge, Button, Group, Stack, Tabs, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";

import type { CommandAuthoringChange } from "../api/client";
import { CommandSourceDiff } from "./CommandSourceDiff";
import { CommandSourcePreview } from "./CommandSourcePreview";

export type CommandAuthoringCurrentSource = {
  relativePath: string;
  path: string;
  content: string;
};

export type CommandAuthoringWorkspaceProps = {
  changes: CommandAuthoringChange[];
  commandsRoot: string;
  current?: CommandAuthoringCurrentSource;
  pending: boolean;
  error: unknown;
  onApply: () => void;
  onDiscard: () => void;
};

function errorMessage(error: unknown): string | null {
  return error instanceof Error ? error.message : error ? String(error) : null;
}

/** Read-only, file-oriented review surface kept outside the chat transcript. */
export function CommandAuthoringWorkspace({
  changes,
  commandsRoot,
  current,
  pending,
  error,
  onApply,
  onDiscard,
}: CommandAuthoringWorkspaceProps) {
  const { t } = useTranslation();
  const initialTab = changes.length ? "proposal-0" : "current";
  const message = errorMessage(error);

  return (
    <section
      aria-label={t("commands.authoringProposal.reviewRegion")}
      className="command-authoring-workspace"
    >
      <Tabs className="command-authoring-tabs" defaultValue={initialTab} keepMounted={false}>
        <Tabs.List className="command-authoring-tabs-list" grow={false}>
          {current ? (
            <Tabs.Tab value="current">
              <Group component="span" gap={6} wrap="nowrap">
                <Badge color="gray" size="xs" variant="light">
                  {t("commands.authoringProposal.current")}
                </Badge>
                <Text component="span" size="sm" title={current.relativePath} truncate="end">
                  {current.relativePath}
                </Text>
              </Group>
            </Tabs.Tab>
          ) : null}
          {changes.map((change, index) => (
            <Tabs.Tab
              key={`${change.operation}:${change.relative_path}`}
              value={`proposal-${index}`}
            >
              <Group component="span" gap={6} wrap="nowrap">
                <Badge color={change.operation === "create" ? "blue" : "orange"} size="xs">
                  {t(`commands.authoringProposal.operations.${change.operation}`)}
                </Badge>
                <Text component="span" size="sm" title={change.relative_path} truncate="end">
                  {change.relative_path}
                </Text>
              </Group>
            </Tabs.Tab>
          ))}
        </Tabs.List>

        {current ? (
          <Tabs.Panel className="command-authoring-tabs-panel" value="current">
            <CommandSourcePreview path={current.path} source={current.content} />
          </Tabs.Panel>
        ) : null}
        {changes.map((change, index) => (
          <Tabs.Panel
            className="command-authoring-tabs-panel"
            key={`${change.operation}:${change.relative_path}`}
            value={`proposal-${index}`}
          >
            {change.operation === "update" && current ? (
              <CommandSourceDiff
                before={current.content}
                after={change.content}
                path={`${commandsRoot}/${change.relative_path}`}
              />
            ) : (
              <CommandSourcePreview
                path={`${commandsRoot}/${change.relative_path}`}
                source={change.content}
              />
            )}
          </Tabs.Panel>
        ))}
      </Tabs>

      <Stack className="command-authoring-actions" gap="xs">
        {message ? (
          <Alert color="warning" title={t("commands.authoringProposal.applyErrorTitle")}>
            {message}
          </Alert>
        ) : null}
        <Text className="command-authoring-help" c="dimmed" size="sm">
          {t("commands.authoringProposal.applyHelp", { count: changes.length })}
        </Text>
        <Group className="command-authoring-buttons" gap="xs" justify="flex-end">
          <Button variant="subtle" disabled={pending} onClick={onDiscard}>
            {t("commands.authoringProposal.discard")}
          </Button>
          <Button loading={pending} onClick={onApply}>
            {t("commands.authoringProposal.apply")}
          </Button>
        </Group>
      </Stack>
    </section>
  );
}
