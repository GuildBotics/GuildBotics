// One SSE connection per origin. The lock is held until this subscriber stops;
// closing a document also releases it, so a queued tab takes over automatically.
const STREAM_NAME = "branvil-change-stream";
const MAX_BACKOFF_MS = 30_000;

export type StreamMessage =
  | { type: "status"; downSince: number | null }
  | { type: "change"; data: string }
  | { type: "reset" };

export function subscribeChangeStream(receive: (message: StreamMessage) => void): () => void {
  const channel = new BroadcastChannel(STREAM_NAME);
  const abort = new AbortController();
  let source: EventSource | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let release: (() => void) | undefined;
  let stopped = false;
  let leader = false;
  let downSince: number | null = Date.now();

  const publish = (message: StreamMessage) => {
    receive(message);
    channel.postMessage(message);
  };
  const status = () => ({ type: "status", downSince }) as const;
  channel.onmessage = ({ data }: MessageEvent<StreamMessage | { type: "hello" }>) => {
    if (data.type === "hello") {
      if (leader) channel.postMessage(status());
    } else if (!leader) {
      if (data.type === "status") downSince = data.downSince;
      receive(data);
    }
  };

  // Joining tabs need the current connection state even if no change arrives.
  receive(status());
  channel.postMessage({ type: "hello" });
  void navigator.locks.request(STREAM_NAME, { signal: abort.signal }, async () => {
    if (stopped) return;
    leader = true;
    downSince ??= Date.now();
    let backoff = 1000;
    const connect = () => {
      source = new EventSource("/api/v1/events", { withCredentials: true });
      source.onopen = () => {
        backoff = 1000;
        downSince = null;
        publish(status());
      };
      source.addEventListener("change", (e) => publish({ type: "change", data: (e as MessageEvent<string>).data }));
      source.addEventListener("reset", () => publish({ type: "reset" }));
      source.onerror = () => {
        downSince ??= Date.now();
        publish(status());
        // EventSource reconnects CONNECTING streams itself. CLOSED streams
        // (including a service shutdown) need a new instance after a backoff.
        if (source?.readyState === EventSource.CLOSED) {
          source.close();
          retry = setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        }
      };
    };
    publish(status());
    connect();
    await new Promise<void>((resolve) => { release = resolve; });
  }).catch((error: unknown) => {
    if (!stopped) {
      // Lock acquisition failure must not open independent per-tab streams.
      console.error("Could not acquire the change stream lock", error);
    }
  });

  return () => {
    stopped = true;
    abort.abort();
    clearTimeout(retry);
    source?.close();
    channel.close();
    release?.();
  };
}
