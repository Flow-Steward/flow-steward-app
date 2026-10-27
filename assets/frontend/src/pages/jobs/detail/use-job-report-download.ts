import { useCallback, useEffect, useRef, useState } from "react";

type ReportDownloadStatus = "idle" | "preparing" | "started" | "failed";

const REPORT_DOWNLOAD_COOKIE_PREFIX = "fs_job_report_download_";
const REPORT_PREPARATION_TIMEOUT_MS = 120_000;
const REPORT_STARTED_VISIBLE_MS = 4_000;
const REPORT_COOKIE_POLL_MS = 250;

export type JobReportDownloadController = {
  available: boolean;
  busy: boolean;
  start: () => void;
  status: ReportDownloadStatus;
};

function reportDownloadToken(): string {
  return globalThis.crypto.randomUUID().replaceAll("-", "");
}

function hasCookie(name: string): boolean {
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .some((part) => part.startsWith(`${name}=`));
}

function clearCookie(name: string): void {
  document.cookie = `${name}=; Max-Age=0; Path=/; SameSite=Lax`;
}

export function useJobReportDownload(reportUrl: string): JobReportDownloadController {
  const [status, setStatus] = useState<ReportDownloadStatus>("idle");
  const activeTokenRef = useRef("");
  const pollTimerRef = useRef<ReturnType<typeof globalThis.setInterval> | null>(null);
  const timeoutTimerRef = useRef<ReturnType<typeof globalThis.setTimeout> | null>(null);
  const resetTimerRef = useRef<ReturnType<typeof globalThis.setTimeout> | null>(null);

  const clearTimers = useCallback(() => {
    if (pollTimerRef.current !== null) {
      globalThis.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (timeoutTimerRef.current !== null) {
      globalThis.clearTimeout(timeoutTimerRef.current);
      timeoutTimerRef.current = null;
    }
    if (resetTimerRef.current !== null) {
      globalThis.clearTimeout(resetTimerRef.current);
      resetTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      clearTimers();
      const token = activeTokenRef.current;
      if (token) {
        clearCookie(`${REPORT_DOWNLOAD_COOKIE_PREFIX}${token}`);
      }
    };
  }, [clearTimers]);

  const start = useCallback(() => {
    if (!reportUrl || activeTokenRef.current) {
      return;
    }

    const token = reportDownloadToken();
    const cookieName = `${REPORT_DOWNLOAD_COOKIE_PREFIX}${token}`;
    activeTokenRef.current = token;
    clearTimers();
    clearCookie(cookieName);
    setStatus("preparing");

    pollTimerRef.current = globalThis.setInterval(() => {
      if (!hasCookie(cookieName)) {
        return;
      }
      clearTimers();
      clearCookie(cookieName);
      setStatus("started");
      resetTimerRef.current = globalThis.setTimeout(() => {
        activeTokenRef.current = "";
        resetTimerRef.current = null;
        setStatus("idle");
      }, REPORT_STARTED_VISIBLE_MS);
    }, REPORT_COOKIE_POLL_MS);

    timeoutTimerRef.current = globalThis.setTimeout(() => {
      clearTimers();
      clearCookie(cookieName);
      activeTokenRef.current = "";
      setStatus("failed");
    }, REPORT_PREPARATION_TIMEOUT_MS);

    try {
      const url = new URL(reportUrl, globalThis.location.origin);
      url.searchParams.set("download_token", token);
      const anchor = document.createElement("a");
      anchor.download = "";
      anchor.href = url.toString();
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } catch {
      clearTimers();
      clearCookie(cookieName);
      activeTokenRef.current = "";
      setStatus("failed");
    }
  }, [clearTimers, reportUrl]);

  return {
    available: Boolean(reportUrl),
    busy: status === "preparing" || status === "started",
    start,
    status,
  };
}
