import { createApiClient } from "@guildbotics/ui/api";
// Low-level HTTP client for the Branvil API.
//
// Authentication is the HttpOnly `branvil_session_<port>` cookie; the CSRF value of
// the session is kept only in this module's memory (never in storage) and is
// sent on every non-GET request.

import type { Page } from "./types";

export const API_PREFIX = "/api/v1";

export function setCsrfToken(value: string | null): void {
  transport.setCsrf(value);
}

export function getCsrfToken(): string | null {
  return transport.getCsrf();
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

const transport = createApiClient({
  prefix: API_PREFIX,
  csrfHeader: "X-Branvil-CSRF",
  readError: async (response) => {
    let code = "http_error";
    let message = `${response.status} ${response.statusText}`.trim();
    try {
      const data = (await response.json()) as { error?: { code?: string; message?: string } };
      code = data.error?.code || code;
      message = data.error?.message || message;
    } catch { /* Keep the status line for a non-JSON response. */ }
    return new ApiError(response.status, code, message);
  },
  networkError: (err) => new ApiError(0, "network", err instanceof Error ? err.message : String(err)),
  recoveryError: (err) => {
    if (isApiError(err, "csrf_mismatch", 403) || isApiError(err, undefined, 401)) {
      setCsrfToken(null);
      return new ApiError(err.status, "session_changed", "The session could not be refreshed. Reload the page and try again.");
    }
    return err;
  },
});

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const file = opts.body instanceof Blob ? opts.body : null;
  if (opts.body !== undefined) headers["Content-Type"] = file ? file.type || "application/octet-stream" : "application/json";
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  const response = await transport.send(buildUrl(path, opts.query).slice(API_PREFIX.length), {
    method, headers,
    body: file ?? (opts.body === undefined ? undefined : JSON.stringify(opts.body)),
    signal: opts.signal,
  });
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
