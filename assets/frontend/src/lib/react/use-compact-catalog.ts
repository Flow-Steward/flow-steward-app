import { useSyncExternalStore } from "react";

const PHONE_QUERY = "(width < 640px)";

function subscribeToPhone(onChange: () => void) {
  if (typeof globalThis.matchMedia !== "function") return () => undefined;
  const query = globalThis.matchMedia(PHONE_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function phoneSnapshot() {
  return typeof globalThis.matchMedia === "function" && globalThis.matchMedia(PHONE_QUERY).matches;
}

/** Below the design system's sm breakpoint a catalog uses its phone layout. */
export function useCompactCatalog(): boolean {
  return useSyncExternalStore(subscribeToPhone, phoneSnapshot, () => false);
}
