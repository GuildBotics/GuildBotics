import { Alert, Button, Text, TextInput } from "@mantine/core";
import { CircleAlert, FileKey, FolderOpen } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

// Form parts the setup screens and the providers' panels share.

export function FilePicker({
  label,
  withAsterisk,
  value,
  onChange,
  error,
}: {
  label: string;
  withAsterisk?: boolean;
  value: string;
  onChange: (value: string) => void;
  error?: string;
}) {
  const { t } = useTranslation();
  const [picking, setPicking] = useState(false);
  const pickFile = async () => {
    setPicking(true);
    try {
      if (!isTauriRuntime()) {
        return;
      }
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({ directory: false, multiple: false });
      if (typeof selected === "string") {
        onChange(selected);
      }
    } finally {
      setPicking(false);
    }
  };
  return (
    <div className="field-action-row">
      <TextInput
        label={withAsterisk ? <RequiredLabel text={label} /> : label}
        aria-label={label}
        aria-required={withAsterisk}
        leftSection={<FileKey size={16} />}
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        error={error}
        flex={1}
      />
      <Button
        className="field-action-button"
        leftSection={<FolderOpen size={16} />}
        loading={picking}
        onClick={pickFile}
        variant="default"
      >
        {t("setup.project.choose")}
      </Button>
    </div>
  );
}

export function RequiredLabel({ text }: { text: string }) {
  return (
    <>
      {text}
      <Text span c="danger" inherit aria-hidden="true">
        {" *"}
      </Text>
    </>
  );
}

export function InfoCallout({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Alert color="warning" icon={<CircleAlert size={18} />} title={title}>
      {children}
    </Alert>
  );
}

export function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}
