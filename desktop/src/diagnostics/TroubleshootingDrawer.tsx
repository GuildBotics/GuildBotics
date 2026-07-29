import { Alert, Badge, Button, Drawer, Group, Switch, Text } from "@mantine/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getSchedulerStatus,
  getTeam,
  getTraceDetail,
  troubleshoot,
  type TroubleshootingFocus,
} from "../api/client";
import { AssistantChatPanel, type AssistantReference } from "../assistant/AssistantChatPanel";
import { useAssistantConversation } from "../assistant/useAssistantConversation";
import { MemberSelector } from "../MemberSelector";
import { tracePresentationMessage } from "../tracePresentation";

const PRIVACY_STORAGE_KEY = "guildbotics.troubleshootingPrivacyAck";
const PROGRESS_POLL_INTERVAL = 2000;
/** Command label prefix AppRuntime gives every troubleshooting turn. */
const TURN_COMMAND_PREFIX = "troubleshoot:";

export type TroubleshootingDrawerProps = {
  opened: boolean;
  onClose: () => void;
  /** What the user is currently looking at, re-read on every submit. */
  focus: TroubleshootingFocus;
  /** Human label for the focused target, resolved by the diagnostics screen. */
  focusLabel: string;
  /** Label for a trace the assistant cites, resolved by the diagnostics screen. */
  traceLabel: (traceId: string) => string;
};

export function TroubleshootingDrawer({
  opened,
  onClose,
  focus,
  focusLabel,
  traceLabel,
}: TroubleshootingDrawerProps) {
  const { t } = useTranslation();
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
  const [followTarget, setFollowTarget] = useState(true);
  const [personId, setPersonId] = useState<string | null>(null);
  const [pinnedFocus, setPinnedFocus] = useState<TroubleshootingFocus>(focus);
  const [pinnedLabel, setPinnedLabel] = useState(focusLabel);
  const [privacyAcknowledged, setPrivacyAcknowledged] = useState(
    () => globalThis.localStorage?.getItem(PRIVACY_STORAGE_KEY) === "1",
  );

  const activeFocus = followTarget ? focus : pinnedFocus;
  const activeLabel = followTarget ? focusLabel : pinnedLabel;

  const activeMembers = (team.data?.members ?? []).filter((member) => member.is_active);
  const person =
    activeMembers.find((member) => member.person_id === personId) ??
    activeMembers.find((member) => member.person_id === team.data?.default_person_id) ??
    null;
  const effectivePerson = person?.person_id ?? null;

  // The conversation follows its member and target, so switching either — or
  // asking about a different record of the same execution — starts a fresh
  // investigation rather than continuing the old one.
  const targetKey = [
    effectivePerson ?? "",
    activeFocus.view,
    activeFocus.trace_id ?? "",
    activeFocus.span_id ?? "",
    activeFocus.record_timestamp ?? "",
  ].join(":");
  const conversation = useAssistantConversation(targetKey);

  const mutation = useMutation({
    mutationFn: (request: {
      conversationId: string;
      targetKey: string;
      message: string;
      person: string;
      focus: TroubleshootingFocus;
    }) =>
      troubleshoot({
        conversation_id: request.conversationId,
        message: request.message,
        person: request.person,
        focus: request.focus,
      }),
    onMutate: (request) => {
      conversation.appendUser(request.conversationId, request.targetKey, request.message);
    },
    onSuccess: (response, request) => {
      conversation.appendAssistant(request.conversationId, request.targetKey, {
        content: response.message,
        traceId: response.trace_id,
        references: response.trace_ids.map(
          (traceId): AssistantReference => ({
            traceId,
            label: traceLabel(traceId),
            to: `/diagnostics?tab=executions&trace_id=${encodeURIComponent(traceId)}&assist=1`,
          }),
        ),
      });
    },
  });

  const progress = useTurnProgress(mutation.isPending);

  const acknowledgePrivacy = () => {
    globalThis.localStorage?.setItem(PRIVACY_STORAGE_KEY, "1");
    setPrivacyAcknowledged(true);
  };

  const header = (
    <>
      <Group gap="xs" justify="space-between">
        <Badge variant="light" tt="none">
          {activeLabel}
        </Badge>
        <Switch
          checked={followTarget}
          label={t("diagnostics.troubleshooting.followTarget")}
          size="xs"
          onChange={(event) => {
            const next = event.currentTarget.checked;
            if (!next) {
              setPinnedFocus(focus);
              setPinnedLabel(focusLabel);
            }
            setFollowTarget(next);
          }}
        />
      </Group>
      {effectivePerson ? null : (
        <Alert color="warning">{t("diagnostics.troubleshooting.noMember")}</Alert>
      )}
      {privacyAcknowledged ? null : (
        <Alert color="warning" title={t("diagnostics.troubleshooting.privacyTitle")}>
          <Text size="sm">{t("diagnostics.troubleshooting.privacyBody")}</Text>
          <Button mt="xs" size="compact-xs" variant="light" onClick={acknowledgePrivacy}>
            {t("diagnostics.troubleshooting.privacyDismiss")}
          </Button>
        </Alert>
      )}
    </>
  );

  return (
    <Drawer
      className="assistant-chat-drawer"
      opened={opened}
      onClose={onClose}
      position="right"
      size={520}
      title={
        <Group gap="xs" wrap="nowrap">
          <MemberSelector
            ariaLabel={t("diagnostics.troubleshooting.runner", { person: person?.name ?? "" })}
            member={person}
            members={activeMembers}
            onChange={setPersonId}
          />
          <Text fw={600}>{t("diagnostics.troubleshooting.title")}</Text>
        </Group>
      }
      // Non-modal: the executions list behind the drawer stays clickable so the
      // user can move between traces without losing the conversation.
      withOverlay={false}
      lockScroll={false}
      closeOnClickOutside={false}
      trapFocus={false}
    >
      <AssistantChatPanel
        namespace="diagnostics.troubleshooting"
        key={conversation.conversationId}
        messages={conversation.messages}
        pending={mutation.isPending}
        disabled={!effectivePerson}
        autoScrollOnAssistantResponse
        // A failure belongs to the turn that produced it: moving to another
        // execution must not leave the previous one's error on screen.
        error={mutation.variables?.targetKey === targetKey ? errorMessage(mutation.error) : null}
        header={header}
        progress={progress}
        onSubmit={(message) => {
          if (effectivePerson) {
            mutation.mutate({
              conversationId: conversation.conversationId,
              targetKey,
              message,
              person: effectivePerson,
              focus: activeFocus,
            });
          }
        }}
      />
    </Drawer>
  );
}

