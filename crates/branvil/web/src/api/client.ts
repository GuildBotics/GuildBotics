// Low-level HTTP client for the Branvil API.
//
// Authentication is the HttpOnly `branvil_session` cookie; the CSRF value of
// the session is kept only in this module's memory (never in storage) and is
// sent on every non-GET request.

import type { Page, Session } from "./types";

export const API_PREFIX = "/api/v1";

let csrfToken: string | null = null;

export function setCsrfToken(value: string | null): void {
  csrfToken = value;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

/** An error answered by the service, or a network failure (status 0). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function isApiError(err: unknown, code?: string, status?: number): err is ApiError {
  if (!(err instanceof ApiError)) return false;
  if (code !== undefined && err.code !== code) return false;
  if (status !== undefined && err.status !== status) return false;
  return true;
}

/** Encodes a path parameter (repository, workspace, branch) as one segment. */
export function enc(segment: string | number): string {
  return encodeURIComponent(String(segment));
}

export type Query = Record<string, string | number | boolean | undefined | null>;

export interface RequestOptions {
  body?: unknown;
  query?: Query;
  signal?: AbortSignal;
  idempotencyKey?: string;
}

function buildUrl(path: string, query?: Query): string {
  let url = API_PREFIX + path;
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
    }
    const qs = params.toString();
    if (qs) url += (url.includes("?") ? "&" : "?") + qs;
  }
  return url;
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  try {
    return await send<T>(method, path, opts);
  } catch (err) {
    if (method === "GET" || method === "HEAD" || !isApiError(err, "csrf_mismatch", 403)) throw err;
  }

  // Another tab may have replaced the shared cookie. Authentication refused
  // the write before its handler ran, so only this refusal is safe to retry.
  try {
    const session = await send<Session>("GET", "/session", { signal: opts.signal });
    setCsrfToken(session.csrf);
    return await send<T>(method, path, opts);
  } catch (err) {
    if (isApiError(err, "csrf_mismatch", 403) || isApiError(err, undefined, 401)) {
      setCsrfToken(null);
      throw new ApiError(err.status, "session_changed", "The session could not be refreshed. Reload the page and try again.");
    }
    throw err;
  }
}

/** One attempt; recovery calls this directly so a retry cannot retry itself. */
async function send<T>(method: string, path: string, opts: RequestOptions): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  // A file (a member's picture) goes as it is; everything else as JSON.
  const file = opts.body instanceof Blob ? opts.body : null;
  if (opts.body !== undefined) headers["Content-Type"] = file ? file.type || "application/octet-stream" : "application/json";
  if (method !== "GET" && method !== "HEAD" && csrfToken) headers["X-Branvil-CSRF"] = csrfToken;
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body: file ?? (opts.body === undefined ? undefined : JSON.stringify(opts.body)),
      credentials: "same-origin",
      signal: opts.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network", err instanceof Error ? err.message : String(err));
  }

  if (!response.ok) {
    let code = "http_error";
    let message = `${response.status} ${response.statusText}`.trim();
    try {
      const data = (await response.json()) as { error?: { code?: string; message?: string } };
      if (data.error?.code) code = data.error.code;
      if (data.error?.message) message = data.error.message;
    } catch {
      // Not a JSON error body; keep the status line.
    }
    throw new ApiError(response.status, code, message);
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const get = <T>(path: string, query?: Query, signal?: AbortSignal) =>
  request<T>("GET", path, { query, signal });

/**
 * Follows `next_cursor` until the collection ends or `maxItems` is reached.
 * Returns the items and whether the collection was cut short.
 */
export async function getAll<T>(
  path: string,
  query: Query = {},
  opts: { maxItems?: number; signal?: AbortSignal } = {},
): Promise<{ items: T[]; complete: boolean }> {
  const items: T[] = [];
  let cursor: string | null = null;
  const max = opts.maxItems ?? Infinity;
  for (;;) {
    const page: Page<T> = await get<Page<T>>(path, { ...query, limit: 100, cursor }, opts.signal);
    items.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) return { items, complete: true };
    if (items.length >= max) return { items, complete: false };
  }
}

/** A fresh key for one user-initiated create, so a retried request is not applied twice. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
