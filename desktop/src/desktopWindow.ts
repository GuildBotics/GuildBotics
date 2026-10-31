/**
 * Whether the page runs in a Desktop (Tauri) window rather than a browser tab.
 *
 * The same UI serves both; what needs the host (the OS file dialogs, opening
 * a local file, hotkeys, cross-window events) works only in a Desktop window.
 */
export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Open a file of this machine in its default application (a Desktop window only). */
export async function openLocalFile(path: string): Promise<void> {
  const { open } = await import("@tauri-apps/plugin-shell");
  await open(path);
}
