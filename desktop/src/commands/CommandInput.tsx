import {
  ActionIcon,
  Button,
  Group,
  Stack,
  Text,
  Textarea,
  Tooltip,
  type TextareaProps,
} from "@mantine/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import {
  checkCommandInputPaths,
  copyCommandInputFile,
  uploadCommandInputFile,
  type CommandInputGrantSuggestion,
  type CommandInputPathStatus,
} from "../api/client";
import { isMacPlatform } from "../hotkeys/accelerator";
import { openMainWindow } from "../hotkeys/hotkeyRuntime";

type CommandInputProps = Omit<TextareaProps, "onChange" | "value"> & {
  inputRef?: RefObject<HTMLTextAreaElement | null>;
  value: string;
  onChange: (value: string) => void;
  /** Where the command will run, when the screen knows; a dropped file there is reachable. */
  cwd?: string;
};

type DropPosition = { x: number; y: number };

/**
 * The settings screen where a grant that would open a dropped path is added:
 * the advanced intelligence panel, scrolled to the right card, with the
 * directory already in its path field.
 */
export function grantSettingsRoute(grant: CommandInputGrantSuggestion): string {
  const search = new URLSearchParams({
    section: "intelligence",
    advanced: "intelligence",
    focus: grant.scope === "document" ? "grants-documents" : "grants-device",
    grant: grant.path,
  });
  return `/setup?${search.toString()}`;
}

export function appendCommandInputPaths(value: string, paths: string[]): string {
  const additions = paths.filter(Boolean);
  if (!additions.length) {
    return value;
  }
  const separator = value && !value.endsWith("\n") ? "\n" : "";
  return `${value}${separator}${additions.join("\n")}`;
}

