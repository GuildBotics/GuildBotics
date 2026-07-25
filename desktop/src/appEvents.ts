// Cross-window notifications.
//
// Each window runs its own webview with its own query cache, so state that is
// scoped to the workspace has to be announced rather than left for the other
// window to notice.

const WORKSPACE_CHANGED = "app://workspace-changed";

function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Tell the other windows that the active workspace is now a different one. */
export async function announceWorkspaceChange(): Promise<void> {
  if (!isTauriRuntime()) {
    return;
  }
  try {
    const { emit } = await import("@tauri-apps/api/event");
    await emit(WORKSPACE_CHANGED);
  } catch {
    // The event API is unavailable (browser preview or a stubbed harness).
  }
}

/**
 * React to a workspace switch made in another window.
 *
 * Returns a function that stops listening.
 */
export function onWorkspaceChange(handler: () => void): () => void {
  if (!isTauriRuntime()) {
    return () => {};
  }
  let stop: (() => void) | undefined;
  let cancelled = false;
  void (async () => {
    try {
      const { listen } = await import("@tauri-apps/api/event");
      const unlisten = await listen(WORKSPACE_CHANGED, () => handler());
      if (cancelled) {
        unlisten();
      } else {
        stop = unlisten;
      }
    } catch {
      // The event API is unavailable; this window keeps its current data.
    }
  })();
  return () => {
    cancelled = true;
    stop?.();
  };
}
