import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";

import { startBackend } from "../api/backend";
import { onWorkspaceChange } from "../appEvents";
import { followAppLanguage } from "../i18n";
import "../i18n";
import { QuickRun } from "./QuickRun";
import type { QuickRunTrigger } from "./quickRunState";
import "@mantine/core/styles.css";
import "../styles.css";
import "./quick.css";

const queryClient = new QueryClient();

/** Bridge the host's hotkey event onto the component's activation callback. */
function subscribeToHotkey(handler: (trigger: QuickRunTrigger) => void): () => void {
  let stop: (() => void) | undefined;
  let cancelled = false;
  void (async () => {
    const { listen } = await import("@tauri-apps/api/event");
    const unlisten = await listen<QuickRunTrigger>("hotkey://triggered", (event) =>
      handler(event.payload),
    );
    if (cancelled) {
      unlisten();
    } else {
      stop = unlisten;
    }
  })();
  return () => {
    cancelled = true;
    stop?.();
  };
}

function QuickWindow() {
  // The window is created once and never reloaded, so a language change made in
  // the main window has to arrive as an event.
  useEffect(followAppLanguage, []);
  // Everything this window caches — commands, members — belongs to the
  // workspace, so a switch invalidates all of it.
  useEffect(() => onWorkspaceChange(() => void queryClient.invalidateQueries()), []);

  // This window shares the sidecar the host already started; connecting is only
  // about discovering its port and token.
  const connection = useQuery({
    queryKey: ["quick-run-connection"],
    queryFn: async () => {
      await startBackend();
      return true;
    },
    retry: false,
  });

  return connection.isSuccess ? <QuickRun subscribe={subscribeToHotkey} /> : null;
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <MantineProvider defaultColorScheme="auto">
      <QueryClientProvider client={queryClient}>
        <QuickWindow />
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
);
