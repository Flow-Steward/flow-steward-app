export type WebSocketReconnectOptions = {
  reconnectDelayMs?: number;
  maxRetries?: number;
};

export type WebSocketCallbacks = {
  onOpen?: () => void;
  onMessage?: (event: MessageEvent<string>) => void;
  onError?: (event: Event) => void;
  onClose?: (event: CloseEvent) => void;
};

export function openReconnectingWebSocket(
  url: string,
  callbacks: WebSocketCallbacks = {},
  reconnect: WebSocketReconnectOptions = {},
) {
  let socket: WebSocket | null = null;
  let closed = false;
  let retries = 0;
  const reconnectDelayMs = reconnect.reconnectDelayMs ?? 1500;
  const maxRetries = reconnect.maxRetries ?? Infinity;

  function connect() {
    if (closed) {
      return;
    }

    socket = new WebSocket(url);
    socket.onopen = () => {
      retries = 0;
      callbacks.onOpen?.();
    };
    socket.onmessage = (event) => {
      callbacks.onMessage?.(event);
    };
    socket.onerror = (event) => {
      callbacks.onError?.(event);
    };
    socket.onclose = (event) => {
      callbacks.onClose?.(event);
      if (closed || retries >= maxRetries) {
        return;
      }
      retries += 1;
      setTimeout(connect, reconnectDelayMs);
    };
  }

  connect();

  return {
    send(payload: string) {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(payload);
      }
    },
    close() {
      closed = true;
      socket?.close();
      socket = null;
    },
  };
}
