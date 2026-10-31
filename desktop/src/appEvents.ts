// Changes the server announces on `/api/events`.
//
// Every screen -- a Desktop window or a browser tab, on any machine -- keeps
// its own query cache, so state that changes on the server has to be
// announced rather than left for each screen to notice.

import { subscribeEvents } from "./api/client";

/** What the server announces for every screen to react to. */
export type AppEvent = "workspace.changed" | "hotkeys.changed";

/** How long a screen waits before listening again after the stream closes. */
const RECONNECT_DELAY_MS = 2000;

/**
 * Run `handler` whenever the server publishes `type`.
 *
 * The stream closes when the server restarts, so it is opened again after a
 * short wait until the listening stops. Each connection replays the server's
 * recent events; the handlers only re-read state, so a replay is harmless.
 *
 * Returns a function that stops listening.
 */
export function onAppEvent(type: AppEvent, handler: () => void): () => void {
  let stopped = false;
  let retry: ReturnType<typeof setTimeout> | undefined;
  const listen = (): (() => void) =>
    subscribeEvents(
      (event) => {
        if (event.type === type) {
          handler();
        }
      },
      (status) => {
        if (status === "disconnected" && !stopped) {
          retry = setTimeout(() => (close = listen()), RECONNECT_DELAY_MS);
        }
      },
    );
  let close = listen();
  return () => {
    stopped = true;
    clearTimeout(retry);
    close();
  };
}

/** React to the server opening another workspace, from whichever screen. */
export function onWorkspaceChange(handler: () => void): () => void {
  return onAppEvent("workspace.changed", handler);
}
