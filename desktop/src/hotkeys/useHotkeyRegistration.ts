// Keeps the OS registrations in step with the workspace configuration.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { getHotkeys } from "../api/client";
import { onAppEvent } from "../appEvents";
import { isTauriRuntime } from "../desktopWindow";
import { syncHotkeys } from "./hotkeyRuntime";

/**
 * Register the workspace's hotkeys and keep the tray menu readable.
 *
 * The query is shared with the assignment screens, so switching workspaces (or
 * saving an assignment) re-registers without extra plumbing. An assignment
 * saved from another screen -- a browser tab -- arrives as the server's
 * announcement, so the Desktop window re-reads and re-registers then too.
 */
export function useHotkeyRegistration(): void {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const hotkeys = useQuery({ queryKey: ["hotkeys"], queryFn: getHotkeys });
  const settings = hotkeys.data;

  useEffect(() => {
    if (settings) {
      void syncHotkeys(settings);
    }
  }, [settings]);

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }
    return onAppEvent(
      "hotkeys.changed",
      () => void queryClient.invalidateQueries({ queryKey: ["hotkeys"] }),
    );
  }, [queryClient]);

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }
    void (async () => {
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke("set_tray_labels", {
          show: t("tray.show"),
          quit: t("tray.quit"),
          appQuit: t("tray.appQuit"),
        });
      } catch {
        // The tray is unavailable in test harnesses and browser previews.
      }
    })();
  }, [t]);
}
