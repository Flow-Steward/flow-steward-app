export type SseReconnectOptions = {
  maxRetries?: number;
  reconnectDelayMs?: number;
  withCredentials?: boolean;
};

export type SseCallbacks = {
  onMessage?: (event: MessageEvent<string>) => void;
  onOpen?: () => void;
  onError?: (event: Event) => void;
};

export function openEventStream(
  url: string,
  callbacks: SseCallbacks = {},
  reconnect: SseReconnectOptions = {},
) {
  let eventSource: EventSource | null = null;
  let closed = false;
  let retries = 0;
  const maxRetries = reconnect.maxRetries ?? Infinity;
  const reconnectDelayMs = reconnect.reconnectDelayMs ?? 1500;
  const withCredentials = reconnect.withCredentials ?? true;

  function connect() {
    if (closed) {
      return;
    }

    eventSource = new EventSource(url, { withCredentials });
    eventSource.onopen = () => {
      retries = 0;
      callbacks.onOpen?.();
    };
    eventSource.onmessage = (event) => {
      callbacks.onMessage?.(event);
    };
    eventSource.onerror = (event) => {
      callbacks.onError?.(event);
      eventSource?.close();
      if (closed || retries >= maxRetries) {
        return;
      }
      retries += 1;
      setTimeout(connect, reconnectDelayMs);
    };
  }

  connect();

  return {
    close() {
      closed = true;
      eventSource?.close();
      eventSource = null;
    },
  };
}
