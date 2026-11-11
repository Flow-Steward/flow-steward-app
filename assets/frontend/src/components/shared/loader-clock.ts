import { LOADER_MIN_SHOWN_MS } from "@/components/shared/use-loader-delay";

/**
 * When the route-level page loader went on screen (0 = it is not showing).
 *
 * A Suspense fallback cannot hold itself for the minimum display time: React
 * unmounts it the moment the lazy page resolves. So the lazy page holds its own
 * resolution instead (`holdForLoader`) until the loader, if it appeared, has
 * been visible for `LOADER_MIN_SHOWN_MS`. Only `PageLoadingState` marks it.
 */
let shownAt = 0;

/** The earliest paint wins: a loader that takes over a painted wait keeps its start. */
export function markLoaderShown(at = Date.now()) {
  if (!shownAt || at < shownAt) shownAt = at;
}

export function markLoaderHidden() {
  shownAt = 0;
}

/** A page module that loaded before the loader painted is never delayed. */
export async function holdForLoader<T>(load: Promise<T>, min = LOADER_MIN_SHOWN_MS): Promise<T> {
  const loaded = await load;
  const wait = shownAt ? shownAt + min - Date.now() : 0;
  if (wait > 0) await new Promise((resolve) => { globalThis.setTimeout(resolve, wait); });
  return loaded;
}
