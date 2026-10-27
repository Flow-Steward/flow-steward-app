import { useCallback, useEffect, useRef, useState } from "react";

import {
  BUILDER_DRAWER_RESIZE_BREAKPOINT,
  clampBuilderDrawerWidth,
  persistBuilderDrawerWidth,
  readStoredBuilderDrawerWidth,
} from "@/pages/workflows/detail/detail-helpers";

export function useWorkflowBuilderDrawerResize(builderSurface: boolean) {
  const [builderDrawerWidth, setBuilderDrawerWidth] = useState(
    readStoredBuilderDrawerWidth,
  );
  const [builderDrawerViewportWidth, setBuilderDrawerViewportWidth] = useState(
    () =>
      globalThis.window === undefined
        ? BUILDER_DRAWER_RESIZE_BREAKPOINT
        : globalThis.innerWidth,
  );
  const builderDrawerResizeCleanupRef = useRef<(() => void) | null>(null);

  const builderDrawerResizable =
    builderSurface &&
    builderDrawerViewportWidth >= BUILDER_DRAWER_RESIZE_BREAKPOINT;
  const clampedBuilderDrawerWidth = clampBuilderDrawerWidth(
    builderDrawerWidth,
    builderDrawerViewportWidth,
  );
  const builderDrawerStyle = builderSurface
    ? {
        width: builderDrawerResizable
          ? `${clampedBuilderDrawerWidth}px`
          : "min(100vw, 100%)",
      }
    : undefined;

  useEffect(() => {
    if (!builderSurface || globalThis.window === undefined) {
      return;
    }
    const updateViewportWidth = () => {
      const viewportWidth = globalThis.innerWidth;
      setBuilderDrawerViewportWidth(viewportWidth);
      setBuilderDrawerWidth((current) =>
        clampBuilderDrawerWidth(current, viewportWidth),
      );
    };
    updateViewportWidth();
    globalThis.addEventListener("resize", updateViewportWidth);
    return () => globalThis.removeEventListener("resize", updateViewportWidth);
  }, [builderSurface]);

  const stopBuilderDrawerResize = useCallback(() => {
    builderDrawerResizeCleanupRef.current?.();
    builderDrawerResizeCleanupRef.current = null;
  }, []);

  const beginBuilderDrawerResize = useCallback(
    (initialClientX: number, pointerId: number, target: HTMLElement) => {
      if (
        !builderSurface ||
        !builderDrawerResizable ||
        globalThis.window === undefined
      ) {
        return;
      }
      stopBuilderDrawerResize();
      const previousCursor = document.body.style.cursor;
      const previousUserSelect = document.body.style.userSelect;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      try {
        target.setPointerCapture?.(pointerId);
      } catch {
        // Synthetic pointer events may not establish an active pointer.
      }

      const updateFromClientX = (clientX: number) => {
        const nextWidth = clampBuilderDrawerWidth(
          globalThis.innerWidth - clientX,
          globalThis.innerWidth,
        );
        setBuilderDrawerWidth(nextWidth);
        persistBuilderDrawerWidth(nextWidth);
      };
      const onPointerMove = (event: PointerEvent) => {
        event.preventDefault();
        updateFromClientX(event.clientX);
      };
      let active = true;
      const cleanup = () => {
        if (!active) {
          return;
        }
        active = false;
        document.body.style.cursor = previousCursor;
        document.body.style.userSelect = previousUserSelect;
        globalThis.removeEventListener("pointermove", onPointerMove);
        globalThis.removeEventListener("pointerup", cleanup);
        globalThis.removeEventListener("pointercancel", cleanup);
        globalThis.removeEventListener("blur", cleanup);
        target.removeEventListener("lostpointercapture", cleanup);
        try {
          if (target.hasPointerCapture?.(pointerId)) {
            target.releasePointerCapture(pointerId);
          }
        } catch {
          // Ignore stale pointer state during teardown.
        }
        if (builderDrawerResizeCleanupRef.current === cleanup) {
          builderDrawerResizeCleanupRef.current = null;
        }
      };
      globalThis.addEventListener("pointermove", onPointerMove, { passive: false });
      globalThis.addEventListener("pointerup", cleanup);
      globalThis.addEventListener("pointercancel", cleanup);
      globalThis.addEventListener("blur", cleanup);
      target.addEventListener("lostpointercapture", cleanup);
      builderDrawerResizeCleanupRef.current = cleanup;
      updateFromClientX(initialClientX);
    },
    [builderDrawerResizable, builderSurface, stopBuilderDrawerResize],
  );

  useEffect(() => stopBuilderDrawerResize, [stopBuilderDrawerResize]);

  return {
    beginBuilderDrawerResize,
    builderDrawerResizable,
    builderDrawerStyle,
    builderDrawerViewportWidth,
    builderDrawerWidth,
    clampedBuilderDrawerWidth,
    setBuilderDrawerWidth,
  };
}
