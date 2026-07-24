import type {
  CommandArgumentOption,
  CommandFileBlockingCode,
  CommandFileDetail,
  CommandInputs,
} from "../api/client";
import type { CommandRunRecord } from "../App";

export const COMMAND_EDITOR_STATE_KEY = "guildbotics.commands.editorState";

// Fraction (0..1) of the editor/verify split given to the editor. Stored
// globally (a layout preference, not workspace data) and clamped so neither
// pane can collapse.
export const COMMAND_EDITOR_RATIO_KEY = "guildbotics.commands.editorRatio";
export const DEFAULT_EDITOR_RATIO = 0.6;
export const MIN_EDITOR_RATIO = 0.15;
export const MAX_EDITOR_RATIO = 0.85;

export function clampEditorRatio(ratio: number): number {
  if (!Number.isFinite(ratio)) {
    return DEFAULT_EDITOR_RATIO;
  }
  return Math.min(MAX_EDITOR_RATIO, Math.max(MIN_EDITOR_RATIO, ratio));
}

export function loadEditorRatio(): number {
  try {
    const raw = window.localStorage.getItem(COMMAND_EDITOR_RATIO_KEY);
    if (!raw) {
      return DEFAULT_EDITOR_RATIO;
    }
    return clampEditorRatio(Number.parseFloat(raw));
  } catch {
    return DEFAULT_EDITOR_RATIO;
  }
}

export function saveEditorRatio(ratio: number): void {
  try {
    window.localStorage.setItem(COMMAND_EDITOR_RATIO_KEY, String(clampEditorRatio(ratio)));
  } catch {
    // Ignore persistence failures (e.g. storage disabled or full).
  }
}

// Per-workspace UI state persisted between sessions. Draft source content,
// revision and validation errors are intentionally excluded: the disk file is
// always the source of truth and is re-fetched on load.
export type CommandEditorPersisted = {
  selectedFileId: string | null;
  person: string | null;
  argValues: Record<string, string>;
  extraArgs: string;
  message: string;
  cwd: string;
  showAdvanced: boolean;
  history: CommandRunRecord[];
  activeTraceId: string | null;
  activeTab: string | null;
};

export const EMPTY_EDITOR_STATE: CommandEditorPersisted = {
  selectedFileId: null,
  person: null,
  argValues: {},
  extraArgs: "",
  message: "",
  cwd: "",
  showAdvanced: false,
  history: [],
  activeTraceId: null,
  activeTab: "events",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validateRunRecord(value: unknown): value is CommandRunRecord {
  if (!isRecord(value)) return false;
  return (
    typeof value.traceId === "string" &&
    typeof value.person === "string" &&
    typeof value.command === "string" &&
    typeof value.startedAt === "string" &&
    (value.status === "running" || value.status === "success" || value.status === "failed") &&
    (value.output === undefined || typeof value.output === "string") &&
    (value.error === undefined || typeof value.error === "string")
  );
}

function stringRecord(value: unknown): Record<string, string> {
  if (!isRecord(value)) {
    return {};
  }
  const result: Record<string, string> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === "string") {
      result[key] = entry;
    }
  }
  return result;
}

function storageKey(storageDir: string): string {
  return `${COMMAND_EDITOR_STATE_KEY}:${storageDir}`;
}

export function loadEditorState(storageDir?: string): CommandEditorPersisted {
  if (!storageDir) {
    return { ...EMPTY_EDITOR_STATE };
  }
  try {
    const raw = window.localStorage.getItem(storageKey(storageDir));
    if (!raw) {
      return { ...EMPTY_EDITOR_STATE };
    }
    const parsed = JSON.parse(raw) as Partial<CommandEditorPersisted>;
    return {
      selectedFileId: typeof parsed.selectedFileId === "string" ? parsed.selectedFileId : null,
      person: typeof parsed.person === "string" ? parsed.person : null,
      argValues: stringRecord(parsed.argValues),
      extraArgs: typeof parsed.extraArgs === "string" ? parsed.extraArgs : "",
      message: typeof parsed.message === "string" ? parsed.message : "",
      cwd: typeof parsed.cwd === "string" ? parsed.cwd : "",
      showAdvanced: parsed.showAdvanced === true,
      history: Array.isArray(parsed.history)
        ? (parsed.history.filter(validateRunRecord) as CommandRunRecord[]).slice(0, 50)
        : [],
      activeTraceId: typeof parsed.activeTraceId === "string" ? parsed.activeTraceId : null,
      activeTab: typeof parsed.activeTab === "string" ? parsed.activeTab : "events",
    };
  } catch {
    return { ...EMPTY_EDITOR_STATE };
  }
}

export function saveEditorState(value: CommandEditorPersisted, storageDir?: string): void {
  if (!storageDir) {
    return;
  }
  try {
    window.localStorage.setItem(storageKey(storageDir), JSON.stringify(value));
  } catch {
    // Ignore persistence failures (e.g. storage disabled or full).
  }
}

export type SaveStatus = "clean" | "dirty" | "saving" | "error" | "conflict";

export function deriveSaveStatus(
  draftContent: string,
  savedContent: string,
  pending: boolean,
  conflict: boolean,
): SaveStatus {
  if (pending) return "saving";
  if (conflict) return "conflict";
  if (draftContent !== savedContent) return "dirty";
  return "clean";
}

// Build the argument list for a run from the command file's declared/discovered
// arguments plus free-form extra args, mirroring the catalog run behavior.
export function buildFileRunArgs(
  file: Pick<CommandFileDetail, "arguments" | "inputs"> | null,
  values: Record<string, string>,
  extraArgs: string,
): string[] {
  const args: string[] = [];
  if (file && file.inputs.defined_args !== "hidden") {
    for (const argument of file.arguments) {
      const value = values[argument.name]?.trim();
      if (!value) continue;
      args.push(argument.kind === "positional" ? value : `${argument.name}=${value}`);
    }
  }
  if (!file || file.inputs.extra_args === "optional") {
    args.push(...splitCommandLine(extraArgs));
  }
  return args;
}

export function splitCommandLine(value: string): string[] {
  const args: string[] = [];
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
  for (const match of value.matchAll(pattern)) {
    args.push(match[1] ?? match[2] ?? match[3] ?? "");
  }
  return args.filter(Boolean);
}

export function hasMissingRequiredArgument(
  argumentsList: CommandArgumentOption[],
  inputs: CommandInputs,
  values: Record<string, string>,
): boolean {
  if (inputs.defined_args === "hidden") {
    return false;
  }
  return argumentsList.some((argument) => argument.required && !values[argument.name]?.trim());
}

// Map a backend blocking error code (and its context) to an i18n key. The
// `command_file_shadowed` code chooses a member/template/workspace-specific
// message from `shadow_source`; everything else maps directly.
export function blockingMessageKey(
  code: CommandFileBlockingCode | string,
  context: Record<string, string>,
): string {
  if (code === "command_file_shadowed") {
    const source = context.shadow_source;
    if (source === "member" || source === "template" || source === "workspace") {
      return `commands.shadow.${source}`;
    }
    return "commands.shadow.generic";
  }
  return `commands.errors.${code}`;
}
