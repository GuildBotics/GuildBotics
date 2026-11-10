// One pending browser consent flow, shared by the integrated screens.
const RETURN_KEY = "guildbotics.entryReturn";
const RETURN_MS = 10 * 60_000;
const CONSENT_PATHS = ["/-/authorize", "/-/guildbotics/-/authorize"];

function isConsentPath(value: string, paths: readonly string[]): boolean {
  // eslint-disable-next-line no-control-regex -- reject characters browsers normalize
  return !/[\s#\x00-\x1f\x7f]/.test(value) && paths.some((path) => value.startsWith(`${path}?`));
}

/**
 * Remember this page (the consent page, met without a session) for entering
 * to return to instead of the App, for 10 minutes.
 */
export function rememberReturn(): void {
  try {
    const path = window.location.pathname + window.location.search;
    window.localStorage.setItem(
      RETURN_KEY,
      JSON.stringify({ path, expires: Date.now() + RETURN_MS }),
    );
  } catch {
    // Without storage, entering opens the App; the app can be allowed again.
  }
}

/** The remembered consent page, once, while it has not expired. */
export function takeReturn(paths: readonly string[] = CONSENT_PATHS): string | null {
  try {
    const raw = window.localStorage.getItem(RETURN_KEY);
    window.localStorage.removeItem(RETURN_KEY);
    const saved = raw ? (JSON.parse(raw) as { path?: unknown; expires?: unknown }) : null;
    const fresh = typeof saved?.expires === "number" && saved.expires > Date.now();
    return fresh && typeof saved.path === "string" && isConsentPath(saved.path, paths)
      ? saved.path
      : null;
  } catch {
    return null;
  }
}
