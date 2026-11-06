/**
 * Platform Kit (`/static/platform-kit/`) is maintenance-only; the React app renders none of its
 * elements. Its ~30 modules are served without content hashes, so the browser revalidates each one
 * on every full page load. Waiting for them before the first render left the page blank for seconds
 * on a busy server. They now load after the app has rendered, when the browser is idle, for any
 * older page that still uses a `platform-*` element.
 */
export const PLATFORM_KIT_URL = "/static/platform-kit/index.js";

type Schedule = (task: () => void) => void;

const whenIdle: Schedule = (task) => {
  if (typeof globalThis.requestIdleCallback === "function") globalThis.requestIdleCallback(() => task(), { timeout: 5_000 });
  else globalThis.setTimeout(task, 0);
};

export function loadPlatformKitInBackground({
  importModule = (url: string) => import(/* @vite-ignore */ url),
  schedule = whenIdle,
  warn = (message: string, error: unknown) => console.warn(message, error),
}: {
  importModule?: (url: string) => Promise<unknown>;
  schedule?: Schedule;
  warn?: (message: string, error: unknown) => void;
} = {}): void {
  schedule(() => {
    importModule(PLATFORM_KIT_URL).catch((error: unknown) => {
      warn("Platform kit failed to load; the application UI does not depend on it.", error);
    });
  });
}
