import { Alert, Button, Group, Stack, Switch, Text, TextInput } from "@mantine/core";
import { Play } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { CommandFileDetail, CommandFileExecutionStatus, TraceRecord } from "../api/client";
import { CommandRunDetails, type CommandRunRecord } from "../App";
import { MemberSelector } from "../MemberSelector";
import { CommandInput } from "./CommandInput";
import { blockingMessageKey, hasMissingRequiredArgument } from "./commandEditorState";

type ActiveMember = { person_id: string; name: string };

export type CommandRunPanelProps = {
  file: CommandFileDetail | null;
  members: ActiveMember[];
  /** Explicitly selected member; null runs as the team default. */
  person: string | null;
  /** Member the backend runs as when none is selected. */
  defaultPerson: ActiveMember | null;
  onPersonChange: (person: string | null) => void;
  argValues: Record<string, string>;
  onArgValueChange: (name: string, value: string) => void;
  extraArgs: string;
  onExtraArgsChange: (value: string) => void;
  message: string;
  onMessageChange: (value: string) => void;
  cwd: string;
  onCwdChange: (value: string) => void;
  workspaceCwd: string;
  showAdvanced: boolean;
  onToggleAdvanced: (value: boolean) => void;
  executionStatus: CommandFileExecutionStatus | null;
  runBusy: boolean;
  onSaveAndRun: () => void;
  selectedRecord: CommandRunRecord | null;
  traceRecords: TraceRecord[];
  traceLoading: boolean;
  transcriptAvailable?: boolean;
  activeTab: string | null;
  onTabChange: (value: string | null) => void;
};

/**
 * Run console for the command being edited.
 *
 * A toolbar row carries the runner and the run action, then the inputs and the
 * result sit side by side — the same toolbar-over-master/detail shape the
 * diagnostics execution card uses, so neither column loses height to the other.
 */
export function CommandRunPanel(props: CommandRunPanelProps) {
  const { t } = useTranslation();
  const { file, members, executionStatus } = props;

  const inputs = file?.inputs;
  const showDefinedArgs = inputs?.defined_args === "auto" && Boolean(file?.arguments.length);
  const showExtraArgs = inputs?.extra_args === "optional";
  const showMessage = inputs?.message !== "hidden";
  const messageRequired = inputs?.message === "required";
  const missingRequiredArgument =
    file != null && hasMissingRequiredArgument(file.arguments, file.inputs, props.argValues);

  const blockingCode = executionStatus?.blocking_code ?? null;
  const unsatisfied = (executionStatus?.requirements ?? []).filter((req) => !req.satisfied);
  const noMembers = members.length === 0;
  const runner = members.find((member) => member.person_id === props.person) ?? props.defaultPerson;
  const runDisabled =
    !file ||
    noMembers ||
    !(props.person || props.defaultPerson) ||
    missingRequiredArgument ||
    (messageRequired && !props.message.trim()) ||
    blockingCode != null;

  const showUnsatisfied = unsatisfied.length > 0 && blockingCode !== "command_requirement_missing";

  return (
    <section className="command-run-panel" aria-label={t("commands.verifyHeading")}>
      <Group className="command-run-bar" gap="xs" justify="space-between" wrap="wrap">
        <Group gap="xs" wrap="nowrap">
          <MemberSelector
            ariaLabel={t("commands.runner", { member: runner?.name ?? "" })}
            member={runner}
            members={members}
            onChange={props.onPersonChange}
          />
          <Text fw={600} size="sm">
            {t("commands.verifyHeading")}
          </Text>
        </Group>
        <Button
          leftSection={<Play size={16} />}
          loading={props.runBusy}
          disabled={runDisabled}
          onClick={props.onSaveAndRun}
        >
          {t("commands.saveAndRun")}
        </Button>
      </Group>

      <div className="command-run-columns">
        <Stack className="command-run-settings" gap="sm">
          {showDefinedArgs ? (
            <div className="command-args-grid">
              {file?.arguments.map((argument) => (
                <TextInput
                  key={`${argument.kind}-${argument.name}`}
                  label={argument.name}
                  required={argument.required}
                  placeholder={argument.default || argument.kind}
                  value={props.argValues[argument.name] ?? ""}
                  onChange={(event) =>
                    props.onArgValueChange(argument.name, event.currentTarget.value)
                  }
                />
              ))}
            </div>
          ) : null}

          {showExtraArgs ? (
            <TextInput
              label={t("commands.extraArgs")}
              placeholder={t("commands.extraArgsPlaceholder")}
              value={props.extraArgs}
              onChange={(event) => props.onExtraArgsChange(event.currentTarget.value)}
            />
          ) : null}

          {showMessage ? (
            <CommandInput
              required={messageRequired}
              label={t("commands.message")}
              description={t("commands.messageDescription")}
              rows={3}
              resize="vertical"
              value={props.message}
              onChange={props.onMessageChange}
            />
          ) : null}

          {/* The toggle stays directly above what it reveals. */}
          <Switch
            checked={props.showAdvanced}
            label={t("commands.advanced")}
            onChange={(event) => props.onToggleAdvanced(event.currentTarget.checked)}
          />
          {props.showAdvanced ? (
            <TextInput
              label={t("commands.cwd")}
              description={t("commands.cwdDescription", { cwd: props.workspaceCwd })}
              value={props.cwd}
              onChange={(event) => props.onCwdChange(event.currentTarget.value)}
            />
          ) : null}

          {noMembers ? (
            <Alert color="warning" title={t("commands.noMembersTitle")}>
              {t("commands.noMembersBody")}
            </Alert>
          ) : null}

          {blockingCode ? (
            <Alert color="warning" title={t("commands.runBlockedTitle")}>
              {t(blockingMessageKey(blockingCode, executionStatus?.blocking_context ?? {}))}
            </Alert>
          ) : null}

          {showUnsatisfied ? (
            <Text c="dimmed" size="sm">
              {unsatisfied.map((req) => t(`commands.requirements.${req.kind}`)).join(", ")}
            </Text>
          ) : null}
        </Stack>

        <div className="command-run-output">
          {props.selectedRecord ? (
            <CommandRunDetails
              key={props.selectedRecord.traceId}
              record={props.selectedRecord}
              records={props.traceRecords}
              loading={props.traceLoading}
              transcriptAvailable={props.transcriptAvailable}
              activeTab={props.activeTab}
              onTabChange={props.onTabChange}
            />
          ) : (
            <div className="empty-row">{t("commands.noRunsYet")}</div>
          )}
        </div>
      </div>
    </section>
  );
}
