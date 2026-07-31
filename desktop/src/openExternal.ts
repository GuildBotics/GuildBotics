export async function openExternal(url: string): Promise<void> {
  if (!url) {
    return;
  }
  if (typeof window !== "undefined" && "__TAURI_INTERNALS__" in window) {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}
