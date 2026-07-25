import {
  ActionIcon,
  Alert,
  Avatar,
  Button,
  Checkbox,
  Group,
  Loader,
  Menu,
  Select,
  Text,
  TextInput,
  Textarea,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Play, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getCommandOptions,
  getTeam,
  memberAvatarUrl,
  runCommand,
  type CommandOption,
} from "../api/client";
import { buildFileRunArgs } from "../commands/commandEditorState";
import { clipboardWatchSupported, hideQuickWindow, pollClipboard } from "../hotkeys/hotkeyRuntime";
import {
  canRunUnattended,
  CLIPBOARD_POLL_MS,
  IDLE_RUN_MS,
  initialCommand,
  loadLastCommand,
  loadLastPerson,
  loadWatchClipboard,
  saveLastCommand,
  saveLastPerson,
  resolveRunner,
  saveWatchClipboard,
  unmetRequirements,
  type QuickRunTrigger,
} from "./quickRunState";

const TEAM_QUERY = { queryKey: ["quick-run-team"], queryFn: getTeam };

/**
 * Command catalogue for one member.
 *
 * Members can override commands, so the catalogue has to be fetched for the
 * member the run will be attributed to — otherwise the window could show one
 * command's arguments and run another member's implementation.
 */
function optionsQueryFor(person: string | null) {
  return {
    queryKey: ["quick-run-options", person],
    queryFn: () => getCommandOptions(person ?? undefined),
  };
}

type RunState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; output: string }
  | { status: "failed"; message: string };

export type QuickRunProps = {
  /**
   * Subscribes to hotkey activations and returns an unsubscribe function.
   * Injected so the window can be driven without the Tauri event bridge.
   */
  subscribe: (handler: (trigger: QuickRunTrigger) => void) => () => void;
};

