import { announceCanvasToast } from "@/pages/workflows/builder/canvas-toast-feed";

/**
 * A drawer action that changes the canvas says so in the canvas toast, with
 * Undo when it is cheap. The drawer does not own that toast, so it announces
 * the notice and the canvas, which does, shows it.
 */
export type CanvasNoticeAction = { label: string; onClick: () => void };

export type CanvasNoticeDetail = {
  label: string;
  tone: "success" | "error";
  action?: CanvasNoticeAction;
};

export function announceCanvasNotice(detail: CanvasNoticeDetail): void {
  announceCanvasToast({ label: detail.label, tone: detail.tone, ...(detail.action ? { action: { label: detail.action.label, onAction: detail.action.onClick } } : {}) });
}
