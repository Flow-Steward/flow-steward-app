import { useEffect, useRef } from "react";

import { openEventStream, type SseReconnectOptions } from "@/lib/realtime/sse";

type UseEventStreamOptions = {
  enabled?: boolean;
  onMessage?: (event: MessageEvent<string>) => void;
  onOpen?: () => void;
  onError?: (event: Event) => void;
  reconnect?: SseReconnectOptions;
};

export function useEventStream(url: string, options: UseEventStreamOptions = {}) {
  const onMessageRef = useRef(options.onMessage);
  const onOpenRef = useRef(options.onOpen);
  const onErrorRef = useRef(options.onError);
  const reconnectRef = useRef(options.reconnect);

  useEffect(() => {
    onMessageRef.current = options.onMessage;
  }, [options.onMessage]);

  useEffect(() => {
    onOpenRef.current = options.onOpen;
  }, [options.onOpen]);

  useEffect(() => {
    onErrorRef.current = options.onError;
  }, [options.onError]);

  useEffect(() => {
    reconnectRef.current = options.reconnect;
  }, [options.reconnect]);

  useEffect(() => {
    if (!options.enabled || !url) {
      return undefined;
    }

    const stream = openEventStream(
      url,
      {
        onMessage: (event) => onMessageRef.current?.(event),
        onOpen: () => onOpenRef.current?.(),
        onError: (event) => onErrorRef.current?.(event),
      },
      reconnectRef.current,
    );

    return () => {
      stream.close();
    };
  }, [options.enabled, options.reconnect, url]);
}
