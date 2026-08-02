import { Textarea } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

export function isJsonRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

type JsonObjectFieldProps = {
  label: string;
  description?: string;
  /** Message shown while the text is not a JSON object. */
  errorText: string;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  /** Reports whether the text currently parses, so the page can block saving. */
  onValidityChange?: (valid: boolean) => void;
  minRows?: number;
  size?: string;
};

/**
 * Edit a nested settings object as JSON.
 *
 * A flat key/value grid cannot express these settings: providers disagree on
 * shape (a string for one, an integer for another, a nested object for a
 * third), so the text is parsed as JSON and types survive the round trip.
 *
 * The text is local state so a half-typed object can stay on screen with an
 * error instead of being reverted. It is seeded once per mount, so a caller
 * that switches which object is being edited must remount the field with a
 * `key` identifying that object.
 *
 * A field that reports invalid text and then goes away (remounted under a new
 * `key`, or removed with its slot) reports valid again as it unmounts, so the
 * caller is never left blocking on an error no one can see or correct.
 */
export function JsonObjectField({
  label,
  description,
  errorText,
  value,
  onChange,
  onValidityChange,
  minRows = 2,
  size,
}: JsonObjectFieldProps) {
  const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2));
  const [invalid, setInvalid] = useState(false);
  // The callback is re-created every render, but the entry it clears is fixed
  // for this mount, so the cleanup must not re-run when the identity changes.
  const reportValidity = useRef(onValidityChange);
  useEffect(() => {
    reportValidity.current = onValidityChange;
  });
  useEffect(() => () => reportValidity.current?.(true), []);

  return (
    <Textarea
      label={label}
      description={description}
      autosize
      minRows={minRows}
      size={size}
      value={text}
      error={invalid ? errorText : undefined}
      onChange={(event) => {
        const nextText = event.currentTarget.value;
        setText(nextText);
        const parsed = parseRecord(nextText);
        setInvalid(parsed === null);
        onValidityChange?.(parsed !== null);
        if (parsed !== null) onChange(parsed);
      }}
    />
  );
}

function parseRecord(text: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(text.trim() || "{}") as unknown;
    return isJsonRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}