export function QuickRun(props: QuickRunProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // Which member the run is attributed to. Remembered between windows, since
  // different commands tend to belong to different members.
  const team = useQuery(TEAM_QUERY);
  const [person, setPerson] = useState(loadLastPerson);
  // Same rule as the command edit screen: active members are offered, and the
  // backend owns which of them an omitted person resolves to.
  const activeMembers = (team.data?.members ?? []).filter((member) => member.is_active);
  const runner = resolveRunner(team.data, person);
  useEffect(() => {
    runnerIdRef.current = runner?.person_id ?? null;
  }, [runner]);

  const optionsQuery = useQuery(optionsQueryFor(runner?.person_id ?? null));
  const options = optionsQuery.data?.options ?? [];

  const [command, setCommand] = useState<string | null>(null);
  const [dedicated, setDedicated] = useState(false);
  const [message, setMessage] = useState("");
  const [argValues, setArgValues] = useState<Record<string, string>>({});
  const [run, setRun] = useState<RunState>({ status: "idle" });
  const [watching, setWatching] = useState(loadWatchClipboard);
  // Set from how the window was opened, then freely overridable: a dedicated
  // hotkey means "just run it", the generic window means "let me look first".
  const [autoRun, setAutoRun] = useState(false);
  // Watching is scoped to a visible window; the hidden webview keeps running.
  const [visible, setVisible] = useState(false);
  const [copied, setCopied] = useState(false);
  const messageRef = useRef<HTMLTextAreaElement>(null);
  const selected = options.find((option) => option.command === command);

  // Input text of the most recent run. Auto-run fires on input changes, so this
  // is what keeps a settled field from running the same request again.
  const lastRunText = useRef<string | null>(null);
  // Member the run is attributed to. A ref because `execute` is created once.
  const runnerIdRef = useRef<string | null>(null);
  // Set while dismissing, so the blur that comes with it does not run the text
  // the user just walked away from.
  const dismissing = useRef(false);

  const idleTimer = useRef<number | null>(null);
  const clearIdleRun = useCallback(() => {
    if (idleTimer.current !== null) {
      window.clearTimeout(idleTimer.current);
      idleTimer.current = null;
    }
  }, []);
  useEffect(() => clearIdleRun, [clearIdleRun]);

  const execute = useCallback(
    async (option: CommandOption, text: string, values: Record<string, string>) => {
      lastRunText.current = text;
      setRun({ status: "running" });
      try {
        const response = await runCommand({
          command: option.command,
          person: runnerIdRef.current ?? undefined,
          args: buildFileRunArgs(option, values, ""),
          message: option.inputs.message === "hidden" ? "" : text,
        });
        setRun({ status: "done", output: response.output });
      } catch (cause) {
        setRun({ status: "failed", message: String(cause) });
      }
    },
    [],
  );

  /**
   * Mark the window as going away.
   *
   * Pressing the close button blurs the input first, so the flag has to be set
   * on pointer-down; otherwise the blur runs the text the user just abandoned.
   */
  const beginDismiss = useCallback(() => {
    dismissing.current = true;
    clearIdleRun();
  }, [clearIdleRun]);

  const dismiss = useCallback(() => {
    beginDismiss();
    setVisible(false);
    void hideQuickWindow();
  }, [beginDismiss]);

  const subscribe = props.subscribe;
  useEffect(() => {
    return subscribe(async (trigger) => {
      // The hotkey can fire before the team and command list have loaded, so
      // wait for them rather than deciding against an empty list — and resolve
      // the member first, since the catalogue depends on it.
      const teamData = await queryClient.ensureQueryData(TEAM_QUERY);
      const activeRunner = resolveRunner(teamData, loadLastPerson());
      const { options: available } = await queryClient.ensureQueryData(
        optionsQueryFor(activeRunner?.person_id ?? null),
      );
      const next = trigger.command ?? initialCommand(available, loadLastCommand());
      const option = available.find((candidate) => candidate.command === next);
      if (!trigger.command && next) {
        // Remember the fallback too, so the picker keeps showing the same
        // command until the user chooses a different one.
        saveLastCommand(next);
      }
      const auto = trigger.command != null;
      dismissing.current = false;
      clearIdleRun();
      setCommand(next);
      setDedicated(auto);
      setAutoRun(auto);
      setMessage(trigger.text);
      setArgValues({});
      setRun({ status: "idle" });
      setVisible(true);

      // Auto-run still waits when something the command requires is missing.
      if (auto && option && canRunUnattended(option, trigger.text, {})) {
        void execute(option, trigger.text, {});
      } else {
        messageRef.current?.focus();
      }
    });
  }, [clearIdleRun, execute, queryClient, subscribe]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        dismiss();
      }
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && selected) {
        void execute(selected, message, argValues);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [argValues, dismiss, execute, message, selected]);

  const ready = canRunUnattended(selected, message, argValues);
  const missingRequirements = unmetRequirements(selected);
  const showMessage = selected?.inputs.message !== "hidden";
  const showArgs = selected?.inputs.defined_args === "auto" && selected.arguments.length > 0;

  const watchSupported = useQuery({
    queryKey: ["clipboard-watch-supported"],
    queryFn: clipboardWatchSupported,
  });
  const canWatch = watchSupported.data === true;

  // Read by callbacks that must see the current selection without being torn
  // down and rebuilt (and, for the poll, losing its baseline) on every keystroke.
  const latest = useRef({ selected, argValues, autoRun });
  useEffect(() => {
    latest.current = { selected, argValues, autoRun };
  }, [argValues, autoRun, selected]);

  /**
   * Run because the input changed — from a clipboard copy or from the user
   * finishing an edit.
   *
   * Only a genuinely different request runs: blur fires for plenty of reasons
   * that are not "I am done" (clicking Run, ticking a checkbox, tabbing away),
   * and re-running identical text on each of them would be both surprising and
   * expensive.
   */
  const runOnInputChange = useCallback(
    (text: string) => {
      const { selected: option, argValues: values, autoRun: auto } = latest.current;
      if (!auto || !option || text === lastRunText.current) {
        return;
      }
      if (canRunUnattended(option, text, values)) {
        void execute(option, text, values);
      }
    },
    [execute],
  );

  /**
   * Run once typing has been quiet for a while.
   *
   * Hand-typed input has no "I am done" signal, so a pause is the only thing
   * left to read it from. Each keystroke pushes the moment back.
   */
  const scheduleIdleRun = useCallback(
    (text: string) => {
      clearIdleRun();
      idleTimer.current = window.setTimeout(() => {
        idleTimer.current = null;
        if (!dismissing.current) {
          runOnInputChange(text);
        }
      }, IDLE_RUN_MS);
    },
    [clearIdleRun, runOnInputChange],
  );

  // Copying inside this window — the result of a run, say — is not new input.
  // The text is remembered rather than a "skip the next change" flag because
  // the copy event fires before the clipboard is actually updated, so anything
  // timing-based would race with the poll.
  const selfCopied = useRef<string | null>(null);
  useEffect(() => {
    const onCopy = () => {
      const text = window.getSelection()?.toString() ?? "";
      if (text) {
        selfCopied.current = text;
      }
    };
    document.addEventListener("copy", onCopy);
    return () => document.removeEventListener("copy", onCopy);
  }, []);

  useEffect(() => {
    if (!canWatch || !watching || !visible || !showMessage) {
      return;
    }
    // The counter from the activation read is unknown, so the first tick only
    // establishes a baseline and never overwrites what the hotkey carried in.
    let since: number | null = null;
    let stopped = false;
    const timer = window.setInterval(async () => {
      const poll = await pollClipboard(since ?? -1);
      if (stopped || !poll) {
        return;
      }
      const baseline = since === null;
      since = poll.change_count;
      if (baseline || poll.text === null) {
        return;
      }
      if (poll.text === selfCopied.current) {
        // Our own copy. The baseline has already advanced, so the next copy
        // from anywhere still registers.
        selfCopied.current = null;
        return;
      }
      setMessage(poll.text);
      runOnInputChange(poll.text);
    }, CLIPBOARD_POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [canWatch, runOnInputChange, showMessage, visible, watching]);

  /**
   * Copy the output, and record it as our own so clipboard watching ignores it.
   *
   * The `copy` listener above only sees user-driven copies, so a programmatic
   * write would otherwise look like fresh input and be pulled straight back
   * into the field.
   */
  const copyOutput = async (text: string) => {
    selfCopied.current = text;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      selfCopied.current = null;
    }
  };

  return (
    <div className="quick-run" data-tauri-drag-region>
      {/* Kept out of the header row: beside the command field, at the same
          height, a cross reads as "clear this field" rather than "close". */}
      <ActionIcon
        className="quick-run-close"
        variant="subtle"
        color="gray"
        size="sm"
        aria-label={t("quickRun.close")}
        onMouseDown={beginDismiss}
        onClick={dismiss}
      >
        <X size={16} />
      </ActionIcon>

      <Group gap="xs" wrap="nowrap" className="quick-run-header">
        <Menu position="bottom-start" withinPortal>
          <Menu.Target>
            <UnstyledButton
              className="quick-run-runner"
              aria-label={t("quickRun.runner", { member: runner?.name ?? "" })}
            >
              <Avatar
                src={runner ? memberAvatarUrl(runner.person_id) : undefined}
                size={28}
                radius="xl"
                alt={runner?.name ?? ""}
              >
                {runner?.name.slice(0, 1) ?? ""}
              </Avatar>
            </UnstyledButton>
          </Menu.Target>
          <Menu.Dropdown>
            {activeMembers.map((member) => (
              <Menu.Item
                key={member.person_id}
                leftSection={
                  <Avatar src={memberAvatarUrl(member.person_id)} size={20} radius="xl">
                    {member.name.slice(0, 1)}
                  </Avatar>
                }
                onClick={() => {
                  setPerson(member.person_id);
                  saveLastPerson(member.person_id);
                }}
              >
                {member.name}
              </Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>

        {dedicated ? (
          <Text fw={500} size="sm" className="quick-run-title">
            {selected?.label ?? command}
          </Text>
        ) : (
          <Select
            size="sm"
            className="quick-run-command"
            aria-label={t("quickRun.command")}
            placeholder={t("quickRun.commandPlaceholder")}
            searchable
            value={command}
            onChange={(value) => {
              setCommand(value);
              if (value) {
                saveLastCommand(value);
              }
            }}
            data={options.map((option) => ({ value: option.command, label: option.label }))}
          />
        )}
      </Group>

      {/* Nothing about a disabled run button explains itself, so name the
          integrations the command is still missing. */}
      {missingRequirements.length > 0 ? (
        <Text size="xs" c="dimmed">
          {t("quickRun.requirementsMissing", {
            requirements: missingRequirements
              .map((kind) => t(`commands.requirements.${kind}`))
              .join(", "),
          })}
        </Text>
      ) : null}

      {/* A hotkey can outlive the command it was bound to; say so rather than
          leaving a window that silently refuses to run. */}
      {command && !selected && !optionsQuery.isLoading ? (
        <Alert color="warning" title={t("quickRun.unknownCommandTitle")}>
          {t("quickRun.unknownCommandBody", { command })}
        </Alert>
      ) : null}

      {showArgs
        ? selected?.arguments.map((argument) => (
            <TextInput
              key={`${argument.kind}-${argument.name}`}
              size="sm"
              label={argument.name}
              required={argument.required}
              placeholder={argument.default || argument.kind}
              value={argValues[argument.name] ?? ""}
              onChange={(event) => {
                // Read before the updater runs: React clears currentTarget as
                // soon as the handler returns.
                const value = event.currentTarget.value;
                setArgValues((current) => ({ ...current, [argument.name]: value }));
              }}
            />
          ))
        : null}

      {showMessage ? (
        <Textarea
          ref={messageRef}
          size="sm"
          aria-label={t("quickRun.message")}
          placeholder={t("quickRun.messagePlaceholder")}
          required={selected?.inputs.message === "required"}
          rows={4}
          value={message}
          onChange={(event) => {
            const value = event.currentTarget.value;
            setMessage(value);
            scheduleIdleRun(value);
          }}
          onFocus={() => {
            // A press that never became a click must not keep the window
            // looking like it is on its way out.
            dismissing.current = false;
          }}
          onBlur={() => {
            clearIdleRun();
            if (!dismissing.current) {
              runOnInputChange(message);
            }
          }}
        />
      ) : null}

      <div className="quick-run-actions">
        <div className="quick-run-toggles">
          <Checkbox
            size="xs"
            label={t("quickRun.autoRun")}
            checked={autoRun}
            onChange={(event) => setAutoRun(event.currentTarget.checked)}
          />
          {/* Watching only feeds the input field, so it is offered only when the
              selected command actually takes one. */}
          {showMessage && canWatch ? (
            <Checkbox
              size="xs"
              label={t("quickRun.watchClipboard")}
              checked={watching}
              onChange={(event) => {
                const enabled = event.currentTarget.checked;
                setWatching(enabled);
                saveWatchClipboard(enabled);
              }}
            />
          ) : null}
        </div>
        <Button
          size="xs"
          leftSection={<Play size={14} />}
          loading={run.status === "running"}
          disabled={!ready}
          onClick={() => selected && void execute(selected, message, argValues)}
        >
          {t("quickRun.run")}
        </Button>
      </div>

      <div className="quick-run-result">
        {run.status === "running" ? (
          <div className="quick-run-pending">
            <Loader size="xs" />
            <Text size="xs" c="dimmed">
              {t("quickRun.running")}
            </Text>
          </div>
        ) : null}
        {run.status === "done" ? (
          <div className="quick-run-output-block">
            {run.output ? (
              <Tooltip label={t(copied ? "quickRun.copied" : "quickRun.copy")}>
                <ActionIcon
                  className="quick-run-copy"
                  variant="subtle"
                  color="gray"
                  size="sm"
                  aria-label={t("quickRun.copy")}
                  onClick={() => void copyOutput(run.output)}
                >
                  {copied ? <Check size={14} /> : <Copy size={14} />}
                </ActionIcon>
              </Tooltip>
            ) : null}
            <pre className="quick-run-output">{run.output || t("quickRun.noOutput")}</pre>
          </div>
        ) : null}
        {run.status === "failed" ? (
          <Alert color="danger" title={t("quickRun.failed")}>
            {run.message}
          </Alert>
        ) : null}
      </div>
    </div>
  );
}
