import { useEffect, useState } from "react";

import { DsLoader } from "@/components/shared/ds-loader";
import { LOADER_DELAY_MS } from "@/components/shared/use-loader-delay";
import { beginPageWait } from "@/lib/http/request-activity";

/** Canon Loader: a wait shorter than this reads as a flash, so nothing paints. */
export { LOADER_DELAY_MS as PAGE_LOADING_DELAY_MS } from "@/components/shared/use-loader-delay";

import "@/components/shared/page-loading-state.css";

/**
 * The canon page loading state (03_components/data-display/loader.md): the
 * content region's own canvas with the wide page Loader centred in it and one
 * line under it that names what is being prepared.
 *
 * - No card, border, shadow or eyebrow: a wait asks nothing, so it gets no
 *   surface that implies it does.
 * - Nothing paints for the first 400ms. A session restored in 200ms shows an
 *   empty region, never a flash.
 * - The region is a status and announces once, when the line appears.
 * - Reduced motion swaps to the published still asset (themes.css).
 * - It owns the wait: the global request indicator paints nothing meanwhile,
 *   so the page never shows two loaders for one wait.
 */
/**
 * One wait across the page loaders that hand it on (the session restore, then
 * the page): how many are mounted, when it began and when a loader first
 * painted it. A loader that renders while another is still mounted takes the
 * wait over in the same commit: it paints at once if the wait was painted, and
 * otherwise counts the delay from the wait's start, so the handover never
 * flashes a loader and then a blank region. A loader after all have gone
 * starts a wait of its own.
 */
const pageWait = { open: 0, startedAt: 0, shownAt: 0 };

function joinPageWait(now: number): { handover: boolean; startedAt: number; visible: boolean } {
  const handover = pageWait.open > 0;
  return handover
    ? { handover, startedAt: pageWait.startedAt, visible: pageWait.shownAt > 0 }
    : { handover, startedAt: now, visible: false };
}

export function PageLoadingState({
  label,
  standalone = false,
}: Readonly<{
  /** What is being prepared, e.g. "Restoring your session" - never "Loading…". */
  label: string;
  /** Before the shell has painted: the region fills the viewport. */
  standalone?: boolean;
}>) {
  const [joined] = useState(() => joinPageWait(Date.now()));
  const [visible, setVisible] = useState(joined.visible);
  useEffect(() => beginPageWait(), []);
  useEffect(() => {
    pageWait.open += 1;
    if (!joined.handover) Object.assign(pageWait, { startedAt: joined.startedAt, shownAt: 0 });
    return () => {
      pageWait.open -= 1;
    };
  }, [joined]);
  useEffect(() => {
    if (visible) {
      if (!pageWait.shownAt) pageWait.shownAt = Date.now();
      return;
    }
    const timer = globalThis.setTimeout(() => setVisible(true), Math.max(0, LOADER_DELAY_MS - (Date.now() - joined.startedAt)));
    return () => globalThis.clearTimeout(timer);
  }, [joined, visible]);

  return (
    <div className={standalone ? "fs-page-loading is-standalone" : "fs-page-loading"} role="status">
      {visible ? (
        <>
          <DsLoader variant="wide-page" />
          <p className="fs-page-loading-line">{label}</p>
        </>
      ) : null}
    </div>
  );
}
