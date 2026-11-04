/** Same-origin HTTP transport. Each app supplies its wire error contract. */
export function createApiClient<E extends Error & { code: string }>({
  prefix,
  csrfHeader,
  headers = () => ({}),
  readError,
  networkError = (error) => error,
  onConnectionLost = () => {},
  recoveryError = (error) => error,
}: {
  prefix: string;
  csrfHeader: string;
  headers?: () => Record<string, string>;
  readError: (response: Response) => Promise<E>;
  networkError?: (error: unknown) => unknown;
  onConnectionLost?: (loss: "signed_out" | "unreachable") => void;
  recoveryError?: (error: unknown) => unknown;
}) {
  let csrf: string | null = null;
  const setCsrf = (value: string | null) => {
    csrf = value;
  };

  async function attempt(path: string, options: RequestInit): Promise<Response> {
    let response: Response;
    try {
      response = await fetch(prefix + path, {
        ...options,
        credentials: "same-origin",
        headers: {
          ...headers(),
          ...options.headers,
          ...(options.method !== "GET" && options.method !== "HEAD" && csrf
            ? { [csrfHeader]: csrf }
            : {}),
        },
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      onConnectionLost("unreachable");
      throw networkError(error);
    }
    if (response.ok) return response;
    const error = await readError(response);
    if (response.status === 401) onConnectionLost("signed_out");
    throw Object.assign(error, { status: response.status });
  }

  async function send(path: string, options: RequestInit): Promise<Response> {
    try {
      return await attempt(path, options);
    } catch (error) {
      // A CSRF refusal happens before a write runs. Retry it only once.
      if (
        options.method === "GET" ||
        options.method === "HEAD" ||
        !(error instanceof Error) ||
        !("code" in error) ||
        error.code !== "csrf_mismatch" ||
        !("status" in error) ||
        error.status !== 403
      )
        throw error;
    }
    try {
      const response = await attempt("/session", { method: "GET", signal: options.signal });
      setCsrf(((await response.json()) as { csrf: string }).csrf);
      return await attempt(path, options);
    } catch (error) {
      throw recoveryError(error);
    }
  }
  return { send, setCsrf, getCsrf: () => csrf };
}
