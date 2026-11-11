import { useLayoutEffect, useState } from "react";

export const LOADER_DELAY_MS = 400;
export const LOADER_MIN_SHOWN_MS = 500;

/**
 * A pending episode paints after 400ms and remains visible for at least 500ms.
 * Keep this owner mounted and pass pending to retain the minimum display time.
 * The no-argument compatibility form delays a mounted loading surface.
 *
 * The timer lives in a layout effect on purpose. A passive effect's cleanup
 * runs after the commit, so a 400ms timer that comes due as the work settles
 * could still fire in that gap: the real content would paint and then be
 * swapped for the loader for its whole minimum time, replacing the rows or
 * fields a person had just reached. A layout cleanup clears the timer in the
 * same commit that ends the pending state.
 */
export function useLoaderDelay(pending = true): boolean {
  const [display, setDisplay] = useState({ visible: false, shownAt: 0 });

  useLayoutEffect(() => {
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
