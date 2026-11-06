/**
 * A number that changes whenever the page theme may have changed: the app's
 * `data-theme` attribute or class on the root element, or the system colour
 * scheme. The MCP Apps viewer applies host theme changes to the same root, so
 * one subscription serves every host.
 */
import { useEffect, useState } from "react";

export function useThemeRevision(): number {
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const bump = () => setRevision((value) => value + 1);
    const root = document.documentElement;
    const observer =
      typeof MutationObserver === "undefined" ? null : new MutationObserver(bump);
    observer?.observe(root, { attributes: true, attributeFilter: ["data-theme", "class"] });
    const media =
      typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    media?.addEventListener("change", bump);
    return () => {
      observer?.disconnect();
      media?.removeEventListener("change", bump);
    };
  }, []);
  return revision;
}
