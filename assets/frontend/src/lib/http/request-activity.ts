let pendingRequests = 0;
let pageWaits = 0;
const listeners = new Set<() => void>();

export function hasPendingRequests(): boolean {
  return pendingRequests > 0;
}

export function subscribeToRequestActivity(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** Track the entire request, including reading its response body. */
export function beginRequest(): () => void {
  pendingRequests += 1;
  if (pendingRequests === 1) listeners.forEach((listener) => listener());

  let finished = false;
  return () => {
    if (finished) return;
    finished = true;
    pendingRequests -= 1;
    if (pendingRequests === 0) listeners.forEach((listener) => listener());
  };
}

/** Whether a page shows its own loader for the current wait. */
export function hasPageWait(): boolean {
  return pageWaits > 0;
}

/**
 * A page loader owns the wait while it is mounted, so the global request
 * indicator does not paint a second loader over it (canon: one loader per wait).
 */
export function beginPageWait(): () => void {
  pageWaits += 1;
  if (pageWaits === 1) listeners.forEach((listener) => listener());

  let finished = false;
  return () => {
    if (finished) return;
    finished = true;
    pageWaits -= 1;
    if (pageWaits === 0) listeners.forEach((listener) => listener());
  };
}