/**
 * Report what the running turn is doing.
 *
 * The turn records its own trace while it runs, so its progress is read back
 * from the diagnostics it is already writing rather than from a separate
 * streaming channel.
 */
function useTurnProgress(pending: boolean) {
  const { t } = useTranslation();
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!pending) {
      return;
    }
    const startedAt = Date.now();
    const update = () => setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    // Zero the clock on the next tick rather than in the effect body, so a new
    // turn never briefly shows the previous turn's elapsed time.
    const reset = setTimeout(update, 0);
    const timer = setInterval(update, 1000);
    return () => {
      clearTimeout(reset);
      clearInterval(timer);
    };
  }, [pending]);

  const status = useQuery({
    queryKey: ["scheduler-status"],
    queryFn: getSchedulerStatus,
    enabled: pending,
    refetchInterval: pending ? PROGRESS_POLL_INTERVAL : false,
  });
  const turnTraceId =
    status.data?.active_works?.find(
      (work) => work.source === "manual" && work.command.startsWith(TURN_COMMAND_PREFIX),
    )?.id ?? "";

  const turnDetail = useQuery({
    queryKey: ["diagnostics-trace", turnTraceId],
    queryFn: () => getTraceDetail(turnTraceId),
    enabled: pending && Boolean(turnTraceId),
    refetchInterval: pending ? PROGRESS_POLL_INTERVAL : false,
  });

  const lastStep = useMemo(() => {
    const records = turnDetail.data?.records ?? [];
    const last = records[records.length - 1];
    return last ? tracePresentationMessage(t, last.presentation) : "";
  }, [turnDetail.data, t]);

  if (!pending) {
    return null;
  }
  return (
    <>
      {lastStep ? (
        <Text c="dimmed" size="xs">
          {lastStep}
        </Text>
      ) : null}
      <Text c="dimmed" size="xs">
        {t("diagnostics.troubleshooting.elapsed", { seconds: elapsed })}
      </Text>
    </>
  );
}

function errorMessage(error: unknown): string | null {
  if (error instanceof Error) {
    return error.message;
  }
  return error ? String(error) : null;
}
