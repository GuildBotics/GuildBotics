// Keeps the OS registrations in step with the workspace configuration.

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { getHotkeys } from "../api/client";
import { syncHotkeys } from "./hotkeyRuntime";

function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/**
 * Register the workspace's hotkeys and keep the tray menu readable.
 *
 * The query is shared with the assignment screens, so switching workspaces (or
 * saving an assignment) re-registers without extra plumbing.
 */
export function useHotkeyRegistration(): void {
  const { t } = useTranslation();
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
    void (async () => {
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke("set_tray_labels", { show: t("tray.show"), quit: t("tray.quit") });
      } catch {
        // The tray is unavailable in test harnesses and browser previews.
      }
    })();
  }, [t]);
}
