// What this browser remembers for its viewer (the shown workspace, the last
// repository, the board's filter). A browser without storage still works; it
// just does not remember.

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
