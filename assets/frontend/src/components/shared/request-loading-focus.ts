import { hasPendingRequests, subscribeToRequestActivity } from "@/lib/http/request-activity";

const requestOpeners = new WeakMap<HTMLElement, HTMLElement | null>();
const activeLoaders = new Set<HTMLElement>();
type DeferredFocus = {
  target: HTMLElement;
  onFocused: () => void;
  canFocus?: (target: HTMLElement) => boolean;
  /** Re-resolve an optional safe fallback at delivery; null declines focus. */
  resolveTarget?: () => HTMLElement | null;
  focusOptions?: FocusOptions;
};
const deferredFocus = new Set<DeferredFocus>();
let unsubscribeRequestActivity: (() => void) | null = null;

function releaseRequestActivitySubscription() {
  unsubscribeRequestActivity?.();
  unsubscribeRequestActivity = null;
}

function completeFocusWithoutLoader() {
  // A mounted loader owns closing its native modal and unregistering first.
  if (hasPendingRequests() || activeLoaders.size > 0) return;
  const activeModal = globalThis.document === undefined ? undefined : Array.from(document.querySelectorAll<HTMLElement>(
    'dialog[open][aria-modal="true"], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]',
  )).findLast((node) => !node.closest('[hidden], [aria-hidden="true"]'));
  completeRequestLoadingFocus(activeModal);
}

function deliverFocus(request: DeferredFocus, activeModal?: HTMLElement) {
  const target = request.resolveTarget ? request.resolveTarget() : request.target;
  if (!target?.isConnected || (activeModal && !activeModal.contains(target)) || request.canFocus?.(target) === false) return;
  if (request.focusOptions) target.focus(request.focusOptions);
  else target.focus();
  if (target.ownerDocument.activeElement === target) request.onFocused();
}

/** A dialog opened during loading must remember the user's control, not the temporary loader. */
export function resolveRequestLoadingOpener(element: HTMLElement | null): HTMLElement | null {
  return element && requestOpeners.has(element) ? requestOpeners.get(element) ?? null : element;
}

export function registerRequestLoadingOpener(loader: HTMLElement, opener: HTMLElement | null): () => void {
  requestOpeners.set(loader, resolveRequestLoadingOpener(opener));
  activeLoaders.add(loader);
  return () => {
    requestOpeners.delete(loader);
    activeLoaders.delete(loader);
  };
}

/** Wait for the native request dialog; optional guards are rechecked immediately before focus. */
export function focusAfterRequestLoading(
  target: HTMLElement | null,
  onFocused: () => void,
  options: { canFocus?: (target: HTMLElement) => boolean; resolveTarget?: () => HTMLElement | null; focusOptions?: FocusOptions } = {},
): () => void {
  if (!target) return () => {};
  const request = { target, onFocused, ...options };
  if (hasPendingRequests() || activeLoaders.size > 0) {
    deferredFocus.add(request);
    unsubscribeRequestActivity ??= subscribeToRequestActivity(completeFocusWithoutLoader);
    // Subscription does not replay current state; reconcile after installing it.
    completeFocusWithoutLoader();
  } else deliverFocus(request);
  return () => {
    deferredFocus.delete(request);
    if (deferredFocus.size === 0) releaseRequestActivitySubscription();
  };
}

/** Called after closing the loader and restoring its opener; a surviving modal retains priority. */
export function completeRequestLoadingFocus(activeModal?: HTMLElement): void {
  // StrictMode's temporary cleanup is not the end of the request episode.
  if (hasPendingRequests() || activeLoaders.size > 0) return;
  const requests = [...deferredFocus];
  deferredFocus.clear();
  releaseRequestActivitySubscription();
  requests.forEach((request) => deliverFocus(request, activeModal));
}
