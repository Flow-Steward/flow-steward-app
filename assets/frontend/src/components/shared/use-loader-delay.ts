import { useEffect, useState } from "react";

export const LOADER_DELAY_MS = 400;
export const LOADER_MIN_SHOWN_MS = 500;

/**
 * A pending episode paints after 400ms and remains visible for at least 500ms.
 * Keep this owner mounted and pass pending to retain the minimum display time.
 * The no-argument compatibility form delays a mounted loading surface.
 */
export function useLoaderDelay(pending = true): boolean {
  const [display, setDisplay] = useState({ visible: false, shownAt: 0 });

  useEffect(() => {
    if (pending && display.visible) return;
    if (!pending && !display.visible) return;
    const delay = pending ? LOADER_DELAY_MS : Math.max(0, LOADER_MIN_SHOWN_MS - (Date.now() - display.shownAt));
    const timer = globalThis.setTimeout(() => {
      setDisplay(pending ? { visible: true, shownAt: Date.now() } : { visible: false, shownAt: 0 });
    }, delay);
    return () => globalThis.clearTimeout(timer);
  }, [pending, display.visible, display.shownAt]);

  return display.visible;
}
