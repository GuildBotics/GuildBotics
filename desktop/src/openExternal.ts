import { isTauriRuntime } from "./desktopWindow";

/** Open a page outside GuildBotics: the system browser from a Desktop window, a new tab in a browser. */
export async function openExternal(url: string): Promise<void> {
  if (!url) {
    return;
  }
  if (isTauriRuntime()) {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}
