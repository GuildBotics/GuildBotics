// Shared hotkey helpers for the screens that assign combinations.

import { useCallback } from "react";

import { ApiRequestError } from "../api/client";
import { resumeHotkeys, suspendHotkeys } from "./hotkeyRuntime";

const KNOWN_CODES = ["hotkey_invalid", "hotkey_needs_modifier", "hotkey_conflict"];

/** Map a rejected save to the message explaining why the combination was refused. */
export function hotkeyErrorMessageKey(cause: unknown): string {
  const code = cause instanceof ApiRequestError ? cause.code : "";
  return KNOWN_CODES.includes(code) ? `hotkey.errors.${code}` : "hotkey.saveErrorTitle";
}

/**
 * Release the registered combinations while a recorder is capturing.
 *
 * A registered accelerator is consumed by the OS before any window sees it, so
 * without this the combination already in use could never be re-recorded.
 */
export function useHotkeyRecordingGuard(): (recording: boolean) => void {
  return useCallback((recording: boolean) => {
    void (recording ? suspendHotkeys() : resumeHotkeys());
  }, []);
}
