import { useSyncExternalStore } from "react";

/** A screen worked by touch (a phone or a tablet), not by a mouse. */
const TOUCH_QUERY = "(pointer: coarse)";

function subscribeToTouch(onChange: () => void) {
  if (typeof globalThis.matchMedia !== "function") return () => undefined;
  const query = globalThis.matchMedia(TOUCH_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function touchSnapshot() {
  return typeof globalThis.matchMedia === "function" && globalThis.matchMedia(TOUCH_QUERY).matches;
}

/** Whether the main pointer is a finger: the camera is one tap away there. */
export function useTouchDevice(): boolean {
  return useSyncExternalStore(subscribeToTouch, touchSnapshot, () => false);
}
