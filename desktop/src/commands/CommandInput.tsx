import { Text, Textarea, type TextareaProps } from "@mantine/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import { uploadCommandInputFile } from "../api/client";

type CommandInputProps = Omit<TextareaProps, "onChange" | "value"> & {
  inputRef?: RefObject<HTMLTextAreaElement | null>;
  value: string;
  onChange: (value: string) => void;
};

type DropPosition = { x: number; y: number };

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
  ...textareaProps
}: CommandInputProps) {
  const { t } = useTranslation();
  const localInputRef = useRef<HTMLTextAreaElement>(null);
  const resolvedInputRef = inputRef ?? localInputRef;
  const valueRef = useRef(value);
  const [dropActive, setDropActive] = useState(false);
  const [uploadsInFlight, setUploadsInFlight] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);

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
        const inside = containsPhysicalPoint(resolvedInputRef.current, payload.position);
        if (payload.type === "drop") {
          setDropActive(false);
          if (inside) {
            appendPaths(payload.paths);
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
  }, [appendPaths, resolvedInputRef]);

  const uploadPastedImage = useCallback(
    async (file: File) => {
      setUploadError(null);
      setUploadsInFlight((count) => count + 1);
      try {
        const response = await uploadCommandInputFile(file);
        appendPaths([response.path]);
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

function containsPhysicalPoint(element: HTMLElement | null, position: DropPosition): boolean {
  if (!element) {
    return false;
  }
  const scale = window.devicePixelRatio || 1;
  const x = position.x / scale;
  const y = position.y / scale;
  const rect = element.getBoundingClientRect();
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

function isTauriRuntime(): boolean {
  return "__TAURI_INTERNALS__" in window;
}
