// Pure state rules for the quick-run window.
//
// Kept apart from the component so the "can this run without asking?" decision
// — the one thing that separates a dedicated hotkey from the generic window —
// is directly testable.

import type { CommandOption, TeamSummary } from "../api/client";
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
