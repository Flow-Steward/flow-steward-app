import { useEffect, useRef } from "react";

import { openReconnectingWebSocket } from "@/lib/realtime/websocket";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

type JobStreamMessage = {
  type?: string;
  [key: string]: unknown;
};

type UseJobStreamOptions = {
  enabled?: boolean;
  onMessage?: (payload: JobStreamMessage) => void;
  onOpen?: () => void;
  onError?: (event: Event) => void;
};

function toWsOrigin() {
  if (globalThis.window === undefined) {
    return "";
  }
  const protocol = globalThis.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${globalThis.location.host}`;
}

export function useJobStream(jobId: string | number | null | undefined, options: UseJobStreamOptions = {}) {
  const onMessageRef = useRef(options.onMessage);
  const onOpenRef = useRef(options.onOpen);
  const onErrorRef = useRef(options.onError);

  useEffect(() => {
    onMessageRef.current = options.onMessage;
  }, [options.onMessage]);

  useEffect(() => {
    onOpenRef.current = options.onOpen;
  }, [options.onOpen]);

  useEffect(() => {
    onErrorRef.current = options.onError;
  }, [options.onError]);

  const normalizedJobId = firstStringFromUnknownValue([jobId, ""]).trim();
  const streamUrl = normalizedJobId ? `${toWsOrigin()}/ws/jobs/${encodeURIComponent(normalizedJobId)}` : "";

  useEffect(() => {
    if (!(options.enabled ?? true) || !streamUrl) {
      return undefined;
    }

    const socket = openReconnectingWebSocket(
      streamUrl,
      {
        onOpen: () => onOpenRef.current?.(),
        onError: (event) => onErrorRef.current?.(event),
        onMessage: (event) => {
          try {
            const payload = JSON.parse(event.data || "{}") as JobStreamMessage;
            onMessageRef.current?.(payload);
          } catch {
            onMessageRef.current?.({ type: "raw", text: event.data || "" });
          }
        },
      },
      { reconnectDelayMs: 1500, maxRetries: 10 },
    );

    return () => {
      socket.close();
    };
  }, [options.enabled, streamUrl]);
}
