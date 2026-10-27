import { AlertCircle, CheckCircle2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { DsLoader } from "@/components/shared/ds-loader";
import { DsToast } from "@/components/shared/design-system";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { useI18n } from "@/lib/i18n/useI18n";
import type { GraphEditNotice } from "./canvas-core";
import { canvasToastDuration } from "./canvas-toast-feed";

/**
 * The canvas toast at the top of the canvas: what a change did, with Undo when it is cheap.
 * Hovering holds it; an error stays until its × dismisses it.
 */
export function CanvasToast({ notice, onDismiss, held = false, low = false }: Readonly<{
  notice: GraphEditNotice | null;
  onDismiss: () => void;
  /** A gesture in progress (dragging a connection) keeps its guidance on screen. */
  held?: boolean;
  /** Below the selection or connection bar. */
  low?: boolean;
}>) {
  const { t } = useI18n();
  // Hover holds this notice only: a newer one starts its own time.
  const [hoveredNotice, setHoveredNotice] = useState<GraphEditNotice | null>(null);
  const hovered = Boolean(notice && hoveredNotice === notice);
  const savingLoader = useLoaderDelay(notice?.tone === "saving");
  useEffect(() => {
    if (!notice || hovered || held) return undefined;
    const duration = canvasToastDuration(notice);
    if (duration === null) return undefined;
    const timeoutId = globalThis.setTimeout(onDismiss, duration);
    return () => globalThis.clearTimeout(timeoutId);
  }, [held, hovered, notice, onDismiss]);
  if (!notice?.label) return null;
  const error = notice.tone === "error";
  const tone = error ? "danger" : notice.tone === "saving" ? "neutral" : "success";
  let icon = <CheckCircle2 aria-hidden="true" strokeWidth={2} />;
  if (error) icon = <AlertCircle aria-hidden="true" strokeWidth={2} />;
  else if (notice.tone === "saving") icon = savingLoader ? <DsLoader variant="orbit-inline" /> : <span />;
  return <DsToast className="workflow-canvas-toast" tone={tone} role={error ? "alert" : "status"} data-low={low ? "true" : undefined}
    onPointerEnter={() => setHoveredNotice(notice)} onPointerLeave={() => setHoveredNotice(null)}>
    <span className="workflow-canvas-toast-icon">{icon}</span>
    <span className="workflow-canvas-toast-text">{notice.label}</span>
    {notice.action ? <Button variant="ghost" size="sm" type="button" onClick={() => { notice.action?.onAction(); onDismiss(); }}>{notice.action.label}</Button> : null}
    <IconButton icon={X} size="small" label={t("react.workflows.dismiss", "Dismiss")} onClick={onDismiss} />
  </DsToast>;
}
