import { Text, Textarea, type TextareaProps } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { uploadCommandInputFile } from "../api/client";

type CommandInputProps = Omit<TextareaProps, "onChange" | "value"> & {
  value: string;
  onChange: (value: string) => void;
};

/** `value` with `path` on a line of its own after it. */
export function appendCommandInputPath(value: string, path: string): string {
  const separator = value && !value.endsWith("\n") ? "\n" : "";
  return `${value}${separator}${path}`;
}

export function CommandInput({ value, onChange, onPaste, ...textareaProps }: CommandInputProps) {
  const { t } = useTranslation();
  const valueRef = useRef(value);
  const [dropActive, setDropActive] = useState(false);
  const [uploadsInFlight, setUploadsInFlight] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [folderRefused, setFolderRefused] = useState(false);

  useEffect(() => {
    valueRef.current = value;
  }, [value]);

  const appendPath = useCallback(
    (path: string) => {
      const next = appendCommandInputPath(valueRef.current, path);
      valueRef.current = next;
      onChange(next);
    },
    [onChange],
  );

  const uploadCopy = useCallback(
    async (file: File) => {
      setUploadError(null);
      setUploadsInFlight((count) => count + 1);
      try {
        const response = await uploadCommandInputFile(file);
        appendPath(response.guest_path);
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : String(error));
      } finally {
        setUploadsInFlight((count) => count - 1);
      }
    },
    [appendPath],
  );

  // A drop hands over a copy of each file: the browser has no path the server
  // machine could read, so the file travels as a pasted image does. A folder
  // cannot travel that way; its server path is typed instead.
  return (
    <div
      className={dropActive ? "command-input command-input-drop-active" : "command-input"}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) {
          event.preventDefault();
          setDropActive(true);
        }
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setDropActive(false);
        }
      }}
      onDrop={(event) => {
        if (!event.dataTransfer.types.includes("Files")) {
          return;
        }
        event.preventDefault();
        setDropActive(false);
        const items = Array.from(event.dataTransfer.items).filter((item) => item.kind === "file");
        const folders = items.filter((item) => item.webkitGetAsEntry()?.isDirectory);
        setFolderRefused(folders.length > 0);
        for (const item of items) {
          const file = folders.includes(item) ? null : item.getAsFile();
          if (file) {
            void uploadCopy(file);
          }
        }
      }}
    >
      <Textarea
        {...textareaProps}
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
            void uploadCopy(file);
          }
        }}
      />
      {dropActive ? (
        <Text size="xs" c="blue" role="status">
          {t("commands.inputFileDropActive")}
        </Text>
      ) : null}
      {folderRefused ? (
        <Text size="xs" c="red" role="alert">
          {t("commands.inputFolderRefused")}
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
