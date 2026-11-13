import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

const REFRESH_SECONDS_MIN = 10;
const REFRESH_SECONDS_MAX = 3600;

export type ExtensionPageRefresh = {
  /** Seconds between automatic reloads; 0 when the page declares none. */
  seconds: number;
  /** Automatic reloads stopped after a failed load, until the person presses Refresh. */
  stopped: boolean;
  /** The page loads data through queries, so a manual Refresh is offered. */
  available: boolean;
  refresh: () => void;
  loadStarted: () => void;
  loadFinished: (failed: boolean) => void;
};

/** The page's declared `refresh_seconds` (ADR 0067), or 0 when absent or out of bounds. */
export function pageRefreshSeconds(page: Record<string, unknown> | undefined): number {
  const value = page?.refresh_seconds;
  return typeof value === "number" && Number.isInteger(value)
    && value >= REFRESH_SECONDS_MIN && value <= REFRESH_SECONDS_MAX
    ? value
    : 0;
}

/**
 * Reloads a page's queries every `refresh_seconds` while the browser tab is
 * visible and no load is running; a failed load stops it until Refresh.
 */
export function useExtensionPageRefresh({
  pageKey,
  seconds,
  available,
  setComponentQueryRevision,
}: {
  /** Changes when the page, project or connection changes; restarts the cycle. */
  pageKey: string;
  seconds: number;
  available: boolean;
  setComponentQueryRevision: Dispatch<SetStateAction<number>>;
}): ExtensionPageRefresh {
  // The page key a failed load stopped; another page, project or connection starts again.
  const [stoppedKey, setStoppedKey] = useState<string | null>(null);
  const stoppedKeyRef = useRef<string | null>(null);
  const pageKeyRef = useRef(pageKey);
  const loadingRef = useRef(false);
  useEffect(() => {
    pageKeyRef.current = pageKey;
  }, [pageKey]);

  useEffect(() => {
    if (!seconds || !available) return undefined;
    const timer = globalThis.setInterval(() => {
      if (
        globalThis.document?.visibilityState !== "visible"
        || loadingRef.current
        || stoppedKeyRef.current === pageKeyRef.current
      ) return;
      setComponentQueryRevision((current) => current + 1);
    }, seconds * 1000);
    return () => globalThis.clearInterval(timer);
  }, [available, pageKey, seconds, setComponentQueryRevision]);

  const refresh = useCallback(() => {
    stoppedKeyRef.current = null;
    setStoppedKey(null);
    setComponentQueryRevision((current) => current + 1);
  }, [setComponentQueryRevision]);
  const loadStarted = useCallback(() => {
    loadingRef.current = true;
  }, []);
  const loadFinished = useCallback((failed: boolean) => {
    loadingRef.current = false;
    if (failed && seconds) {
      stoppedKeyRef.current = pageKeyRef.current;
      setStoppedKey(pageKeyRef.current);
    }
  }, [seconds]);
  const stopped = stoppedKey !== null && stoppedKey === pageKey;

  return { seconds, stopped, available, refresh, loadStarted, loadFinished };
}
