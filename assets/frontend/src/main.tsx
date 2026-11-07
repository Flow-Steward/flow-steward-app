import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/app/App";
import { AppProviders } from "@/app/providers";
import { loadPlatformKitInBackground } from "@/app/platform-kit-loader";
import "../../platform-components.css";
import "@/styles/index.css";

const preloadReloadKey = "flow-steward:vite-preload-reload-at";
const preloadReloadWindowMs = 30_000;

window.addEventListener("vite:preloadError", (event) => {
  event.preventDefault();
  const now = Date.now();
  const lastReload = Number(sessionStorage.getItem(preloadReloadKey) || "0");
  if (Number.isFinite(lastReload) && now - lastReload < preloadReloadWindowMs) {
    return;
  }
  sessionStorage.setItem(preloadReloadKey, String(now));
  window.location.reload();
});

window.addEventListener(
  "load",
  () => {
    sessionStorage.removeItem(preloadReloadKey);
  },
  { once: true },
);

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("Missing #root mount element for the application UI.");
}

createRoot(rootElement).render(
  <StrictMode>
    <AppProviders>
      <App />
    </AppProviders>
  </StrictMode>,
);

// The app renders first; Platform Kit follows when the browser is idle (see the loader).
loadPlatformKitInBackground();