export function CommandInput({
  inputRef,
  value,
  onChange,
  onPaste,
  cwd,
  ...textareaProps
}: CommandInputProps) {
  const { t } = useTranslation();
  const localInputRef = useRef<HTMLTextAreaElement>(null);
  const resolvedInputRef = inputRef ?? localInputRef;
  const valueRef = useRef(value);
  const [dropActive, setDropActive] = useState(false);
  const [uploadsInFlight, setUploadsInFlight] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  /** Dropped paths the isolated agent environment cannot reach, awaiting the user's choice. */
  const [unreachable, setUnreachable] = useState<CommandInputPathStatus[]>([]);

  useEffect(() => {
    valueRef.current = value;
  }, [value]);

  const appendPaths = useCallback(
    (paths: string[]) => {
      const next = appendCommandInputPaths(valueRef.current, paths);
      valueRef.current = next;
      onChange(next);
    },
    [onChange],
  );

  /**
   * A dropped path goes in as it is when a turn would see it there; otherwise
   * the user decides between a copy the environment can reach and the original.
   * A missing path is not a file to hand over, so it goes in untouched too.
   */
  const acceptDroppedPaths = useCallback(
    async (paths: string[]) => {
      setUploadError(null);
      let described: CommandInputPathStatus[];
      try {
        described = (await checkCommandInputPaths({ paths, cwd })).paths;
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : String(error));
        appendPaths(paths);
        return;
      }
      const held = described.filter((entry) => !entry.reachable && entry.kind !== "missing");
      appendPaths(
        described.filter((entry) => !held.includes(entry)).map((entry) => entry.guest_path),
      );
      setUnreachable((current) => [...current, ...held]);
    },
    [appendPaths, cwd],
  );

  /**
   * Hand over a copy the environment can reach, keep the original and go and
   * open its directory to agents (the path goes in as it is, so the command
   * is ready once the grant is saved), or drop the file altogether.
   */
  const settleUnreachable = useCallback(
    async (entry: CommandInputPathStatus, choice: "copy" | "grant" | "dismiss") => {
      setUnreachable((current) => current.filter((held) => held !== entry));
      if (choice === "dismiss") {
        return;
      }
      if (choice === "grant") {
        appendPaths([entry.guest_path]);
        if (entry.grant) {
          void openMainWindow(grantSettingsRoute(entry.grant));
        }
        return;
      }
      setUploadError(null);
      setUploadsInFlight((count) => count + 1);
      try {
        appendPaths([(await copyCommandInputFile(entry.path)).guest_path]);
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : String(error));
      } finally {
        setUploadsInFlight((count) => count - 1);
      }
    },
    [appendPaths],
  );

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void getCurrentWebview()
      .onDragDropEvent((event) => {
        const payload = event.payload;
        if (payload.type === "leave") {
          setDropActive(false);
          return;
        }
        const inside = containsDropPoint(resolvedInputRef.current, payload.position);
        if (payload.type === "drop") {
          setDropActive(false);
          if (inside) {
            void acceptDroppedPaths(payload.paths);
          }
          return;
        }
        setDropActive(inside);
      })
      .then((stopListening) => {
        if (disposed) {
          stopListening();
        } else {
          unlisten = stopListening;
        }
      })
      .catch(() => {});
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [acceptDroppedPaths, resolvedInputRef]);

  const uploadPastedImage = useCallback(
    async (file: File) => {
      setUploadError(null);
      setUploadsInFlight((count) => count + 1);
      try {
        const response = await uploadCommandInputFile(file);
        appendPaths([response.guest_path]);
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : String(error));
      } finally {
        setUploadsInFlight((count) => count - 1);
      }
    },
    [appendPaths],
  );

  return (
    <div className={dropActive ? "command-input command-input-drop-active" : "command-input"}>
      <Textarea
        {...textareaProps}
        ref={resolvedInputRef}
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        onPaste={(event) => {
          onPaste?.(event);
          if (event.defaultPrevented) {
            return;
          }
          const file = pastedImage(event.clipboardData.items);
          if (file) {
            event.preventDefault();
            void uploadPastedImage(file);
          }
        }}
      />
      {dropActive ? (
        <Text size="xs" c="blue" role="status">
          {t("commands.inputFileDropActive")}
        </Text>
      ) : null}
      {unreachable.length ? (
        <Stack gap={4} role="group" aria-label={t("commands.inputPathUnreachable")}>
          <Text size="xs" c="dimmed">
            {t("commands.inputPathUnreachable")}
          </Text>
          {unreachable.map((entry) => (
            <Group key={entry.path} gap="xs" wrap="nowrap">
              <Text size="xs" ff="monospace" style={{ flex: 1, wordBreak: "break-all" }}>
                {entry.path}
              </Text>
              {entry.kind === "file" ? (
                <Button
                  size="compact-xs"
                  variant="light"
                  onClick={() => void settleUnreachable(entry, "copy")}
                >
                  {t("commands.inputPathCopy")}
                </Button>
              ) : null}
              {entry.grant ? (
                <Button
                  size="compact-xs"
                  variant="subtle"
                  onClick={() => void settleUnreachable(entry, "grant")}
                >
                  {t("commands.inputPathGrant")}
                </Button>
              ) : null}
              <Tooltip label={t("commands.inputPathDismiss")}>
                <ActionIcon
                  aria-label={t("commands.inputPathDismiss")}
                  size="xs"
                  variant="subtle"
                  color="gray"
                  onClick={() => void settleUnreachable(entry, "dismiss")}
                >
                  <X size={12} />
                </ActionIcon>
              </Tooltip>
            </Group>
          ))}
          <Text size="xs" c="dimmed">
            {t("commands.inputPathHint")}
          </Text>
        </Stack>
      ) : null}
      {uploadsInFlight > 0 ? (
        <Text size="xs" c="dimmed" role="status">
          {t("commands.inputFileSaving")}
        </Text>
      ) : null}
      {uploadError ? (
        <Text size="xs" c="red" role="alert">
          {t("commands.inputFileSaveError", { message: uploadError })}
        </Text>
      ) : null}
    </div>
  );
}

function pastedImage(items: DataTransferItemList): File | null {
  for (const item of Array.from(items)) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      return item.getAsFile();
    }
  }
  return null;
}

/**
 * Whether a drop landed on the element. The host reports the position as
 * physical pixels, except on macOS, where wry reads the view's own
 * coordinates (points, i.e. CSS pixels) and Tauri wraps them unscaled
 * (`wry/src/wkwebview/drag_drop.rs`); dividing those by the pixel ratio put
 * the target below and to the right of the field on Retina displays.
 */
function containsDropPoint(element: HTMLElement | null, position: DropPosition): boolean {
  if (!element) {
    return false;
  }
  const scale = isMacPlatform() ? 1 : window.devicePixelRatio || 1;
  const x = position.x / scale;
  const y = position.y / scale;
  const rect = element.getBoundingClientRect();
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

function isTauriRuntime(): boolean {
  return "__TAURI_INTERNALS__" in window;
}
