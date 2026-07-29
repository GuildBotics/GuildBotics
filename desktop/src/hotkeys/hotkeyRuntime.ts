// Bridge between the stored hotkey assignments and the OS registrations.
//
// The workspace config decides which combination runs which command; the Tauri
// host only registers what it is handed here. Every call is a no-op outside the
// desktop shell so the browser-based tests and dev server keep working.

import { Image } from "@tauri-apps/api/image";

import type { HotkeySettings } from "../api/client";

function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function invokeHost<T>(command: string, args?: Record<string, unknown>): Promise<T | null> {
  if (!isTauriRuntime()) {
    return null;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<T>(command, args);
  } catch {
    // The host command is unavailable (e.g. a harness that only stubs
    // __TAURI_INTERNALS__): behave as if there were nothing to register.
    return null;
  }
}

/**
 * Register the given assignments, replacing whatever was registered before.
 *
 * Returns the accelerators the OS refused — usually because another
 * application already holds them — so the caller can tell the user which
 * assignments are not actually live.
 */
export async function syncHotkeys(settings: HotkeySettings): Promise<string[]> {
  const result = await invokeHost<{ rejected: string[] }>("sync_hotkeys", { settings });
  return result?.rejected ?? [];
}

/**
 * Release every combination while a recorder is capturing.
 *
 * Without this the accelerator currently in use is swallowed by the OS and
 * never reaches the field, making it impossible to re-record.
 */
export async function suspendHotkeys(): Promise<void> {
  await invokeHost("suspend_hotkeys");
}

export async function resumeHotkeys(): Promise<void> {
  await invokeHost("resume_hotkeys");
}

export async function hideQuickWindow(): Promise<void> {
  await invokeHost("hide_quick_window");
}

/** Whether this platform can report clipboard changes without reading them. */
export async function clipboardWatchSupported(): Promise<boolean> {
  return (await invokeHost<boolean>("clipboard_watch_supported")) ?? false;
}

export type ClipboardPoll = {
  change_count: number;
  /** Contents, present only when the clipboard changed since the last poll. */
  text: string | null;
  /** Image resource, present instead of text when the clipboard holds an image. */
  image: number | null;
};

/**
 * Check whether the clipboard changed, reading the text only when it did.
 *
 * macOS has no change notification, so watching is a poll; the change counter
 * keeps each tick from touching the clipboard contents.
 */
export async function pollClipboard(since: number): Promise<ClipboardPoll | null> {
  return invokeHost<ClipboardPoll>("poll_clipboard", { since });
}

/** Convert a clipboard image resource into the PNG upload accepted by the App API. */
export async function clipboardImageFile(resourceId: number): Promise<File> {
  const image = new Image(resourceId);
  try {
    const [{ width, height }, rgba] = await Promise.all([image.size(), image.rgba()]);
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) {
      throw new Error("Canvas is unavailable.");
    }
    context.putImageData(new ImageData(new Uint8ClampedArray(rgba), width, height), 0, 0);
    const blob = await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(
        (value) => (value ? resolve(value) : reject(new Error("Clipboard image encoding failed."))),
        "image/png",
      );
    });
    return new File([blob], "clipboard.png", { type: "image/png" });
  } finally {
    await image.close();
  }
}

/** Release an image that arrived after its quick-run window was dismissed. */
export async function releaseClipboardImage(resourceId: number): Promise<void> {
  await new Image(resourceId).close();
}
