import { ActionIcon, TextInput } from "@mantine/core";
import { X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  acceleratorFromKeyPress,
  formatAccelerator,
  hasEnoughModifiers,
  isMacPlatform,
} from "./accelerator";

export type HotkeyInputProps = {
  value: string;
  onChange: (accelerator: string) => void;
  label?: string;
  description?: string;
  error?: string;
  disabled?: boolean;
  /** Overrides the notation used for display; defaults to the running platform. */
  isMac?: boolean;
  /**
   * Raised while the field is capturing. Callers must release the registered
   * global shortcuts for the duration: a registered accelerator is swallowed by
   * the OS and would never reach this field, making it impossible to re-record
   * the combination already in use.
   */
  onRecordingChange?: (recording: boolean) => void;
};

export function HotkeyInput(props: HotkeyInputProps) {
  const { t } = useTranslation();
  const [recording, setRecording] = useState(false);
  const [hint, setHint] = useState("");
  const isMac = props.isMac ?? isMacPlatform();

  const setRecordingState = (next: boolean) => {
    setRecording(next);
    setHint("");
    props.onRecordingChange?.(next);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    event.preventDefault();

    if (event.code === "Escape") {
      event.currentTarget.blur();
      return;
    }
    if ((event.code === "Backspace" || event.code === "Delete") && !hasModifier(event)) {
      props.onChange("");
      event.currentTarget.blur();
      return;
    }

    const accelerator = acceleratorFromKeyPress(event);
    if (!accelerator) {
      // A bare modifier: keep waiting for the key it belongs to.
      return;
    }
    if (!hasEnoughModifiers(accelerator)) {
      setHint(t("hotkey.needsModifier"));
      return;
    }

    props.onChange(accelerator);
    event.currentTarget.blur();
  };

  const display = recording
    ? t("hotkey.recording")
    : formatAccelerator(props.value, isMac) || t("hotkey.unset");

  return (
    <TextInput
      label={props.label}
      description={props.description}
      error={props.error || hint || undefined}
      disabled={props.disabled}
      readOnly
      value={display}
      aria-label={props.label ?? t("hotkey.label")}
      placeholder={t("hotkey.unset")}
      onFocus={() => setRecordingState(true)}
      onBlur={() => setRecordingState(false)}
      onKeyDown={handleKeyDown}
      rightSection={
        props.value && !recording ? (
          <ActionIcon
            variant="subtle"
            color="gray"
            aria-label={t("hotkey.clear")}
            onClick={() => props.onChange("")}
          >
            <X size={14} />
          </ActionIcon>
        ) : null
      }
    />
  );
}

function hasModifier(event: React.KeyboardEvent): boolean {
  return event.ctrlKey || event.altKey || event.shiftKey || event.metaKey;
}
