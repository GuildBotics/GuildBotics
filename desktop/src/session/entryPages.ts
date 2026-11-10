// The pages the server serves `index.html` for besides the App, which the UI
// picks by `location.pathname`, and where entering returns to.

import { resumeSession, type AppAuthorizationRequest } from "../api/client";

/** This browser's session: whether it has one, taking up its CSRF value. */
export const SESSION_QUERY = {
  queryKey: ["session"],
  queryFn: resumeSession,
  retry: false,
  refetchOnWindowFocus: false,
} as const;

/** Where an entry link (`guildbotics open`) lands, with its code in the fragment. */
export const ENTER_PATH = "/-/guildbotics/-/enter";

/** Where a local app asks the owner to allow it (`guildbotics connect`). */
export const AUTHORIZE_PATH = "/-/guildbotics/-/authorize";

const LOOPBACK = ["127.0.0.1", "localhost", "[::1]"];

/**
 * A local app's request from the consent page's query, checked as the server
 * checks it before anything is offered; else the names of the invalid ones.
 */
export function parseAppRequest(search: string): AppAuthorizationRequest | { invalid: string[] } {
  const query = new URLSearchParams(search);
  const client = query.get("client") ?? "";
  const redirect = query.get("redirect_uri") ?? "";
  const state = query.get("state") ?? "";
  const challenge = query.get("code_challenge") ?? "";
  const url = URL.canParse(redirect) ? new URL(redirect) : null;
  const stateBytes = new TextEncoder().encode(state).length;
  const checks: [string, boolean][] = [
    ["client", /^[A-Za-z0-9 ._-]{1,64}$/.test(client) && client.trim() !== ""],
    [
      "redirect_uri",
      url !== null &&
        url.protocol === "http:" &&
        LOOPBACK.includes(url.hostname) &&
        !url.username &&
        !url.password &&
        !redirect.includes("#"),
    ],
    ["state", stateBytes >= 1 && stateBytes <= 512],
    ["code_challenge", /^[A-Za-z0-9_-]{43}$/.test(challenge)],
    ["code_challenge_method", query.get("code_challenge_method") === "S256"],
  ];
  const invalid = checks.filter(([, ok]) => !ok).map(([name]) => name);
  return invalid.length > 0
    ? { invalid }
    : { client, redirect_uri: redirect, state, code_challenge: challenge };
}

/** The app's address with the owner's refusal, as OAuth's redirect carries it. */
export function deniedRedirect({ redirect_uri, state }: AppAuthorizationRequest): string {
  const url = new URL(redirect_uri);
  url.searchParams.append("error", "access_denied");
  url.searchParams.append("state", state);
  return url.toString();
}

/**
 * `next` when it names a page of this server, which is all entering may open:
 * the server's rule for it, and no control characters, which a browser drops
 * from an address (`/<tab>/evil.example` would open `//evil.example`).
 */
export function samePagePath(next: string | null): string | null {
  // eslint-disable-next-line no-control-regex -- control characters are what it refuses
  return next !== null && /^\/(?![/\\])[^\s#\x00-\x1f\x7f]*$/.test(next) ? next : null;
}

export { rememberReturn, takeReturn } from "@guildbotics/ui/entryReturn";
