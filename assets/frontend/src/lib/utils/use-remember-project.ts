import { useLayoutEffect } from "react";

/** Keep extension connection selection aligned with the last visited project. */
export function useRememberProject(projectId: string | null) {
  useLayoutEffect(() => {
    if (!projectId) return;
    try {
      globalThis.localStorage.setItem("platform-last-project-id", projectId);
    } catch {
      // Storage can be unavailable in restricted browser contexts.
    }
  }, [projectId]);
}
