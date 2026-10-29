type CanvasControlRect = Pick<DOMRect, "left" | "top" | "right" | "bottom">;

/** Choose a visible minimap corner that clears measured canvas controls. */
export function chooseCanvasMinimapRect(
  stage: CanvasControlRect,
  region: CanvasControlRect,
  minimap: Readonly<{ width: number; height: number }>,
  obstructions: readonly CanvasControlRect[],
  inset = 16,
  gap = 8,
): Readonly<{ left: number; top: number; right: number; bottom: number }> {
  const left = stage.left + inset;
  const right = Math.min(stage.right, region.right) - inset;
  const top = stage.top + inset;
  const bottom = stage.bottom - inset;
  const candidates = [
    { left: right - minimap.width, top: bottom - minimap.height },
    { left: right - minimap.width, top },
    { left, top: bottom - minimap.height },
    { left, top },
  ].map((point) => ({ ...point, right: point.left + minimap.width, bottom: point.top + minimap.height }));
  const overlapArea = (candidate: CanvasControlRect) => obstructions.reduce((sum, obstruction) => {
    const width = Math.max(0, Math.min(candidate.right + gap, obstruction.right) - Math.max(candidate.left - gap, obstruction.left));
    const height = Math.max(0, Math.min(candidate.bottom + gap, obstruction.bottom) - Math.max(candidate.top - gap, obstruction.top));
    return sum + width * height;
  }, 0);
  return candidates.find((candidate) => overlapArea(candidate) === 0)
    ?? candidates.reduce((best, candidate) => overlapArea(candidate) < overlapArea(best) ? candidate : best);
}
