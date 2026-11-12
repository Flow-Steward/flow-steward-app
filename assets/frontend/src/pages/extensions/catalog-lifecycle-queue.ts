/**
 * The server runs one extension lifecycle operation at a time and refuses a
 * second one ("Another extension lifecycle operation is already in progress").
 * Catalog installs and updates started from this tab therefore wait their turn
 * here instead: a second Update click queues behind the first and starts when
 * it ends, whatever the first one's result.
 *
 * Only server calls are queued. An update that needs browser-authority
 * approval leaves the queue while the operator decides, and the approved
 * retry queues again, so an abandoned dialog never blocks the queue.
 */
let tail: Promise<unknown> = Promise.resolve();
let pending = 0;

/** True while a queued call runs or waits: a new call would queue behind it. */
export function catalogLifecycleBusy(): boolean {
  return pending > 0;
}

/** Run `operation` after every call queued before it; its own result is returned. */
export function runCatalogLifecycle<T>(operation: () => Promise<T>): Promise<T> {
  pending += 1;
  const result = tail.then(operation);
  tail = result.catch(() => undefined).finally(() => {
    pending -= 1;
  });
  return result;
}
