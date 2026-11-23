import { useCallback, useState } from "react";

export function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Remembering is a convenience.
  }
}

export function takeStored(key: string): string | null {
  try {
    const value = window.localStorage.getItem(key);
    window.localStorage.removeItem(key);
    return value;
  } catch {
    return null;
  }
}

/** A value kept in this browser under `key`, and the setter that keeps it (stable while `key` is). */
export function useStored(key: string): [string | null, (value: string) => void] {
  const [value, setValue] = useState(() => readStored(key));
  const keep = useCallback(
    (next: string) => {
      setValue(next);
      writeStored(key, next);
    },
    [key],
  );
  return [value, keep];
}
