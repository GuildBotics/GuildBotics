// Pure state rules for the quick-run window.
//
// Kept apart from the component so the "can this run without asking?" decision
// — the one thing that separates a dedicated hotkey from the generic window —
// is directly testable.

import type {
  CommandOption,
  RuntimeEvent,
  TeamSummary,
  TracePresentation,
  TraceRecord,
} from "../api/client";
import { hasMissingRequiredArgument } from "../commands/commandEditorState";

type TeamMember = TeamSummary["members"][number];

export const LAST_COMMAND_KEY = "guildbotics.quickRun.lastCommand";
export const WATCH_CLIPBOARD_KEY = "guildbotics.quickRun.watchClipboard";
export const LAST_PERSON_KEY = "guildbotics.quickRun.lastPerson";
/** Interval between clipboard checks while watching. */
export const CLIPBOARD_POLL_MS = 300;
/**
 * How long typing must stay quiet before auto-run treats the input as final.
 *
 * Long enough not to fire between words; anyone who wants to think longer
 * turns auto-run off.
 */
export const IDLE_RUN_MS = 3000;

export type QuickRunTrigger = {
  /** Command bound to the pressed hotkey, or null for the generic window. */
  command: string | null;
  /** Clipboard text captured when the hotkey fired. */
  text: string;
  /** Clipboard image resource captured when the hotkey fired. */
  image?: number | null;
};

/** Requirements (GitHub, Slack, LLM, ...) the command needs but does not have. */
export function unmetRequirements(option: CommandOption | undefined): string[] {
  return (option?.requirements ?? [])
    .filter((requirement) => !requirement.satisfied)
    .map((requirement) => requirement.kind);
}

/**
 * Whether the command can run as it stands.
 *
 * Covers both the inputs the caller has to supply and the integrations the
 * command depends on: a hotkey must not fire a command whose GitHub or LLM
 * configuration is missing. When this does not hold, the window opens and
 * waits instead of running a request that cannot succeed.
 */
export function canRunUnattended(
  option: CommandOption | undefined,
  message: string,
  argValues: Record<string, string>,
): boolean {
  if (unmetRequirements(option).length > 0) {
    return false;
  }
  if (!option) {
    return false;
  }
  if (option.inputs.message === "required" && !message.trim()) {
    return false;
  }
  return !hasMissingRequiredArgument(option.arguments, option.inputs, argValues);
}

/** Command to preselect in the generic window: the last selected one, if it still exists. */
export function initialCommand(options: CommandOption[], remembered: string | null): string | null {
  if (remembered && options.some((option) => option.command === remembered)) {
    return remembered;
  }
  return options[0]?.command ?? null;
}

export function loadLastCommand(): string | null {
  try {
    return window.localStorage.getItem(LAST_COMMAND_KEY);
  } catch {
    return null;
  }
}

export function saveLastCommand(command: string): void {
  try {
    window.localStorage.setItem(LAST_COMMAND_KEY, command);
  } catch {
    // Ignore persistence failures (e.g. storage disabled or full).
  }
}

export function loadWatchClipboard(): boolean {
  try {
    return window.localStorage.getItem(WATCH_CLIPBOARD_KEY) === "true";
  } catch {
    return false;
  }
}

export function saveWatchClipboard(enabled: boolean): void {
  try {
    window.localStorage.setItem(WATCH_CLIPBOARD_KEY, String(enabled));
  } catch {
    // Ignore persistence failures (e.g. storage disabled or full).
  }
}

export function loadLastPerson(): string | null {
  try {
    return window.localStorage.getItem(LAST_PERSON_KEY);
  } catch {
    return null;
  }
}

export function saveLastPerson(personId: string): void {
  try {
    window.localStorage.setItem(LAST_PERSON_KEY, personId);
  } catch {
    // Ignore persistence failures (e.g. storage disabled or full).
  }
}

/** The run this window is waiting on: what it asked for, and for whom. */
export type PendingRun = {
  command: string;
  /** Resolved member, or null when the window could not name one. */
  person: string | null;
};

/** Trace source of runs the desktop asks for, as opposed to the ones the service schedules. */
const MANUAL_TRACE_SOURCE = "manual";

/**
 * Trace id of a run announcement that belongs to this window's own run.
 *
 * The service names a run's trace only when it starts — the run request itself
 * does not answer until the command is over, which is the whole period the
 * status line exists for. The scheduler can be running the same command for the
 * same member at the same time, so an announcement counts only when it is a
 * manual run and matches what this window just asked for.
 */
export function pendingRunTraceId(event: RuntimeEvent, pending: PendingRun | null): string | null {
  if (!pending || event.type !== "command.started" || !event.trace_id) {
    return null;
  }
  if (event.source !== MANUAL_TRACE_SOURCE) {
    return null;
  }
  if (event.payload.command !== pending.command) {
    return null;
  }
  if (pending.person !== null && event.payload.person !== pending.person) {
    return null;
  }
  return event.trace_id;
}

/** Newest record of a trace — what a one-line status shows. Records arrive oldest first. */
export function latestPresentation(records: TraceRecord[]): TracePresentation | null {
  return records.length > 0 ? records[records.length - 1].presentation : null;
}

/**
 * Member a run is attributed to: the chosen one while it is still active, and
 * the team default otherwise.
 *
 * Command definitions can differ per member, so this also decides which
 * catalogue the window shows.
 */
export function resolveRunner(
  team: TeamSummary | undefined,
  person: string | null,
): TeamMember | null {
  const active = (team?.members ?? []).filter((member) => member.is_active);
  return (
    active.find((member) => member.person_id === person) ??
    active.find((member) => member.person_id === team?.default_person_id) ??
    null
  );
}
