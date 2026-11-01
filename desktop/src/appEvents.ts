// Changes the server announces on `/api/events`.
//
// Every screen -- a browser tab, on any machine -- keeps its own query cache,
// so state that changes on the server has to be announced rather than left for
// each screen to notice.

import { subscribeEvents } from "./api/client";

/** How long a screen waits before listening again after the stream closes. */
const RECONNECT_DELAY_MS = 2000;

/**
 * Run `handler` whenever the server opens another workspace, from whichever
 * screen.
 *
 * The stream closes when the server restarts, so it is opened again after a
 * short wait until the listening stops. Each connection replays the server's
 * recent events; the handlers only re-read state, so a replay is harmless.
 *
 * Returns a function that stops listening.
 */
export function onWorkspaceChange(handler: () => void): () => void {
  let stopped = false;
  let retry: ReturnType<typeof setTimeout> | undefined;
  const listen = (): (() => void) =>
    subscribeEvents(
      (event) => {
        if (event.type === "workspace.changed") {
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
